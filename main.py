import asyncio, ctypes, datetime, ipaddress, json, socket, ssl, tempfile, threading, time, math, io, secrets, urllib.parse
from collections import deque
import tkinter as tk
from http import HTTPStatus
from pathlib import Path

import qrcode
from PIL import Image, ImageTk, ImageDraw
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Controller as KeyboardController, Key
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

try:
    import mss
    sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
    USE_MSS = True
except Exception:
    from PIL import ImageGrab
    USE_MSS = False

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

PORT = 8443
HERE = Path(__file__).parent
mouse = MouseController()
keyboard = KeyboardController()
BUTTONS = {"left": Button.left, "right": Button.right}

SESSION_TOKEN = secrets.token_hex(4).upper()
authenticated_client = None

user32 = ctypes.windll.user32
SCREEN_W = user32.GetSystemMetrics(0)
SCREEN_H = user32.GetSystemMetrics(1)


def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def make_ssl_context(ip):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ip)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with tempfile.TemporaryDirectory() as tmp:
        cert_file, key_file = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
    return ctx


def wheel(delta):
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(delta), 0)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def move_cursor(dx, dy):
    p = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    ctypes.windll.user32.SetCursorPos(p.x + dx, p.y + dy)


overlay_root = None
draw_canvas = None
last_draw_pt = [None, None]
undo_stack = []
redo_stack = []
current_stroke_id = 0
current_draw_color = "#ef4444"
current_draw_size = 4


def init_draw_overlay():
    global overlay_root, draw_canvas
    overlay_root = tk.Toplevel()
    overlay_root.title("Phcursor Drawing Overlay")
    overlay_root.attributes("-fullscreen", True)
    overlay_root.attributes("-topmost", True)
    overlay_root.attributes("-transparentcolor", "#000001")
    overlay_root.config(bg="#000001")
    draw_canvas = tk.Canvas(overlay_root, bg="#000001", highlightthickness=0)
    draw_canvas.pack(fill="both", expand=True)


def draw_on_screen(action, norm_x=0, norm_y=0, color=None, size=None):
    global current_stroke_id, last_draw_pt, current_draw_color, current_draw_size
    if not draw_canvas:
        return
    x = int(norm_x * SCREEN_W)
    y = int(norm_y * SCREEN_H)
    
    if color:
        current_draw_color = color
    if size:
        current_draw_size = size

    if action == "down":
        current_stroke_id += 1
        undo_stack.append(current_stroke_id)
        redo_stack.clear()
        tag = f"stroke_{current_stroke_id}"
        last_draw_pt[0], last_draw_pt[1] = x, y
        s = current_draw_size
        draw_canvas.create_oval(x - s, y - s, x + s, y + s, fill=current_draw_color, outline=current_draw_color, tags=tag)
    elif action == "move":
        if last_draw_pt[0] is not None:
            tag = f"stroke_{current_stroke_id}"
            draw_canvas.create_line(last_draw_pt[0], last_draw_pt[1], x, y, fill=current_draw_color, width=current_draw_size*2, capstyle=tk.ROUND, smooth=True, tags=tag)
        last_draw_pt[0], last_draw_pt[1] = x, y
    elif action == "up":
        last_draw_pt[0], last_draw_pt[1] = None, None
    elif action == "undo":
        if undo_stack:
            sid = undo_stack.pop()
            redo_stack.append(sid)
            draw_canvas.itemconfigure(f"stroke_{sid}", state="hidden")
    elif action == "redo":
        if redo_stack:
            sid = redo_stack.pop()
            undo_stack.append(sid)
            draw_canvas.itemconfigure(f"stroke_{sid}", state="normal")
    elif action == "clear":
        draw_canvas.delete("all")
        undo_stack.clear()
        redo_stack.clear()
        last_draw_pt[0], last_draw_pt[1] = None, None


BUFFER_MS = 45
MAX_STEP_PER_TICK = 32.0

timeline = deque()
tl_lock = threading.Lock()
cum = [0.0, 0.0]
play_clock = [None]
last_pkt_time = [0]
s = [0.0, 0.0]
sent = [0, 0]


def schedule(d):
    t = d.get("t")
    if t is None:
        return
    with tl_lock:
        if play_clock[0] is None or (t - last_pkt_time[0] > 70):
            play_clock[0] = t - BUFFER_MS
            s[0] = cum[0]
            s[1] = cum[1]
            sent[0] = round(s[0])
            sent[1] = round(s[1])
            timeline.clear()

        last_pkt_time[0] = t
        cum[0] += float(d["dx"])
        cum[1] += float(d["dy"])
        timeline.append((t, cum[0], cum[1]))


def mover():
    global s, sent
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    last = time.perf_counter()
    while True:
        time.sleep(0.003)
        now = time.perf_counter()
        dt, last = now - last, now

        with tl_lock:
            if not timeline or play_clock[0] is None:
                continue

            target_playback_t = timeline[-1][0] - BUFFER_MS
            err = target_playback_t - play_clock[0]

            if play_clock[0] >= timeline[-1][0]:
                play_clock[0] = timeline[-1][0]
                speed = 0.0
            elif err > 15:
                speed = 1.0 + min(0.20, err / 150.0)
            elif err < -15:
                speed = 0.88
            else:
                speed = 1.0

            play_clock[0] += (dt * 1000.0) * speed

            while len(timeline) > 2 and timeline[1][0] <= play_clock[0]:
                timeline.popleft()

            p0 = timeline[0]
            if len(timeline) >= 2:
                p1 = timeline[1]
                span = max(p1[0] - p0[0], 1.0)
                alpha = min(max((play_clock[0] - p0[0]) / span, 0.0), 1.0)
                tx = p0[1] + (p1[1] - p0[1]) * alpha
                ty = p0[2] + (p1[2] - p0[2]) * alpha
            else:
                tx, ty = p0[1], p0[2]

        a = 1.0 - math.exp(-dt / 0.009)
        s[0] += (tx - s[0]) * a
        s[1] += (ty - s[1]) * a

        ix = round(s[0]) - sent[0]
        iy = round(s[1]) - sent[1]

        dist = math.hypot(ix, iy)
        if dist > MAX_STEP_PER_TICK:
            scale = MAX_STEP_PER_TICK / dist
            ix = round(ix * scale)
            iy = round(iy * scale)

        if ix or iy:
            sent[0] += ix
            sent[1] += iy
            move_cursor(ix, iy)


active_mirror_ws = {}

def capture_desktop():
    if USE_MSS:
        monitor = sct.monitors[1]
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
    else:
        img = ImageGrab.grab()
    
    cx, cy = get_cursor_pos()
    draw = ImageDraw.Draw(img)
    cursor_pts = [
        (cx, cy),
        (cx, cy + 19),
        (cx + 4, cy + 15),
        (cx + 8, cy + 23),
        (cx + 11, cy + 22),
        (cx + 7, cy + 14),
        (cx + 14, cy + 14)
    ]
    draw.polygon(cursor_pts, fill="white", outline="black")
    
    if SCREEN_W > 1600:
        target_w = 1600
        target_h = int(target_w * (SCREEN_H / SCREEN_W))
        img = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
    
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80, subsampling=0, optimize=False)
    return buf.getvalue(), cx, cy


async def mirror_loop():
    while True:
        now = time.perf_counter() * 1000
        ready_clients = [ws for ws, info in list(active_mirror_ws.items()) if info["in_flight"] < 2 or (now - info["sent_t"] > 150)]
        if ready_clients:
            try:
                frame_data, cx, cy = await asyncio.to_thread(capture_desktop)
                
                nx = int(max(0, min(cx / SCREEN_W, 1.0)) * 65535)
                ny = int(max(0, min(cy / SCREEN_H, 1.0)) * 65535)
                header = nx.to_bytes(2, "big") + ny.to_bytes(2, "big")
                payload = header + frame_data
                
                for ws in ready_clients:
                    active_mirror_ws[ws]["in_flight"] += 1
                    active_mirror_ws[ws]["sent_t"] = now
                    try:
                        await ws.send(payload)
                    except Exception:
                        active_mirror_ws.pop(ws, None)
            except Exception:
                pass
            await asyncio.sleep(0.02)
        else:
            await asyncio.sleep(0.01)


async def handler(ws):
    global authenticated_client
    req_path = getattr(ws, "request", None) and ws.request.path or ""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(req_path).query)
    token = query.get("token", [None])[0]

    if token != SESSION_TOKEN:
        await ws.close(1008, "Unauthorized: Invalid Session Token")
        return

    if authenticated_client is not None and authenticated_client != ws:
        await ws.close(1008, "Locked: Another device is active.")
        return

    authenticated_client = ws

    with tl_lock:
        play_clock[0] = None
        timeline.clear()
        cum[0] = cum[1] = 0.0
        s[0] = s[1] = 0.0
        sent[0] = sent[1] = 0
    try:
        async for raw in ws:
            try:
                if isinstance(raw, bytes):
                    continue
                d = json.loads(raw)
                
                if "ack_frame" in d:
                    if ws in active_mirror_ws:
                        active_mirror_ws[ws]["in_flight"] = max(0, active_mirror_ws[ws]["in_flight"] - 1)
                elif "mirror" in d:
                    if d["mirror"]:
                        active_mirror_ws[ws] = {"in_flight": 0, "sent_t": 0}
                    else:
                        active_mirror_ws.pop(ws, None)
                elif "draw" in d:
                    act = d["draw"]
                    draw_on_screen(
                        act.get("type"), 
                        act.get("x", 0), 
                        act.get("y", 0), 
                        act.get("color"), 
                        act.get("size")
                    )
                elif "dx" in d:
                    schedule(d)
                elif "wheel" in d:
                    wheel(d["wheel"])
                elif "btn" in d:
                    b, act = BUTTONS[d["btn"]], d.get("act", "click")
                    if act == "down":
                        mouse.press(b)
                    elif act == "up":
                        mouse.release(b)
                    else:
                        mouse.click(b)
                elif "alt_tab" in d:
                    cmd = d["alt_tab"]
                    if cmd == "start":
                        keyboard.press(Key.alt)
                        keyboard.press(Key.tab)
                        keyboard.release(Key.tab)
                    elif cmd == "next":
                        keyboard.press(Key.tab)
                        keyboard.release(Key.tab)
                    elif cmd == "release":
                        keyboard.release(Key.alt)
                elif "ctrl_backspace" in d:
                    keyboard.press(Key.ctrl)
                    keyboard.press(Key.backspace)
                    keyboard.release(Key.backspace)
                    keyboard.release(Key.ctrl)
                elif "key" in d:
                    k = d["key"]
                    if k == "Backspace":
                        keyboard.press(Key.backspace)
                        keyboard.release(Key.backspace)
                    elif k == "Enter":
                        keyboard.press(Key.enter)
                        keyboard.release(Key.enter)
                    elif k == "Tab":
                        keyboard.press(Key.tab)
                        keyboard.release(Key.tab)
                    elif k == "Space":
                        keyboard.press(Key.space)
                        keyboard.release(Key.space)
                elif "text" in d:
                    keyboard.type(d["text"])

            except (ValueError, KeyError, TypeError):
                pass
    except ConnectionClosed:
        pass
    finally:
        active_mirror_ws.pop(ws, None)
        if authenticated_client == ws:
            authenticated_client = None
        mouse.release(Button.left)
        mouse.release(Button.right)
        keyboard.release(Key.alt)


def process_request(connection, request):
    if request.path.startswith("/ws"):
        return None
    
    parsed = urllib.parse.urlparse(request.path)
    params = urllib.parse.parse_qs(parsed.query)
    token = params.get("token", [None])[0]

    if parsed.path == "/":
        if token != SESSION_TOKEN:
            resp = connection.respond(
                HTTPStatus.UNAUTHORIZED, 
                "<h2>401 Unauthorized</h2><p>Access Denied: Invalid or missing pairing token. Please scan the QR code on your laptop screen.</p>"
            )
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            return resp

        resp = connection.respond(HTTPStatus.OK, (HERE / "index.html").read_text(encoding="utf-8"))
        del resp.headers["Content-Type"]
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp
    return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")


async def server_main(ctx):
    asyncio.create_task(mirror_loop())
    async with serve(handler, "0.0.0.0", PORT, ssl=ctx,
                     process_request=process_request, compression=None):
        await asyncio.Future()


def show_ui(url):
    root = tk.Tk()
    root.title("Phcursor Pro - Secure Remote")
    root.configure(bg="#1e1e1e")
    kw = dict(fg="white", bg="#1e1e1e")
    
    tk.Label(root, text="Phcursor Secure Pairing", font=("Arial", 16, "bold"), **kw).pack(pady=(15, 2))
    tk.Label(root, text=f"Pairing Token: {SESSION_TOKEN}", font=("Arial", 11, "bold"), fg="#10b981", bg="#1e1e1e").pack(pady=(0, 6))
    
    img = qrcode.make(url).get_image().convert("RGB").resize((300, 300), Image.NEAREST)
    photo = ImageTk.PhotoImage(img)
    qr = tk.Label(root, image=photo, bg="#1e1e1e")
    qr.image = photo
    qr.pack()
    
    tk.Label(root, text=url, font=("Arial", 10), fg="#3b82f6", bg="#1e1e1e").pack(pady=6)
    tk.Label(root, text="Protected: Only devices scanning this QR code can control input.",
             font=("Arial", 10, "italic"), fg="#888", bg="#1e1e1e").pack(padx=20, pady=(0, 10))
    
    init_draw_overlay()
    root.mainloop()


if __name__ == "__main__":
    ip = get_ip()
    if ip is None:
        print("No network found. Connect to WiFi or a hotspot and restart.")
    else:
        ctx = make_ssl_context(ip)
        threading.Thread(target=mover, daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(server_main(ctx)), daemon=True).start()
        show_ui(f"https://{ip}:{PORT}/?token={SESSION_TOKEN}")

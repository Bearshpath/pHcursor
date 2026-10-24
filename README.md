# pHcursor

Turn a smartphone into an air mouse, a wireless presentation remote, and a screen annotator over your local network. No app to install: the phone side is a web page.

---

## What It Does

- **Air mouse:** Move the cursor by tilting your phone in the air (gyroscope + accelerometer sensor fusion).
- **Physical controls:** Touchpad with left click and hold, right click, and a central gyro toggle.
- **Window switcher:** A vertical sliding switch for Windows `Alt + Tab` (tap to open or cycle windows, slide down to select).
- **Screen mirroring and whiteboard:** Streams your laptop screen to the phone. Draw on the phone with your finger and the annotations appear live on the Windows desktop.
- **Remote typing:** Type on the laptop using the phone's keyboard.
- **Pairing token:** The server generates a one-time token that is embedded in the QR code, so only someone who scans it can connect. This helps on shared Wi-Fi, but it is not a full security system.

---

## Requirements

- A Windows laptop (the cursor control uses Windows-specific code)
- Python 3.10 or newer, installed from python.org (this includes `tkinter`)
- A phone with Chrome (tested on Android)
- Both devices on the same Wi-Fi network or phone hotspot

## Setup

1. Download `main.py`, `index.html` and `requirements.txt` and put them in the same folder.
2. Install the dependencies:

   ```
   pip install -r requirements.txt
   ```

3. Run the program:

   ```
   python main.py
   ```

   A window with a QR code appears. If Windows Firewall asks, allow Python on private networks.
4. Scan the QR code with your phone. Chrome will show a certificate warning because the server uses a self-signed certificate. Tap **Advanced**, then **Proceed**.
5. Tap **Start** on the phone and use it.

## Notes

- Tested on Android with Chrome and on Windows. iOS and macOS are untested.
- If the cursor feels jumpy on a busy Wi-Fi network, try your phone's hotspot, or raise `BUFFER_MS` in `main.py`.
- The certificate is regenerated each time you start the program, so the warning appears again on each run.

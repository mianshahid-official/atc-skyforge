"""
ATC SkyForge - Standalone Desktop Application
=============================================
Runs ATC SkyForge as a native desktop application (not a browser),
with native window frame, window controls, and 100% offline pipeline.
"""

import os
import sys
import time
import subprocess
import threading
from pathlib import Path

# Configure UTF-8 on Windows
if sys.platform.startswith("win"):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import uvicorn
from server import app

PORT = 5000
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}"


def start_server():
    """Runs uvicorn in a daemon thread."""
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def wait_for_server(timeout=10):
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            res = urllib.request.urlopen(f"{URL}/api/status", timeout=1)
            if res.getcode() == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False


def launch_desktop_window():
    # 1. Try pywebview native window first (WebView2 / WinForms)
    try:
        import webview
        print("[Desktop App] Launching native window via pywebview...")
        window = webview.create_window(
            title="ATC SkyForge | Aviation Emergency Video Generator",
            url=URL,
            width=1480,
            height=940,
            min_size=(1050, 720),
            background_color="#060911",
            text_select=False,
            zoomable=True,
        )
        webview.start(debug=False)
        return True
    except Exception as e:
        print(f"[Desktop App] pywebview window notice ({e}), launching standalone app frame...")

    # 2. Fallback to Edge / Chrome in dedicated standalone app frame (no browser tabs, no URL bar)
    app_launched = False
    browser_exes = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in browser_exes:
        if os.path.exists(exe):
            print(f"[Desktop App] Launching standalone window via {os.path.basename(exe)}...")
            proc = subprocess.Popen([
                exe,
                f"--app={URL}",
                "--window-size=1480,940",
                "--app-id=atc_skyforge_app"
            ])
            proc.wait()
            app_launched = True
            break

    if not app_launched:
        import webbrowser
        webbrowser.open(URL)

    return True


def main():
    print("==================================================================")
    print("  [ATC SKYFORGE] Launching Standalone Desktop Application")
    print("==================================================================")

    # Check if server is already running
    import urllib.request
    already_running = False
    try:
        res = urllib.request.urlopen(f"{URL}/api/status", timeout=1)
        if res.getcode() == 200:
            already_running = True
            print("[Desktop App] Local engine already online.")
    except Exception:
        pass

    if not already_running:
        # Start FastAPI backend in background thread
        print("[Desktop App] Starting background pipeline engine...")
        server_thread = threading.Thread(target=start_server, daemon=True)
        server_thread.start()
        wait_for_server(timeout=10)

    # Open standalone desktop window
    launch_desktop_window()

    print("[Desktop App] Application window closed. Clean exit.")
    sys.exit(0)


if __name__ == "__main__":
    main()

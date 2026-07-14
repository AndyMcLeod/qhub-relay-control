"""Q-Hub Relay Control -- double-click entry point.

Starts a local web server with the control panel and opens it in your
default browser. Leave this window open while you use the control panel;
closing it stops the server. Safe to leave running for long periods --
every command to the relay board is a short, independent connection, so a
flaky network link just means the odd retry, not a frozen program.
"""

import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from qhub_relay.config import Config, app_dir
from qhub_relay.logbook import Logbook
from qhub_relay.manager import Manager
from qhub_relay.server import run_server

HOST = "127.0.0.1"
PORT = 8420


def _already_running(url):
    """True if something is already answering our API at this address --
    most likely a previous copy of this program still running. Starting a
    second copy anyway doesn't fail loudly on Windows; it can silently
    coexist with the first, and then it's a coin flip which one answers
    each request (confusingly "losing" renames, log entries, whatever the
    other copy did). Better to detect it and hand off instead."""
    try:
        with urllib.request.urlopen(url + "api/status", timeout=0.5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def main():
    url = f"http://{HOST}:{PORT}/"
    if _already_running(url):
        print("Q-Hub Relay Control is already running -- opening your browser to it.")
        webbrowser.open(url)
        return

    config = Config()
    logbook = Logbook(app_dir())
    manager = Manager(config, logbook)
    manager.start()

    httpd = run_server(manager, host=HOST, port=PORT)

    print("Q-Hub Relay Control")
    print(f"  Device:  {config.device_ip}:{config.device_port}")
    print(f"  Panel:   {url}")
    print(f"  Log:     {logbook.today_path()}")
    print("  Press Ctrl+C to stop.")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        manager.stop()
        httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())

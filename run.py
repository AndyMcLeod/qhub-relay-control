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
import webbrowser

from qhub_relay.config import Config
from qhub_relay.manager import Manager
from qhub_relay.server import run_server

HOST = "127.0.0.1"
PORT = 8420


def main():
    config = Config()
    manager = Manager(config)
    manager.start()

    httpd = run_server(manager, host=HOST, port=PORT)
    url = f"http://{HOST}:{PORT}/"

    print("Q-Hub Relay Control")
    print(f"  Device:  {config.device_ip}:{config.device_port}")
    print(f"  Panel:   {url}")
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

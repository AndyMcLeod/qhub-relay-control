"""Minimal stdlib-only HTTP server: serves the control panel page and a small
JSON API on top of Manager. No third-party dependencies, so packaging this
into a single executable is straightforward."""

import json
import os
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .device import QHubError, NUM_CHANNELS

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_CHANNEL_ACTION_RE = re.compile(r"^/api/channel/(\d+)/(on|off|toggle|pulse|rename)$")


def make_handler(manager):
    class Handler(BaseHTTPRequestHandler):
        server_version = "QHubRelayControl/1.0"

        def log_message(self, fmt, *args):
            pass  # keep the console quiet for a non-technical user

        # -- helpers ----------------------------------------------------

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, message, status=400):
            self._send_json({"error": message}, status=status)

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        def _channel_index_or_error(self, raw_index):
            try:
                index = int(raw_index)
            except ValueError:
                return None
            if not 0 <= index < NUM_CHANNELS:
                return None
            return index

        # -- routing ------------------------------------------------------

        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                return self._serve_static("index.html", "text/html")
            if self.path == "/api/status":
                return self._send_json(manager.snapshot())
            if self.path == "/api/network":
                return self._run_action(manager.query_ip)
            if self.path == "/api/discover":
                return self._run_action(lambda: {"found": manager.find_devices()})
            self.send_error(404)

        def do_POST(self):
            if self.path == "/api/all_on":
                return self._run_action(manager.do_all_on)
            if self.path == "/api/all_off":
                return self._run_action(manager.do_all_off)
            if self.path == "/api/device_address":
                return self._handle_device_address()
            if self.path == "/api/network":
                return self._handle_set_network()

            m = _CHANNEL_ACTION_RE.match(self.path)
            if m:
                index = self._channel_index_or_error(m.group(1))
                if index is None:
                    return self._send_error_json("invalid channel index", 404)
                action = m.group(2)
                if action == "on":
                    return self._run_action(manager.do_set, index, True)
                if action == "off":
                    return self._run_action(manager.do_set, index, False)
                if action == "toggle":
                    return self._run_action(manager.do_toggle, index)
                if action == "pulse":
                    body = self._read_json_body()
                    try:
                        duration = float(body.get("duration", manager.config.default_pulse_seconds))
                    except (TypeError, ValueError):
                        return self._send_error_json("invalid duration", 400)
                    if not 0 < duration <= 3600:
                        return self._send_error_json("duration must be between 0 and 3600 seconds", 400)
                    return self._run_action(manager.do_pulse, index, duration)
                if action == "rename":
                    body = self._read_json_body()
                    name = str(body.get("name", "")).strip()
                    if not name:
                        return self._send_error_json("name must not be empty", 400)
                    manager.rename_channel(index, name[:64])
                    return self._send_json(manager.snapshot())

            self.send_error(404)

        def _run_action(self, fn, *args):
            try:
                result = fn(*args)
                return self._send_json(result)
            except QHubError as exc:
                return self._send_error_json(str(exc), 502)
            except ValueError as exc:
                return self._send_error_json(str(exc), 400)

        def _handle_device_address(self):
            body = self._read_json_body()
            ip = str(body.get("ip", "")).strip()
            if not ip:
                return self._send_error_json("ip must not be empty", 400)
            port = body.get("port")
            try:
                port = int(port) if port else None
            except (TypeError, ValueError):
                return self._send_error_json("invalid port", 400)
            manager.set_device_address(ip, port)
            return self._send_json(manager.snapshot())

        def _handle_set_network(self):
            body = self._read_json_body()
            ip = str(body.get("ip", "")).strip()
            dhcp = bool(body.get("dhcp", False))
            if not body.get("confirm") is True:
                return self._send_error_json(
                    "this changes the device's network address and requires "
                    "confirm: true -- see the warning in the control panel", 400
                )
            if not ip:
                return self._send_error_json("ip must not be empty", 400)
            return self._run_action(manager.set_ip, ip, dhcp)

        def _serve_static(self, filename, content_type):
            path = os.path.join(STATIC_DIR, filename)
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_server(manager, host="127.0.0.1", port=8420):
    handler_cls = make_handler(manager)
    httpd = Server((host, port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd

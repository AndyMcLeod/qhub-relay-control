"""Ties together the device client, on-disk config, a background status
poller, and pulse (momentary on) scheduling. This is the object the HTTP
layer talks to; it never lets a slow/dead network link block the whole
program -- every device transaction is time-bounded (see device.py) and
the poller just records the latest error and keeps retrying."""

import threading
import time

from .device import QHubClient, QHubError, NUM_CHANNELS, discover as discover_devices

POLL_INTERVAL_SECONDS = 2.0
PULSE_OFF_RETRY_DELAYS = (1.0, 2.0, 4.0)


class Manager:
    def __init__(self, config):
        self.config = config
        self.client = QHubClient(config.device_ip, config.device_port)

        self._state_lock = threading.Lock()
        self._last_status = None
        self._last_ok_time = None
        self._last_error = None
        self._pulsing = {}  # channel_index -> ends_at (epoch seconds)

        self._poll_stop = threading.Event()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)

    def start(self):
        self._poll_thread.start()

    def stop(self):
        self._poll_stop.set()

    # -- background polling -------------------------------------------------

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            self._refresh()
            self._poll_stop.wait(POLL_INTERVAL_SECONDS)

    def _refresh(self):
        try:
            status = self.client.query_all()
            self._record_success(status)
        except QHubError as exc:
            self._record_error(exc)

    def _record_success(self, status):
        with self._state_lock:
            self._last_status = status
            self._last_ok_time = time.time()
            self._last_error = None

    def _record_error(self, exc):
        with self._state_lock:
            self._last_error = str(exc)

    # -- snapshot for the UI --------------------------------------------------

    def snapshot(self):
        with self._state_lock:
            names = self.config.channel_names
            now = time.time()
            channels = []
            for i in range(NUM_CHANNELS):
                on = self._last_status["channels"][i] if self._last_status else None
                ends_at = self._pulsing.get(i)
                channels.append({
                    "index": i,
                    "name": names[i],
                    "on": on,
                    "pulsing": ends_at is not None,
                    "pulse_seconds_left": round(max(0.0, ends_at - now), 1) if ends_at else None,
                })
            return {
                "connected": self._last_status is not None and self._last_error is None,
                "error": self._last_error,
                "last_ok": self._last_ok_time,
                "voltage": self._last_status["voltage"] if self._last_status else None,
                "device_ip": self.config.device_ip,
                "device_port": self.config.device_port,
                "default_pulse_seconds": self.config.default_pulse_seconds,
                "channels": channels,
            }

    # -- actions --------------------------------------------------------------

    def do_toggle(self, index):
        status = self.client.toggle(index)
        self._record_success(status)
        return self.snapshot()

    def do_set(self, index, on):
        status = self.client.set_channel(index, on)
        self._record_success(status)
        return self.snapshot()

    def do_all_on(self):
        status = self.client.all_on()
        self._record_success(status)
        return self.snapshot()

    def do_all_off(self):
        status = self.client.all_off()
        self._record_success(status)
        return self.snapshot()

    def do_pulse(self, index, duration):
        with self._state_lock:
            if index in self._pulsing:
                raise QHubError(f"channel {index} is already pulsing")

        # Turn on synchronously so the caller gets an immediate error if the
        # device is unreachable, rather than a pulse silently never starting.
        status = self.client.set_channel(index, True)
        self._record_success(status)

        with self._state_lock:
            self._pulsing[index] = time.time() + duration

        thread = threading.Thread(
            target=self._pulse_worker, args=(index, duration), daemon=True
        )
        thread.start()
        return self.snapshot()

    def _pulse_worker(self, index, duration):
        time.sleep(duration)
        last_exc = None
        for attempt, delay in enumerate((0.0,) + PULSE_OFF_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                status = self.client.set_channel(index, False)
                self._record_success(status)
                last_exc = None
                break
            except QHubError as exc:
                last_exc = exc
                self._record_error(exc)
        with self._state_lock:
            self._pulsing.pop(index, None)
        if last_exc is not None:
            # Out of retries: the channel may still be energized. This is
            # surfaced through _last_error / snapshot()["error"] for the UI.
            pass

    # -- configuration ---------------------------------------------------------

    def rename_channel(self, index, name):
        names = self.config.channel_names
        names[index] = name
        self.config.update(channel_names=names)

    def set_device_address(self, ip, port=None):
        port = port or self.config.device_port
        self.client.host = ip
        self.client.port = port
        self.config.update(device_ip=ip, device_port=port)
        with self._state_lock:
            self._last_status = None
            self._last_error = None

    # -- network diagnostics (advanced, use with care) ------------------------

    def query_ip(self):
        """See QHubClient.query_ip's caveat: this device's own reported network
        config may not reflect its real operating address."""
        return self.client.query_ip()

    def set_ip(self, ip, dhcp):
        """See QHubClient.set_ip's caveat: unverified against real hardware,
        can leave the device unreachable if wrong. Callers must get explicit
        user confirmation before calling this."""
        self.client.set_ip(ip, dhcp)

    def find_devices(self):
        return discover_devices()

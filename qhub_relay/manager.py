"""Ties together the device client, on-disk config, a background status
poller, pulse (momentary on) scheduling, and the mission log. This is the
object the HTTP layer talks to; it never lets a slow/dead network link block
the whole program -- every device transaction is time-bounded (see
device.py) and the poller just records the latest error and keeps retrying.

On/off state is tracked here, in software, rather than read back from the
device: bench testing confirmed the device's own per-channel status field
does not track real relay position (see device.py's module docstring).
Toggle, All On, and All Off are all confirmed reliable (each verified by a
measurable supply-voltage change), so this class treats its own record of
"what did we last command" as ground truth, seeded to a known state only by
All On / All Off, and left unknown (not guessed) until one of those runs."""

import threading
import time

from .device import QHubClient, QHubError, NUM_CHANNELS, discover as discover_devices

POLL_INTERVAL_SECONDS = 2.0
PULSE_OFF_RETRY_DELAYS = (1.0, 2.0, 4.0)
HEARTBEAT_INTERVAL_SECONDS = 60.0

UNKNOWN_STATE_MESSAGE = (
    "channel {index} state is not yet known -- press All On or All Off "
    "to establish a synchronized starting point"
)


class Manager:
    def __init__(self, config, logbook=None):
        self.config = config
        self.logbook = logbook
        self.client = QHubClient(config.device_ip, config.device_port)

        self._state_lock = threading.Lock()
        self._last_status = None
        self._last_ok_time = None
        self._last_error = None
        self._pulsing = {}  # channel_index -> ends_at (epoch seconds)
        self._commanded = [None] * NUM_CHANNELS  # tri-state: None/True/False
        self._connected_logged = None  # tri-state: None (never logged) / True / False
        self._last_heartbeat = 0.0  # monotonic seconds; 0 => log one on first poll

        self._poll_stop = threading.Event()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)

    def start(self):
        self._poll_thread.start()

    def stop(self):
        self._poll_stop.set()
        if self.logbook is not None:
            self.logbook.close()

    # -- background polling -------------------------------------------------

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            self._refresh()
            self._poll_stop.wait(POLL_INTERVAL_SECONDS)

    def _refresh(self):
        try:
            status = self.client.query_all()
            self._record_success(status)
            self._log_connection_change(True, "")
            self._maybe_heartbeat(status)
        except QHubError as exc:
            self._record_error(exc)
            self._log_connection_change(False, str(exc))

    def _record_success(self, status):
        with self._state_lock:
            self._last_status = status
            self._last_ok_time = time.time()
            self._last_error = None

    def _record_error(self, exc):
        with self._state_lock:
            self._last_error = str(exc)

    # -- mission log -----------------------------------------------------------

    def _log_command(self, index, event, detail=""):
        if self.logbook is None:
            return
        name = ""
        if index is not None and 0 <= index < NUM_CHANNELS:
            name = self.config.channel_names[index]
        self.logbook.command(index, name, event, detail)

    def _log_connection_change(self, connected, detail):
        if self.logbook is None:
            return
        with self._state_lock:
            prev = self._connected_logged
            self._connected_logged = connected
        if prev == connected:
            return
        self.logbook.status("connected" if connected else "disconnected", detail)

    def _maybe_heartbeat(self, status):
        if self.logbook is None:
            return
        now = time.monotonic()
        due = False
        with self._state_lock:
            if (now - self._last_heartbeat) >= HEARTBEAT_INTERVAL_SECONDS:
                self._last_heartbeat = now
                due = True
                commanded = list(self._commanded)
        if not due:
            return
        states = ",".join("?" if c is None else ("on" if c else "off") for c in commanded)
        self.logbook.status("heartbeat", f"voltage={status.get('voltage')} channels={states}")

    # -- commanded-state tracking ---------------------------------------------

    def _ensure_state(self, index, desired_on):
        """Bring channel `index` to `desired_on`, using our own record of what
        we last commanded (never the device's status field). Raises ValueError
        if we don't yet have a known starting point for this channel. Returns
        True if a toggle was actually sent, False if it was already correct."""
        with self._state_lock:
            current = self._commanded[index]
        if current is None:
            raise ValueError(UNKNOWN_STATE_MESSAGE.format(index=index))
        if current == desired_on:
            return False
        status = self.client.toggle(index)
        self._record_success(status)
        with self._state_lock:
            self._commanded[index] = desired_on
        return True

    # -- snapshot for the UI --------------------------------------------------

    def snapshot(self):
        with self._state_lock:
            names = self.config.channel_names
            now = time.time()
            channels = []
            for i in range(NUM_CHANNELS):
                on = self._commanded[i]
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
        try:
            status = self.client.toggle(index)
        except QHubError as exc:
            self._record_error(exc)
            self._log_command(index, "toggle", f"failed: {exc}")
            raise
        self._record_success(status)
        with self._state_lock:
            current = self._commanded[index]
            self._commanded[index] = (not current) if current is not None else None
        self._log_command(index, "toggle", "ok")
        return self.snapshot()

    def do_set(self, index, on):
        event = "on" if on else "off"
        try:
            changed = self._ensure_state(index, on)
        except ValueError as exc:
            self._log_command(index, event, f"refused: {exc}")
            raise
        except QHubError as exc:
            self._log_command(index, event, f"failed: {exc}")
            raise
        self._log_command(index, event, "ok" if changed else f"ok (already {event})")
        return self.snapshot()

    def do_all_on(self):
        try:
            status = self.client.all_on()
        except QHubError as exc:
            self._record_error(exc)
            self._log_command(None, "all_on", f"failed: {exc}")
            raise
        self._record_success(status)
        with self._state_lock:
            self._commanded = [True] * NUM_CHANNELS
        self._log_command(None, "all_on", "ok")
        return self.snapshot()

    def do_all_off(self):
        try:
            status = self.client.all_off()
        except QHubError as exc:
            self._record_error(exc)
            self._log_command(None, "all_off", f"failed: {exc}")
            raise
        self._record_success(status)
        with self._state_lock:
            self._commanded = [False] * NUM_CHANNELS
        self._log_command(None, "all_off", "ok")
        return self.snapshot()

    def do_pulse(self, index, duration):
        with self._state_lock:
            if index in self._pulsing:
                raise QHubError(f"channel {index} is already pulsing")

        # Turn on synchronously so the caller gets an immediate error (including
        # "state not known yet") rather than a pulse silently never starting.
        try:
            self._ensure_state(index, True)
        except ValueError as exc:
            self._log_command(index, "pulse", f"refused: {exc}")
            raise
        except QHubError as exc:
            self._log_command(index, "pulse", f"failed to start: {exc}")
            raise

        with self._state_lock:
            self._pulsing[index] = time.time() + duration
        self._log_command(index, "pulse_start", f"{duration}s")

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
                self._ensure_state(index, False)
                last_exc = None
                break
            except (QHubError, ValueError) as exc:
                last_exc = exc
                self._record_error(exc)
        with self._state_lock:
            self._pulsing.pop(index, None)
        if last_exc is not None:
            # Out of retries: the channel may still be energized. This is
            # surfaced through _last_error / snapshot()["error"] for the UI.
            self._log_command(index, "pulse_end", f"FAILED to turn off: {last_exc}")
        else:
            self._log_command(index, "pulse_end", "ok")

    # -- configuration ---------------------------------------------------------

    def rename_channel(self, index, name):
        names = self.config.channel_names
        old = names[index]
        names[index] = name
        self.config.update(channel_names=names)
        if self.logbook is not None and old != name:
            self.logbook.command(index, name, "rename", f"{old!r} -> {name!r}")

    def set_device_address(self, ip, port=None):
        port = port or self.config.device_port
        old = f"{self.client.host}:{self.client.port}"
        self.client.host = ip
        self.client.port = port
        self.config.update(device_ip=ip, device_port=port)
        with self._state_lock:
            self._last_status = None
            self._last_error = None
            self._commanded = [None] * NUM_CHANNELS  # a different device: state is unknown again
            self._connected_logged = None
        if self.logbook is not None:
            self.logbook.status("device_address_changed", f"{old} -> {ip}:{port}")

    # -- network diagnostics (advanced, use with care) ------------------------

    def query_ip(self):
        """See QHubClient.query_ip's caveat: this device's own reported network
        config may not reflect its real operating address."""
        return self.client.query_ip()

    def set_ip(self, ip, dhcp):
        """See QHubClient.set_ip's caveat: unverified against real hardware,
        can leave the device unreachable if wrong. Callers must get explicit
        user confirmation before calling this."""
        try:
            self.client.set_ip(ip, dhcp)
        except QHubError as exc:
            if self.logbook is not None:
                self.logbook.status("network_setip", f"FAILED ip={ip} dhcp={dhcp}: {exc}")
            raise
        if self.logbook is not None:
            self.logbook.status("network_setip", f"sent ip={ip} dhcp={dhcp}")

    def find_devices(self):
        return discover_devices()

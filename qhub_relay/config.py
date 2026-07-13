"""Small JSON config file living next to the program, so channel names and the
device address survive restarts without needing a database or install step."""

import json
import os
import sys
import threading

from .device import NUM_CHANNELS

DEFAULT_DEVICE_IP = "192.168.1.210"
DEFAULT_DEVICE_PORT = 23
DEFAULT_CHANNEL_NAMES = [f"Channel {i + 1}" for i in range(NUM_CHANNELS)]
DEFAULT_PULSE_SECONDS = 1.0


def app_dir():
    """Directory the program lives in -- works both as a plain script and as
    a PyInstaller --onefile executable."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    return os.path.join(app_dir(), "config.json")


_DEFAULTS = {
    "device_ip": DEFAULT_DEVICE_IP,
    "device_port": DEFAULT_DEVICE_PORT,
    "channel_names": DEFAULT_CHANNEL_NAMES,
    "default_pulse_seconds": DEFAULT_PULSE_SECONDS,
}


class Config:
    def __init__(self, path=None):
        self.path = path or config_path()
        self._lock = threading.Lock()
        self._data = dict(_DEFAULTS)
        self.load()

    def load(self):
        with self._lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        on_disk = json.load(f)
                    merged = dict(_DEFAULTS)
                    merged.update(on_disk)
                    names = merged.get("channel_names") or []
                    if len(names) != NUM_CHANNELS:
                        names = (names + DEFAULT_CHANNEL_NAMES)[:NUM_CHANNELS]
                    merged["channel_names"] = names
                    self._data = merged
                except (json.JSONDecodeError, OSError):
                    # Corrupt/unreadable config: keep defaults rather than crash.
                    self._data = dict(_DEFAULTS)
            else:
                self._data = dict(_DEFAULTS)
                self._save_locked()

    def _save_locked(self):
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp_path, self.path)

    def save(self):
        with self._lock:
            self._save_locked()

    def as_dict(self):
        with self._lock:
            return dict(self._data)

    def update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)
            self._save_locked()

    @property
    def device_ip(self):
        return self._data["device_ip"]

    @property
    def device_port(self):
        return self._data["device_port"]

    @property
    def channel_names(self):
        return list(self._data["channel_names"])

    @property
    def default_pulse_seconds(self):
        return self._data["default_pulse_seconds"]

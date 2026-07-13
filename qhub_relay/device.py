"""
Wire protocol client for the OTAQ Q-Hub relay board (S0272A) reached through
its underwater switch/relay enclosure over Ethernet.

The protocol was recovered by decompiling OTAQ's own "QHub.exe" control
utility (found already installed on this machine) rather than from public
documentation, since none exists. Frames look like:

    $<command>*<checksum>\r\n

where <checksum> is a 4-digit uppercase hex Fletcher-16-style checksum
(mod 255) of <command> alone, and the device echoes the same framing back
in its responses. The device accepts exactly one TCP connection at a time
on port 23 and closes immediately on anything it doesn't recognise, so
every transaction here opens a fresh short-lived socket.

Commands used:
    MR1#1,all            -> query all channel/sensor state
    MR1#1,relay,<0-5>    -> toggle channel (0-based index)
    MR1#1,allon          -> force all channels on
    MR1#1,alloff         -> force all channels off

Response fields (comma-split, 0-indexed after the "MR1#1" prefix):
    [1..6]  per-channel state, reported from downstream current draw --
            with no load attached (e.g. bench testing) these read 0
            regardless of relay position. This matches the vendor app's
            own behaviour; it is a hardware/telemetry property, not a
            bug in this client.
    [14]    supply voltage in millivolts
"""

import socket
import threading

DEFAULT_PORT = 23
NUM_CHANNELS = 6
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 3.0
RECV_BUFSIZE = 1024


class QHubError(Exception):
    """Base class for all Q-Hub communication errors."""


class DeviceUnreachableError(QHubError):
    """Could not open/complete a TCP transaction with the device in time."""


class ProtocolError(QHubError):
    """Device responded, but the response was malformed or failed checksum."""


def checksum(command: str) -> int:
    """Fletcher-16-style checksum (mod 255), matching QHub.exe's GetChecksum(s, 16)."""
    sum1 = 0
    sum2 = 0
    for byte in command.encode("ascii"):
        sum1 = (sum1 + byte) % 255
        sum2 = (sum2 + sum1) % 255
    return sum1 + sum2 * 256


def frame(command: str) -> bytes:
    return f"${command}*{checksum(command):04X}\r\n".encode("ascii")


def parse_response(raw: str):
    """Validate framing/checksum and return the comma-separated fields (fields[0] == 'MR1#1')."""
    raw = raw.strip()
    parts = raw.split("*")
    if len(parts) != 2:
        raise ProtocolError(f"malformed response (no checksum delimiter): {raw!r}")
    body = parts[0].strip("$*")
    tail = parts[1].strip("\r\n")
    try:
        received = int(tail, 16)
    except ValueError:
        raise ProtocolError(f"non-hex checksum in response: {raw!r}")
    expected = checksum(body)
    if received != expected:
        raise ProtocolError(
            f"checksum mismatch (device sent {received:04X}, expected {expected:04X}): {raw!r}"
        )
    return body.split(",")


def decode_status(fields):
    if len(fields) < 7:
        raise ProtocolError(f"status response too short: {fields!r}")
    channels = [bool(int(fields[i])) for i in range(1, 1 + NUM_CHANNELS)]
    voltage = None
    if len(fields) > 14:
        try:
            voltage = int(fields[14]) / 1000.0
        except ValueError:
            voltage = None
    return {"channels": channels, "voltage": voltage}


class QHubClient:
    """Talks to one Q-Hub unit. Safe to share across threads: device I/O is
    serialized through a timed lock so a stuck/slow link fails fast instead
    of wedging every other caller (and never leaves the device's single
    connection slot occupied)."""

    def __init__(self, host, port=DEFAULT_PORT,
                 connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._lock = threading.Lock()

    def _transact(self, command: str):
        lock_budget = self.connect_timeout + self.read_timeout + 2.0
        if not self._lock.acquire(timeout=lock_budget):
            raise DeviceUnreachableError("device busy: another command is still in flight")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(self.connect_timeout)
                sock.connect((self.host, self.port))
                sock.settimeout(self.read_timeout)
                sock.sendall(frame(command))
                data = sock.recv(RECV_BUFSIZE)
            except socket.timeout:
                raise DeviceUnreachableError(
                    f"timed out talking to {self.host}:{self.port}"
                )
            except OSError as exc:
                raise DeviceUnreachableError(
                    f"could not reach {self.host}:{self.port}: {exc}"
                )
            finally:
                sock.close()
        finally:
            self._lock.release()

        if not data:
            raise DeviceUnreachableError("device closed the connection without responding")
        return parse_response(data.decode("ascii", errors="replace"))

    def query_all(self):
        return decode_status(self._transact("MR1#1,all"))

    def toggle(self, channel_index: int):
        if not 0 <= channel_index < NUM_CHANNELS:
            raise ValueError(f"channel_index out of range: {channel_index}")
        return decode_status(self._transact(f"MR1#1,relay,{channel_index}"))

    def all_on(self):
        return decode_status(self._transact("MR1#1,allon"))

    def all_off(self):
        return decode_status(self._transact("MR1#1,alloff"))

    def set_channel(self, channel_index: int, desired_on: bool):
        """Explicit on/off on top of the device's native toggle-only relay command:
        read current state, toggle only if it doesn't already match."""
        status = self.query_all()
        if status["channels"][channel_index] != desired_on:
            status = self.toggle(channel_index)
        return status

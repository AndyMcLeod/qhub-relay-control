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
    MR1#1,ip             -> query the device's stored network config
    MR1#1,setip,<...>    -> write a new network config (see set_ip below)

Response fields (comma-split, 0-indexed after the "MR1#1" prefix):
    [1..6]  per-channel state. Confirmed unreliable on the unit this was
            tested against: toggling a channel and confirming the relay
            actually energized (via a measurable supply-voltage sag,
            and reproducibly reversing it with a second toggle) still
            leaves this field at 0 throughout. It matches the vendor
            app's own behaviour, so it is a device/firmware property,
            not a bug in this client -- but it means neither this client
            nor QHub.exe can use it as ground truth for on/off state.
            QHubClient still decodes it (decode_status below) for anyone
            who wants the raw value, but callers that need to know
            whether a channel is actually on should track their own
            commanded state instead; see Manager in manager.py.
    [14]    supply voltage in millivolts

Discovery: the device also answers a UDP broadcast on port 50501 -- the
same $<command>*<checksum>\r\n framing, command "?", sent to
255.255.255.255:50501, gets a "$MR1#1*<checksum>\r\n" reply from each
Q-Hub on the local segment. Useful when the device's address isn't known
(e.g. after it's been reconfigured, or on an unfamiliar boat network).

Caveat on MR1#1,ip / MR1#1,setip: querying a real unit on the bench
returned "255.255.255.255" with DHCP off, and OTAQ's own QHub.exe showed
the identical value in its "Set IP" dialog -- so this is a genuine
firmware/protocol quirk, not a bug in this client, and this field should
not be trusted as the device's real operating address. set_ip() is
implemented faithfully against the decompiled protocol (verified byte-for-
byte against QHub.exe) but was deliberately NOT exercised against real
hardware here, since a wrong write could leave the device unreachable
without physical/serial access once it's mounted on a vehicle. Treat it
as an advanced, use-at-your-own-risk feature.
"""

import socket
import threading
import time

DEFAULT_PORT = 23
DISCOVERY_PORT = 50501
NUM_CHANNELS = 6
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 3.0
DISCOVERY_TIMEOUT = 4.0
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


def decode_ip_info(fields):
    if len(fields) < 6:
        raise ProtocolError(f"ip response too short: {fields!r}")
    try:
        octets = tuple(int(fields[i]) for i in range(1, 5))
        dhcp = fields[5] != "0"
    except ValueError:
        raise ProtocolError(f"non-numeric ip fields: {fields!r}")
    return {"ip": ".".join(str(o) for o in octets), "dhcp": dhcp}


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

    def query_ip(self):
        """Read back the device's stored network config. See the caveat in this
        module's docstring: on the unit this was verified against, this reads as
        255.255.255.255 regardless of the device's real operating address --
        OTAQ's own app shows the same thing, so don't treat this as ground truth."""
        return decode_ip_info(self._transact("MR1#1,ip"))

    def set_ip(self, ip: str, dhcp: bool):
        """Write a new network config. Mirrors QHub.exe's "Set IP" dialog exactly
        (verified against the decompiled protocol, not against real hardware --
        see the module docstring). A wrong IP/mask here can make the device
        unreachable until it's re-addressed via physical/serial access, so this
        is intentionally not wired into routine use -- callers should get
        explicit user confirmation first."""
        octets = ip.split(".")
        if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255 for o in octets):
            raise ValueError(f"not a valid IPv4 address: {ip!r}")
        suffix = 128 if dhcp else 0
        command = f"MR1#1,setip,{'.'.join(octets)}.{suffix}"
        self._transact(command)
        # The device doesn't send a status reply to this command (fire-and-forget
        # in the vendor app too), so there is nothing here to validate/decode.


def discover(timeout=DISCOVERY_TIMEOUT, udp_port=DISCOVERY_PORT):
    """Broadcast a UDP query for any Q-Hub on the local network segment and
    return the IP addresses that answered. Safe/read-only. Filters out this
    machine's own addresses, since Windows loops broadcast packets back to
    the sending socket (the vendor app does the same filtering, for the same
    reason)."""
    local_ips = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            local_ips.add(info[4][0])
    except OSError:
        pass
    local_ips.add("127.0.0.1")

    found = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", udp_port))
        sock.settimeout(0.5)
        sock.sendto(frame("?"), ("255.255.255.255", udp_port))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (addr, _port) = sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            if addr in local_ips or addr in found:
                continue
            try:
                parse_response(data.decode("ascii", errors="replace"))
            except ProtocolError:
                continue
            found.append(addr)
    finally:
        sock.close()
    return found

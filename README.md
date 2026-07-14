# Q-Hub Relay Control

A small standalone tool for operating the relay channels on an **OTAQ Offshore
Q-Hub** (underwater Ethernet switch + power relay enclosure, relay board
S0272A) from a browser, over Ethernet. Built for controlling the board from a
laptop on the bench or from a boat/vehicle control PC — no installer, no
dependencies, one program.

![Q-Hub Relay Control screenshot](screenshot.png)

## Features

- Per-channel **On**, **Off**, **Toggle**, and a configurable-duration
  **momentary Pulse**
- **All On** / **All Off**
- Editable channel names (placeholders `Channel 1`..`Channel 6` until you
  rename them for your install) that persist between runs
- Live status panel (voltage, per-channel state) in any browser, polling the
  device in the background
- Built to survive a flaky, long-lived link: every device command is a short,
  independent connection with a bounded timeout, so a dropped or slow network
  degrades to a clear "disconnected" state instead of freezing the program
- **Network settings (advanced)** panel: broadcast discovery to find a Q-Hub
  on the local network segment, a device network-config query, and a
  static-IP/DHCP write -- see the warning under [The protocol](#the-protocol)
  before using the last one

## Quick start

1. Download `QHubRelayControl.exe` from the [Releases](../../releases) page.
2. Put it anywhere and double-click it. A console window opens and your
   browser opens the control panel automatically at `http://127.0.0.1:8420/`.
3. If your Q-Hub isn't at the default address (`192.168.1.210`), type the
   correct IP into the **Device** field at the top and click **Set**.
4. Leave the console window running while you use the panel; closing it stops
   the local server. Every action is a fresh, short connection to the device,
   so it's safe to leave running for long periods — a network blip just means
   the status panel shows "disconnected" until the link comes back.

Channel names and the device address are saved to `config.json`, created next
to the program the first time it runs.

## Running from source

Requires Python 3.9+, no third-party packages.

```
python run.py
```

## Building the executable yourself

```
python -m venv .venv-build
.venv-build\Scripts\pip install pyinstaller
.venv-build\Scripts\pyinstaller --onefile --name QHubRelayControl ^
    --add-data "qhub_relay/static;qhub_relay/static" run.py
```

The result is `dist/QHubRelayControl.exe`.

## The protocol

OTAQ doesn't publish the Q-Hub's control protocol. This tool's `qhub_relay/device.py`
was recovered by decompiling OTAQ's own Windows control utility (`QHub.exe`)
and confirmed against a real unit on the bench. Summary, for anyone extending
this or building their own client:

- TCP port 23. The device accepts exactly **one connection at a time** and
  closes immediately on anything it doesn't recognise — open a fresh short
  connection per command, don't hold one open.
- Frames look like `$<command>*<CCCC>\r\n`, where `<CCCC>` is a 4-digit
  uppercase hex checksum of `<command>` alone: a Fletcher-16-style checksum
  (two running sums mod 255). The device echoes the same framing in its
  replies and expects it to check out.
- Commands: `MR1#1,all` (query), `MR1#1,relay,<0-5>` (toggle one channel,
  0-indexed), `MR1#1,allon`, `MR1#1,alloff`, `MR1#1,ip` (query network config),
  `MR1#1,setip,<a>.<b>.<c>.<d>.<128|0>` (write network config -- suffix `128`
  if the vendor app's DHCP checkbox was ticked, `0` if not).
- Status replies are comma-separated: fields 1-6 are per-channel state,
  field 14 is supply voltage in millivolts.
- Broadcast discovery: UDP, command `?`, sent to `255.255.255.255:50501` from
  a socket bound to local port 50501; each Q-Hub on the segment replies with
  `$MR1#1*<checksum>\r\n`. Filter out your own machine's addresses -- Windows
  loops broadcast packets back to the sending socket, and the vendor app does
  the same filtering for the same reason.

**Caveat 1:** field 1-6 state is derived from downstream current draw, not raw
relay contact position. With no load on a channel (e.g. bench testing with
nothing plugged in) it reads as "off" regardless of the relay's actual
position — this matches the vendor's own app, not a bug here. Once real loads
are wired up on the vehicle, readback should track actual state. Because of
this, on/off/pulse are implemented as read-current-state-then-toggle-if-needed
on top of the device's native toggle-only relay command, exactly like the
vendor app does.

**Caveat 2:** `MR1#1,ip` returned `255.255.255.255` with DHCP off on the unit
this was tested against, rather than its real operating address -- and OTAQ's
own `QHub.exe` showed the identical value in its "Set IP" dialog, so this is a
genuine firmware quirk, not a bug in this client. Don't treat that query as
ground truth. `MR1#1,setip` (the **Set a new IP address** control in the
Network settings panel) is implemented faithfully against the decompiled
protocol but was *not* exercised against real hardware, since a wrong write
could leave the device unreachable until it's re-addressed via physical/serial
access -- treat it as advanced, use-at-your-own-risk functionality, same as in
the vendor app.

## License

MIT — see [LICENSE](LICENSE).

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
- On/Off/Pulse track real relay state in software rather than trusting the
  device's own status field (which doesn't reflect it -- see the caveat
  below). On startup, or after pointing the tool at a different device, it
  doesn't wait for you: it sends **All Off** on its own as soon as it can
  reach the device, always the fail-safe direction, never All On. Per-channel
  On/Off/Pulse stay disabled with a banner until that lands (or until you
  press All On yourself, which cancels the automatic All Off and counts as
  your own synchronization instead). If the device isn't reachable yet, it
  just keeps trying every couple of seconds -- no page refresh or button
  press needed once the link comes up. **Toggle** always works, since it
  doesn't need to know the starting state.
- Editable channel names (placeholders `Channel 1`..`Channel 6` until you
  rename them for your install), saved to `config.json` immediately on edit
  and reloaded on every start -- survives power cycles and separate missions
  the same way
- Live status panel (voltage, per-channel state) in any browser, polling the
  device in the background
- Built to survive a flaky, long-lived link: every device command is a short,
  independent connection with a bounded timeout, so a dropped or slow network
  degrades to a clear "disconnected" state instead of freezing the program
- **Network settings (advanced)** panel: broadcast discovery to find a Q-Hub
  on the local network segment, a device network-config query, and a
  static-IP/DHCP write -- see the warning under [The protocol](#the-protocol)
  before using the last one
- **Mission log**: every command and every connection change, timestamped
  with this computer's local clock (the device has no clock of its own), one
  CSV file per day in `logs/`. Viewable and downloadable from the panel --
  see [The mission log](#the-mission-log) below
- Refuses to run a second copy alongside a first: launching it again while
  one is already open just brings up your browser to the running instance,
  instead of silently starting a second server that fights the first one
  over which copy's changes actually stick

## Quick start

1. Download `QHubRelayControl.exe` from the [Releases](../../releases) page.
2. Put it anywhere and double-click it. A console window opens and your
   browser opens the control panel automatically at `http://127.0.0.1:8420/`.
3. If your Q-Hub isn't at the default address (`192.168.1.210`), type the
   correct IP into the **Device** field at the top and click **Set**.
4. Wait a couple of seconds for it to synchronize on its own (it sends All
   Off automatically -- see Features above), or press **All On** yourself if
   you'd rather start there. Per-channel controls stay disabled until one of
   those lands.
5. Leave the console window running while you use the panel; closing it stops
   the local server. Every action is a fresh, short connection to the device,
   so it's safe to leave running for long periods — a network blip just means
   the status panel shows "disconnected" until the link comes back, and it
   keeps checking in the background the whole time. No refresh needed: once
   the device answers again, the panel and the log both pick it up on their
   own within a couple of seconds.

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

## The mission log

Every command (on/off/toggle/pulse/all-on/all-off/rename/network changes) and
every connection-state change (connected, disconnected, a periodic
one-per-minute heartbeat with voltage and channel states) is appended to
`logs/qhub-log-<YYYY-MM-DD>.csv`, one file per local calendar day. Timestamps
are this computer's local wall-clock time -- the Q-Hub's wire protocol
carries no clock or timestamp of its own, so there's nothing else to log
against. The **Mission log** panel in the control page shows the current
day's recent activity and lets you download any day's CSV; it also opens
directly in Excel/Sheets/Numbers if you'd rather work from the file on disk.
Columns: `timestamp, type, channel_index, channel_name, event, detail`, where
`type` is `command` or `status`.

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

**Caveat 1:** field 1-6 does not reliably reflect relay contact position, full
stop. It was first suspected from current-draw reasoning on an unloaded bench
unit; it's now confirmed directly. Toggling a channel and independently
verifying the relay actually energized (a measurable supply-voltage sag,
reproducibly reversed by a second toggle) still leaves this field at 0. This
matches the vendor's own app, so it's a device/firmware property, not a bug
here -- but it rules the field out as ground truth, loaded or not.

An earlier version of this tool used read-current-state-then-toggle-if-needed
against that field, the same pattern the vendor app uses. In practice that
meant the Off button silently did nothing: it always read the channel as
already off and skipped sending the toggle, even when the relay was actually
on. On/Off/Pulse now track commanded state in software instead (see Features
above), seeded only by All On / All Off, which are the only two commands
confirmed to set state unconditionally rather than merely flip it. The
program seeds that state itself, automatically, with All Off as soon as it
can reach the device -- it does not wait for a button press, and it never
defaults to All On on its own.

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

# CCR screen — edge device setup (display only)

The edge device next to the DCS console only **shows** the advisor (and,
optionally, the twin on a second screen). All ML, the optimizer and the live
feeds run on the GPU server; the device just needs a browser and a network
path to it. If the server or network drops, the advisor page itself says so
("twin offline", "no live feed") and recommendations are blocked — it never
shows stale values as current.

```
 CCR edge device ──http──▶ GPU server 10.53.2.10
   Edge/Chromium kiosk      :8010/advisor  (advisor + API)
   (auto-start, watchdog)   :3010/twin     (optional 2nd screen)
```

## What IT needs to provide
1. A device (Windows 10/11 or Linux, x86 or ARM) with a screen (touch, or a mouse
   for Accept/Decline), auto-login to a restricted local "kiosk" account.
2. Network: the CCR network allowed to reach `10.53.2.10` on TCP **8010**
   (and **3010** for the twin screen). No inbound access to the device needed.
3. Security sign-off: the device only reads web pages; nothing on it can write
   to the DCS (the advisor is advisory-only end to end).

## Install
- **Windows** (as the kiosk user):
  ```powershell
  powershell -ExecutionPolicy Bypass -File setup-kiosk.ps1
  # with the twin on a second monitor:
  powershell -ExecutionPolicy Bypass -File setup-kiosk.ps1 -SecondUrl http://10.53.2.10:3010/twin
  ```
- **Linux** (as the kiosk user, X11 desktop with auto-login):
  ```bash
  bash setup-kiosk.sh http://10.53.2.10:8010/advisor
  ```
Each script checks the server is reachable, starts the browser full-screen on
the advisor, relaunches it if it closes, and disables screen blanking/sleep.
Remove with `-Remove` / `--remove`.

## Check on site
- [ ] Device reaches `http://10.53.2.10:8010/api/health` (`{"status":"ok"}`)
- [ ] Advisor opens full-screen after a reboot, without anyone touching it
- [ ] Closing the window (Alt+F4) brings it back within ~15 s
- [ ] Screen stays on overnight
- [ ] Pull the network cable: page shows "offline", Accept/Decline disabled; reconnect: recovers by itself
- [ ] Operators can read it from their seat (font size, brightness)

## Status
Windows script syntax-checked (script and generated watchdog parse); Linux
script reviewed by hand (no bash available on the development laptop). Neither
has run on a real edge device yet.

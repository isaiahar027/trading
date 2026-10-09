# Windows VPS runbook (UniTrader demo test)

The desk drives UniTrader the way a person would: it looks at the screen and moves the mouse. That only
works inside a **logged-in, unlocked Windows desktop session** on the VPS that runs UniTrader. This page
covers setting that up and keeping it running for a week.

> The desk never logs in for you. You log in to Windows and to UniTrader yourself. If UniTrader logs out,
> crashes or shows a different account, the desk stops trading and sends you an alert. You log back in
> yourself and the desk carries on.

## 1. One-time setup

1. **Python 3.11+** from python.org (tick "Add python.exe to PATH").
2. Copy the `amongus-desk` folder to the VPS (e.g. `C:\desk`) and run
   `powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1`.
3. **Keep the session awake.** In Windows settings, set "Screen: turn off" and "Sleep" to *Never*,
   disable the screen saver and the lock-screen timeout. A locked screen makes screenshots go black and
   clicks fail (the desk detects this and halts).
4. **Display scaling.** Pick one scaling level (100% is simplest) and keep it. The desk refuses to click
   if the terminal's DPI or window size differs from what you calibrated.
5. **Windows Update.** Set active hours that cover your trading hours, so an automatic restart doesn't
   happen mid-session. After any reboot you must log in to Windows and UniTrader again; the desk waits
   for you.

## 2. Instrument specs (no guessing)

Open UniTrader's symbol / contract specification for each instrument you enable. Copy **contract size**,
**lot step** and **minimum lot** into `config\desk.yaml`, then set `specs_verified: true` for that
instrument. Until you do, demo and live mode refuse to trade it. The values in the example config are
placeholders.

Also find out from UnitedPips whether your account is **netting or hedging**. This decides which exit
method is safe: with the default `close_all` exit, `max_open_positions: 1` is enforced.

## 3. Calibrate the screen layout

1. Open UniTrader and **log in to the DEMO account yourself**.
2. Open the one-click trading panels for the instruments you enabled and arrange the window the way it
   will stay all week. Don't resize it afterwards.
3. Run `.\.venv\Scripts\desk.exe calibrate -c config\desk.yaml`. The wizard asks you to hover the mouse
   over each element (lot field, BUY, SELL, the symbol label of each panel, the DEMO account badge, a
   spot that changes only when a position opens/closes, the close-all button if there is one, ...). It
   then captures reference images. You never need to click the terminal during calibration.
4. The wizard re-checks everything and writes `config\unitrader.layout.yaml` + `config\templates\`. Those
   files contain screenshots of your terminal, so they are git-ignored. Keep them on the VPS.

Re-calibrate whenever UniTrader updates its look, you rearrange panels, or the resolution/scaling changes.

## 4. Preflight, then a shadow day

```powershell
# config\desk.yaml: mode: demo, execution.venue: unitrader, execution.dry_run: true
.\.venv\Scripts\desk.exe preflight -c config\desk.yaml --wait-for-login
powershell -File scripts\windows\run-desk.ps1
```

`preflight` checks the window, client size, DPI, the account badge and every panel label without typing
or clicking anything. If the window or badge is missing it tells you to log in, waits for you to press
Enter, and re-checks. With `dry_run: true` the desk runs every pre-click check on real signals but never
types or clicks. Run it like that for a day before letting it trade.

`desk test-order --symbol EURUSD --side BUY --lots 0.01` is a round trip: it places exactly one order
(after you type a confirmation), lets you watch the full click sequence, then closes the position with
the calibrated close method when you press Enter.

## 5. Running unattended

* `scripts\windows\register-task.ps1` registers a Task Scheduler task that starts the desk when **you**
  log on. It runs in your interactive session; a task that runs "whether the user is logged on or not"
  has no desktop and could not see UniTrader.
* **Closing Remote Desktop normally locks the session.** To disconnect without locking, run
  `scripts\windows\keep-session-on-console.bat` as administrator from inside the RDP session. The session
  may switch to the console resolution; if the terminal window size changes, the desk refuses to click
  until it matches again. Calibrate at the resolution the session will actually have.
* **Stop trading from anywhere:**
  * the dashboard **Kill** button;
  * `desk kill` on the VPS;
  * create an empty file named `KILL` in the `data` folder. Delete it, or press Resume on the
    dashboard, to continue.
* **Panic** (dashboard, hold for 2 s) engages the kill switch and clicks the calibrated close-all button.
  If you did not calibrate one, it only engages the kill switch and tells you to close positions by hand.

## 6. Reaching the dashboard from your phone

The dashboard listens on `127.0.0.1:8765` only. Don't open that port to the internet. Pick one:

* **SSH tunnel** (OpenSSH Server on the VPS): `ssh -L 8765:127.0.0.1:8765 you@vps`, then open
  `http://127.0.0.1:8765`.
* **Tailscale** on the VPS and the phone: set `dashboard.host` to the VPS's Tailscale IP, add that IP to
  `dashboard.allowed_hosts`, and set `DESK_DASHBOARD_TOKEN` (24+ random characters).

The page asks for the dashboard token once and keeps it on that device. Phone alerts go through ntfy or
Telegram (`.env`).

## 7. Every evening of the test

1. Compare UniTrader's trade history with the desk's dashboard (Orders / Positions). The desk's P&L is
   an **estimate**: it can't read the broker's ledger. Re-sync equity from the dashboard when it drifts.
2. Resolve every reconcile card. An UNCONFIRMED order means the click happened but the desk couldn't
   prove a fill. Check the terminal and answer "filled" or "not filled". Trading stays halted until you
   do.
3. Check `data\evidence\` for the before/after screenshots of each order, and run `desk audit-verify` to
   confirm the journal hasn't been altered.

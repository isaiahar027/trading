@echo off
rem Disconnects your Remote Desktop window WITHOUT locking the Windows session, so the desk can keep
rem seeing and clicking UniTrader after you close RDP. (Closing RDP normally locks the session: screenshots
rem go black and simulated clicks fail -- the desk detects that and stops trading.)
rem
rem Run it from INSIDE the RDP session, "Run as administrator". Your RDP window closes; reconnect normally.
rem Note: the session may switch to the console's screen resolution. If the terminal's window size changes,
rem the desk refuses to click until it matches the calibration again -- calibrate at that resolution.
for /f "skip=1 tokens=3" %%s in ('query user %USERNAME%') do (
  %windir%\System32\tscon.exe %%s /dest:console
)

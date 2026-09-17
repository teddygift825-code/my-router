# Dual-Router Dashboard — Desktop Edition (Windows)

A Windows desktop app that monitors a two-router chain:

    internet ──► OUTDOOR router (WISP) 192.168.1.1
                     └──► INDOOR router (LAN) 192.168.0.1 ──► your devices

## Install (pick one)

**A. One-click build + install** (recommended, needs Python 3.9+ once):

    double-click  build_desktop.bat

It generates the icon, builds `dist\DualRouterDashboard.exe` with
PyInstaller (auto-installed if missing) and puts a shortcut on your
Desktop and Start Menu. Afterwards you never need Python again —
the exe is fully standalone.

**B. Real installer** (optional): install [Inno Setup](https://jrsoftware.org/isinfo.php),
then compile `installer.iss` → you get `dist\Output\DualRouterDashboardSetup-2.0.0.exe`
with Start-menu + desktop shortcuts and an uninstaller.

**C. Run from source**:

    python dual_router_gui.py

## Daily use

- `Desktop ▸ Dual-Router Dashboard` — main window.
  First launch auto-discovers both routers (≈1 min) and keeps a live
  device list with custom names, vendors and ping latency.
- Right-click (or double-click) any device → ping monitor, traceroute,
  port scan, open in browser, copy IP/MAC, set a custom name.
- Toolbar: rescan, speed test, ping monitor, traceroute, port scan,
  ARP table, CSV/JSON export, open router admin page, settings, about.
- Bottom-right panel: live latency graph (device / indoor / outdoor /
  internet) and an event log with join/leave alerts (sound + toast).
- `Ctrl+E` save CSV snapshot · `Ctrl+J` save JSON · `F5` rescan.

## Settings & data

Stored in `%APPDATA%\DualRouterDashboard\` (`config.json`, `aliases.json`):
refresh interval, auto-scan cadence, sound/toasts, dark/light theme,
manual indoor/outdoor router IPs (blank = auto), internet probe target,
scan batch size.

## Command line

    DualRouterDashboard.exe              start the GUI
    DualRouterDashboard.exe --check      console topology self-check, then exit
    DualRouterDashboard.exe --smoke f    auto-quit self-test → JSON file
    DualRouterDashboard.exe --version    print version

## Troubleshooting

- *Windows Defender SmartScreen* may warn on first run of the exe —
  choose "More info ▸ Run anyway" (the exe is built locally on your PC).
- *No devices found*: Windows blocks ICMP for non-admins on some setups;
  right-click the exe → Properties → Compatibility → "Run as administrator"
  if your LAN devices stay invisible.
- *Outdoor router not found*: the WISP box must answer traceroute hops.
  You can pin it manually in ⚙ Settings (default 192.168.1.1).
- Rebuild anytime with `build_desktop.bat`; settings survive upgrades.

## Files in this folder

    dual_router_gui.py      the GUI application (tkinter, stdlib only)
    dual_router_dashboard.py  shared networking core (also runs as a
                            terminal dashboard — see its docstring)
    make_icon.py            regenerates app_icon.ico/png (no PIL needed)
    build_desktop.bat       one-click build + shortcut install
    install_shortcut.ps1    helper used by the bat file
    installer.iss           optional Inno Setup installer script
    dist\DualRouterDashboard.exe   the built application

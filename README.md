# 🌐 Dual-Router Dashboard

A colourful Windows desktop app for watching everything connected to a
dual-router (WISP) network — indoor LAN **and** the WAN leg between the
routers — plus tools: speed test, ping monitor, traceroute, port scan,
access control with a device quota, and manual device registration.

![status](https://img.shields.io/badge/platform-Windows-blue)
![python](https://img.shields.io/badge/python-3.9%2B-green)
![license](https://img.shields.io/badge/license-MIT-orange)

## ✨ Features

| | |
|---|---|
| 🖥 **Live device table** | LAN + WAN tabs, ping latency, MAC, vendor, first/last seen |
| 🌈 **5 vivid themes** | Sunset · Ocean · Neon · Candy · Forest — no boring black |
| 📶 **Access & quota** | Set how many devices may use the internet; add devices manually by LAN IP or MAC; live gauge in the header |
| ⚡ **Speed test** | Download / upload / latency / jitter — built in, no external deps |
| 📡 **Ping monitor** | Continuous ping with min/avg/max/loss stats |
| 🗺 **Traceroute viewer** | With per-hop ping verification |
| 🔌 **Port scanner** | 80 common TCP ports with service names |
| 📊 **Latency graph** | Live canvas graph, min/avg/max |
| 🔔 **Alerts** | Sound + toast pop-ups when devices or routers go down/up |
| 📤 **Export** | CSV / JSON snapshot of the whole network |
| ⚙ **Settings** | Router IPs, probe host, scan cadence, themes — persisted in `%APPDATA%` |

## 📥 Download

Grab the latest executable from
**[Releases](../../releases)** or from a workflow run's
**Artifacts** section (Actions → build → `DualRouterDashboard`).
No Python required — it's a single standalone `.exe`.

## 🛠 Build it yourself

```bat
build_desktop.bat
```

Requires Python 3.9+ on PATH. The script generates the icon, installs
PyInstaller, builds `dist\DualRouterDashboard.exe`, and installs a
Desktop shortcut.

## ▶ Run from source

```bat
python dual_router_gui.py
python dual_router_dashboard.py --check   :: console topology self-test
```

The GUI reuses the terminal dashboard (`dual_router_dashboard.py`) as its
networking core — pure Python standard library.

## 🧭 How it finds your routers

- **Indoor router** — your default gateway (verified by ping)
- **Outdoor (WISP) router** — first private traceroute hop behind it,
  ping-verified so ISP/CGNAT hops are never misidentified
- Both can be pinned manually in *⚙ Settings*

## 📁 Repository layout

```
dual_router_gui.py        desktop app (tkinter GUI, tools, access control)
dual_router_dashboard.py  networking core + terminal TUI
make_icon.py              icon generator (stdlib only)
build_desktop.bat         one-click build + install
install_shortcut.ps1      desktop/start-menu shortcut installer
installer.iss             Inno Setup script (optional uninstaller installer)
app_icon.ico / .png       generated icons
.github/workflows/        CI that builds the exe on every push
```

## 📄 License

MIT — do whatever you like, attribution appreciated.

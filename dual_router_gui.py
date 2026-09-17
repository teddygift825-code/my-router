#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║   DUAL-ROUTER DASHBOARD — DESKTOP EDITION (Windows)                  ║
╚══════════════════════════════════════════════════════════════════════╝

A native Windows GUI (tkinter — ships with Python, nothing to pip-install
at runtime) around the same networking core as the terminal dashboard.

  OUTDOOR router (WISP mode)   :  192.168.1.1
  INDOOR  router (LAN gateway) :  192.168.0.1

Features
    • live device table for the indoor LAN and the WAN-side leg
    • automatic topology discovery (indoor = default gateway,
      outdoor = first private traceroute hop behind it)
    • custom device names (aliases), stored per user
    • join/leave alerts with sound + pop-up toasts
    • built-in speed test (latency / download / upload)
    • traceroute viewer, continuous-ping window, TCP port scanner
    • CSV / JSON export of the current network snapshot
    • dark & light themes, settings persisted in %APPDATA%

Command line
    DualRouterDashboard.exe              start the GUI
    DualRouterDashboard.exe --check      console topology self-check, exit
    DualRouterDashboard.exe --smoke f    auto-quit self test → JSON file
    DualRouterDashboard.exe --version    print version and exit

Settings + aliases live in %APPDATA%\\DualRouterDashboard\\.
"""
import csv
import ipaddress
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import dual_router_dashboard as core

APP_NAME = "Dual-Router Dashboard"
APP_VERSION = "2.0.0"

COMMON_PORTS = (
    20, 21, 22, 23, 25, 53, 67, 68, 69, 80, 110, 111, 123, 135, 137, 138,
    139, 143, 161, 389, 443, 445, 465, 514, 515, 548, 587, 631, 636, 873,
    993, 995, 1080, 1433, 1723, 1883, 2049, 2181, 2375, 3128, 3260, 3306,
    3389, 4443, 5060, 5432, 5555, 5900, 5984, 6379, 8080, 8081, 8443,
    8888, 9000, 9100, 9200, 11211, 49152, 49153, 50000,
)
PORT_NAMES = {
    20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp",
    53: "dns", 67: "dhcp-s", 68: "dhcp-c", 69: "tftp", 80: "http",
    110: "pop3", 111: "rpcbind", 123: "ntp", 135: "ms-rpc",
    137: "netbios-ns", 138: "netbios-ds", 139: "netbios-ssn", 143: "imap",
    161: "snmp", 389: "ldap", 443: "https", 445: "smb", 465: "smtps",
    514: "syslog", 515: "printer", 548: "afp", 587: "submission",
    631: "ipp", 636: "ldaps", 873: "rsync", 993: "imaps", 995: "pop3s",
    1080: "socks", 1433: "mssql", 1723: "pptp", 1883: "mqtt", 2049: "nfs",
    2181: "zookeeper", 2375: "docker", 3128: "squid", 3260: "iscsi",
    3306: "mysql", 3389: "rdp", 4443: "https-alt", 5060: "sip",
    5432: "postgres", 5555: "adb", 5900: "vnc", 5984: "couchdb",
    6379: "redis", 8080: "http-alt", 8081: "http-alt2", 8443: "https-alt3",
    8888: "http-alt4", 9000: "sonar", 9100: "jetdirect", 9200: "elastic",
    11211: "memcached", 49152: "ms-rpc", 49153: "ms-rpc", 50000: "upnp",
}

SPEED_URLS = (
    "https://speed.cloudflare.com/__down?bytes=52428800",
    "https://proof.ovh.net/files/100Mb.dat",
    "http://ipv4.download.thinkbroadband.com/50MB.zip",
)
SPEED_UPLOAD_URL = "https://speed.cloudflare.com/__up"

try:
    import winsound
except ImportError:                                    # non-Windows
    winsound = None


def app_data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "DualRouterDashboard")
    os.makedirs(path, exist_ok=True)
    return path


def _hex_rgb(value):
    """'#rrggbb' → (r, g, b) ints."""
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def norm_mac(value):
    """Lower-case colon-separated MAC, or '' when empty/invalid."""
    value = value.strip().replace("-", ":").lower()
    return value if MAC_RE.match(value) else ""


DEFAULTS = {
    "refresh_ms": 2000,      # latency probe cadence
    "autoscan_min": 5,       # 0 = manual scans only
    "sound": True,
    "toasts": True,
    "theme": "sunset",
    "indoor_ip": "",         # blank = auto-detect via routing table
    "outdoor_ip": "",        # blank = auto-detect via traceroute
    "internet_probe": "8.8.8.8",
    "ping_wait": 1,
    "offline_keep_min": 10,
    "sweep_batch": 96,
    "max_devices": 10,       # how many devices may use the internet
}


class Config:
    """JSON-persisted settings + per-device alias names."""

    def __init__(self):
        self.dir = app_data_dir()
        self.path = os.path.join(self.dir, "config.json")
        self.alias_path = os.path.join(self.dir, "aliases.json")
        self.devices_path = os.path.join(self.dir, "devices.json")
        self.values = dict(DEFAULTS)
        self.aliases = {}
        self.devices = []            # manual registrations
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.values.update({k: data[k] for k in DEFAULTS if k in data})
        except (OSError, ValueError):
            pass
        try:
            with open(self.alias_path, encoding="utf-8") as fh:
                self.aliases = {str(k): str(v)
                                for k, v in json.load(fh).items()}
        except (OSError, ValueError):
            pass
        try:
            with open(self.devices_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                self.devices = [d for d in raw
                                if isinstance(d, dict) and d.get("ip")]
        except (OSError, ValueError):
            pass

    def save_devices(self):
        try:
            with open(self.devices_path, "w", encoding="utf-8") as fh:
                json.dump(self.devices, fh, indent=2)
        except OSError:
            pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.values, fh, indent=2)
        except OSError:
            pass

    def save_aliases(self):
        try:
            with open(self.alias_path, "w", encoding="utf-8") as fh:
                json.dump(self.aliases, fh, indent=2, sort_keys=True)
        except OSError:
            pass

    def __getitem__(self, key):
        return self.values.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.values[key] = value


# ────────────────────────── NETWORK MODEL (GUI-side) ─────────────────────
class NetworkModel:
    """Background state: topology, devices, latency history, scan waves.

    Reuses the platform layer of dual_router_dashboard (ping/sweep/arp/
    traceroute/discovery) but keeps its own device bookkeeping because the
    GUI needs stable dict objects it can read from the Tk thread.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.out_q = queue.Queue()          # (kind, payload) events → UI
        self.running = True
        self.scanning = False
        self.scan_pct = 0
        self.indoor_ip = cfg["indoor_ip"] or core.get_gateway()
        self.lan_subnet = core.subnet_of(self.indoor_ip)
        self.outdoor_ip = cfg["outdoor_ip"] or None
        self.wan_subnet = core.subnet_of(self.outdoor_ip) if self.outdoor_ip \
            else None
        self.local_ip = core.local_ip()
        self.routers = {
            "indoor":  {"name": "INDOOR ROUTER", "mode": "LAN gateway",
                        "ip": self.indoor_ip, "lat": None, "online": True},
            "outdoor": {"name": "OUTDOOR ROUTER", "mode": "WISP uplink",
                        "ip": self.outdoor_ip, "lat": None, "online": False},
            "internet": {"name": "INTERNET",
                         "mode": cfg["internet_probe"] + " probe",
                         "ip": cfg["internet_probe"], "lat": None,
                         "online": True},
        }
        self.zones = {
            "LAN": {"label": "Indoor LAN", "subnet": self.lan_subnet,
                    "devices": {}, "sorted": []},
            "WAN": {"label": "WAN side (between routers)",
                    "subnet": self.wan_subnet, "devices": {}, "sorted": []},
        }
        self.lat_hist = {f"R:{k}": deque(maxlen=core.MAX_HIST)
                         for k in self.routers}
        self.host_cache = {}
        self.offline_keep = max(60, cfg["offline_keep_min"] * 60)
        self.next_autoscan = time.time() + max(1, cfg["autoscan_min"]) * 60
        self._scan_thread = None

    # ── helpers ──
    def say(self, text, level="info"):
        self.out_q.put(("msg", (text, level)))

    def alias(self, ip):
        return self.cfg.aliases.get(ip, "")

    def registered_ips(self):
        """IPs manually registered for internet access."""
        return {reg.get("ip", "") for reg in self.cfg.devices
                if reg.get("ip")}

    def quota_counts(self):
        """(online registered, registered total, max allowed)."""
        registered = [reg for reg in self.cfg.devices if reg.get("ip")]
        online = 0
        with self.lock:
            lan = self.zones["LAN"]["devices"]
            for reg in registered:
                dev = lan.get(reg["ip"])
                if dev and dev.get("online"):
                    online += 1
        return online, len(registered), int(self.cfg["max_devices"])

    def upsert_registered_device(self, ip, mac, name):
        """Show a freshly registered device in the LAN list right away."""
        with self.lock:
            dev = self.zones["LAN"]["devices"].get(ip)
            if dev is None:
                return
            if name:
                dev["reg_name"] = name
            if mac and not dev.get("mac"):
                dev["mac"] = mac
            dev["role"] = "★ registered"

    def remove_registered_device(self, ip):
        """Drop the ★ badge; unregistered devices vanish from the list."""
        with self.lock:
            dev = self.zones["LAN"]["devices"].get(ip)
            if dev is None:
                return
            if dev.get("role") == "★ registered" and \
                    dev.get("seen") in ("—", ""):
                self.zones["LAN"]["devices"].pop(ip, None)
            else:
                dev.pop("role", None)

    def all_devices(self):
        with self.lock:
            return [(zn, dict(dev))
                    for zn in ("LAN", "WAN")
                    for dev in self.zones[zn]["devices"].values()]

    # ── discovery ──
    def ensure_outdoor(self):
        if self.outdoor_ip:
            return True
        core.OUTDOOR_ROUTER_IP = self.cfg["outdoor_ip"] or "192.168.1.1"
        outdoor, how = core.discover_outdoor(self.indoor_ip, self.lan_subnet)
        if not outdoor or core.subnet_of(outdoor) == self.lan_subnet:
            self.say("Outdoor router not found yet — will retry", "warn")
            return False
        self.outdoor_ip = outdoor
        self.wan_subnet = core.subnet_of(outdoor)
        with self.lock:
            router = self.routers["outdoor"]
            router["ip"] = outdoor
            router["online"] = False
            self.zones["WAN"]["subnet"] = self.wan_subnet
            self.zones["WAN"]["label"] = (f"WAN side {self.wan_subnet}0/24"
                                          " (between routers)")
        self.say(f"Outdoor router located at {outdoor} ({how})")
        return True

    # ── scanning ──
    def full_scan(self):
        if self.scanning:
            return
        self._scan_thread = threading.Thread(target=self._scan_worker,
                                             daemon=True)
        self._scan_thread.start()
        self.out_q.put(("scan_state", True))

    def _scan_worker(self):
        self.scanning = True
        self.scan_pct = 0
        try:
            self.ensure_outdoor()
            jobs = [(zn, zone["subnet"]) for zn, zone in self.zones.items()
                    if zone["subnet"]]
            total = max(1, len(jobs) * 254)
            done = [0]

            def tick():
                done[0] += 1
                self.scan_pct = int(done[0] / total * 100)

            for zn, subnet in jobs:
                must = [self.indoor_ip]
                if zn == "WAN" and self.outdoor_ip:
                    must.append(self.outdoor_ip)
                live = core.sweep(subnet, must, tick,
                                  batch=self.cfg["sweep_batch"],
                                  wait=self.cfg["ping_wait"])
                self._merge_zone(zn, live)
            with self.lock:
                count = sum(len(z["devices"]) for z in self.zones.values())
            self.say(f"Scan complete — {count} devices visible")
            self.out_q.put(("scan_done", count))
            self.beep()
        except Exception as exc:                       # pragma: no cover
            self.say(f"Scan error: {exc}", "error")
        finally:
            self.scanning = False
            self.out_q.put(("scan_state", False))

    def _merge_zone(self, zname, live):
        arps = core.arp_table()
        now = time.time()
        need_dns = []
        with self.lock:
            previous = self.zones[zname]["devices"]
            devices = {}
            for ip in live:
                old = previous.get(ip, {})
                mac = arps.get(ip) or old.get("mac", "")
                host = self._cached_host(ip)
                if host is None:
                    host = old.get("host", "")
                    need_dns.append(ip)
                devices[ip] = {
                    "ip": ip, "mac": mac, "host": host,
                    "vendor": core.vendor_hint(mac) or old.get("vendor", ""),
                    "lat": old.get("lat"), "online": True,
                    "seen": time.strftime("%H:%M:%S"),
                    "first_seen": old.get("first_seen")
                                  or time.strftime("%H:%M:%S"),
                }
            for ip, old in previous.items():
                if ip in devices:
                    continue
                if now - old.get("up_ts", 0) > self.offline_keep:
                    continue
                ghost = dict(old)
                ghost.update(online=False, lat=None)
                devices[ip] = ghost
            if zname == "LAN":
                for reg in self.cfg.devices:
                    ip = reg.get("ip", "")
                    if not ip or core.subnet_of(ip) != self.lan_subnet:
                        continue
                    name = reg.get("name", "")
                    mac = reg.get("mac", "")
                    if ip in devices:
                        dev = devices[ip]
                        if name:
                            dev.setdefault("reg_name", name)
                        if mac and not dev.get("mac"):
                            dev["mac"] = mac
                        dev["role"] = "★ registered"
                    else:
                        devices[ip] = {
                            "ip": ip, "mac": mac,
                            "host": reg.get("name", ""),
                            "vendor": core.vendor_hint(mac),
                            "lat": None, "online": False,
                            "seen": "—", "first_seen": "—",
                            "role": "★ registered",
                        }
            self.zones[zname]["devices"] = devices
            self.zones[zname]["sorted"] = sorted(devices,
                                                 key=core.ip_sort_key)
        self._resolve_hosts(need_dns)

    def _cached_host(self, ip):
        entry = self.host_cache.get(ip)
        if not entry:
            return None
        name, when = entry
        if name:
            return name
        if time.time() - when < core.HOST_CACHE_TTL:
            return ""
        return None

    def _resolve_hosts(self, ips):
        for ip in ips[:80]:
            name = core.resolve_host(ip)
            self.host_cache[ip] = (name, time.time())
            if name:
                with self.lock:
                    for zone in self.zones.values():
                        dev = zone["devices"].get(ip)
                        if dev and not dev.get("host"):
                            dev["host"] = name
        if ips:
            self.out_q.put(("tick", None))

    # ── latency loop ──
    def latency_loop(self):
        pool = ThreadPoolExecutor(max_workers=24)
        try:
            while self.running:
                for key in ("indoor", "outdoor", "internet"):
                    router = self.routers[key]
                    if not router["ip"]:
                        router["online"] = False
                        continue
                    self._apply_router(key,
                                       core.ping_ms(router["ip"], 1, 1))
                with self.lock:
                    targets = [(zn, ip)
                               for zn in ("LAN", "WAN")
                               for ip in self.zones[zn]["devices"]]
                if targets:
                    results = list(pool.map(
                        lambda t: core.ping_ms(t[1], 1, 1), targets))
                    for (zn, ip), ms in zip(targets, results):
                        self._apply_device(zn, ip, ms)
                self.out_q.put(("tick", None))
                time.sleep(max(0.5, self.cfg["refresh_ms"] / 1000.0))
        finally:
            pool.shutdown(wait=False)

    def _hist(self, key):
        return self.lat_hist.setdefault(key, deque(maxlen=core.MAX_HIST))

    def _apply_router(self, key, ms):
        router = self.routers[key]
        was_up = router["online"]
        router["lat"], router["online"] = ms, ms is not None
        self._hist("R:" + key).append(ms if ms is not None else 0)
        if was_up and not router["online"] and router["ip"]:
            self.say(f"{router['name']} ({router['ip']}) UNREACHABLE",
                     "error")
            self.out_q.put(("alert", (router["name"], router["ip"],
                                      "down")))
            self.beep(freq=440)
        elif not was_up and router["online"]:
            self.say(f"{router['name']} ({router['ip']}) back online",
                     "good")
            self.out_q.put(("alert", (router["name"], router["ip"],
                                      "up")))
            self.beep(freq=880)

    def _apply_device(self, zname, ip, ms):
        with self.lock:
            dev = self.zones[zname]["devices"].get(ip)
            if not dev:
                return
            was_up = dev["online"]
            dev["lat"] = ms
            if ms is not None:
                dev["online"] = True
                dev["up_ts"] = time.time()
                dev["seen"] = time.strftime("%H:%M:%S")
            elif was_up:
                dev["online"] = False
        self._hist(ip).append(ms if ms is not None else 0)
        if was_up and ms is None:
            label = self.alias(ip) or dev.get("host") or ip
            self.out_q.put(("alert", (label, ip, "down")))
        elif not was_up and ms is not None:
            label = self.alias(ip) or dev.get("host") or ip
            self.out_q.put(("alert", (label, ip, "up")))

    def beep(self, freq=1200):
        if self.cfg["sound"] and winsound:
            try:
                winsound.Beep(int(freq), 140)
            except Exception:
                pass

    def stop(self):
        self.running = False


# ───────────────────────────── TOOL DIALOGS ──────────────────────────────
class ToolDialog(tk.Toplevel):
    """Base window with a dark-styled text area + close binding."""

    def __init__(self, app, title, geometry="760x520"):
        super().__init__(app)
        self.app = app
        self.title(f"{APP_NAME} — {title}")
        self.geometry(geometry)
        self.configure(bg=app.theme["bg"])
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.transient(app)

    def add_text(self, height=None):
        frame = tk.Frame(self, bg=self.app.theme["panel"])
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        widget = tk.Text(frame, wrap="none", relief="flat",
                         bg=self.app.theme["entry"],
                         fg=self.app.theme["fg"],
                         insertbackground=self.app.theme["fg"],
                         font=("Consolas", 10), state="disabled")
        if height:
            widget.configure(height=height)
        ysb = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        widget.pack(fill="both", expand=True)
        return widget

    def write(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")


class TracerouteDialog(ToolDialog):
    """Live traceroute with per-hop ping verification."""

    def __init__(self, app, target):
        super().__init__(app, f"Traceroute → {target}")
        self.target = target
        self.text = self.add_text()
        row = tk.Frame(self, bg=self.app.theme["bg"])
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right")
        self.after(60, self.run)

    def run(self):
        self.text.configure(state="normal")
        self.text.insert("end", f"Tracing route to {self.target}…\n\n")
        self.text.configure(state="disabled")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        hops = core.traceroute_hops(self.target, max_hops=15, wait=2)
        lines = [f"Tracing route to {self.target}\n"]
        for idx, hop in enumerate(hops, 1):
            if not hop:
                lines.append(f"  {idx:>2}   *  *  *   (no answer)\n")
                continue
            ms = core.ping_ms(hop, 2, 1)
            rtt = f"{ms:6.1f} ms" if ms is not None else "  —  "
            name = core.resolve_host(hop)
            lines.append(f"  {idx:>2}   {hop:<16} {rtt}  {name}\n")
        if self.winfo_exists():
            self.app.ui_queue.put(("tool_text", (self, self.text,
                                                 "".join(lines))))


class PingMonitorDialog(ToolDialog):
    """Continuous ping with live min/avg/max/loss statistics."""

    def __init__(self, app, target):
        super().__init__(app, f"Ping monitor → {target}", "700x470")
        self.target = target
        self.samples = []
        self.sent = 0
        self.lost = 0
        self.running = True
        top = tk.Frame(self, bg=self.app.theme["bg"])
        top.pack(fill="x", padx=8, pady=8)
        self.stat = tk.Label(top, text="starting…", anchor="w",
                             bg=self.app.theme["bg"],
                             fg=self.app.theme["fg"],
                             font=("Consolas", 11, "bold"))
        self.stat.pack(side="left")
        ttk.Button(top, text="Stop", command=self.stop).pack(side="right")
        ttk.Button(top, text="Resume", command=self.resume).pack(
            side="right", padx=4)
        self.text = self.add_text()
        self.protocol("WM_DELETE_WINDOW", self._close)
        threading.Thread(target=self._worker, daemon=True).start()

    def resume(self):
        self.running = True

    def stop(self):
        self.running = False

    def _close(self):
        self.running = False
        self.destroy()

    def _worker(self):
        while self.running and self.winfo_exists():
            ms = core.ping_ms(self.target, 1, 1)
            self.sent += 1
            if ms is None:
                self.lost += 1
                line = f"{time.strftime('%H:%M:%S')}  ———  timeout\n"
            else:
                self.samples.append(ms)
                line = (f"{time.strftime('%H:%M:%S')}  {ms:7.1f} ms"
                        f"   seq {self.sent}\n")
            if self.winfo_exists():
                self.app.ui_queue.put(("tool_append", (self.text, line)))
            time.sleep(0.7)
        if self.winfo_exists():
            self.app.ui_queue.put(("ping_stats", self))


class PortScanDialog(ToolDialog):
    """TCP connect scan of the common ports on a device."""

    def __init__(self, app, target):
        super().__init__(app, f"Port scan → {target}", "640x560")
        self.target = target
        self.text = self.add_text()
        row = tk.Frame(self, bg=self.app.theme["bg"])
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="Close", command=self.destroy).pack(side="right")
        self.after(60, self.run)

    def run(self):
        self.text.configure(state="normal")
        self.text.insert("end",
                         f"Scanning {len(COMMON_PORTS)} common TCP ports "
                         f"on {self.target}…\n\n")
        self.text.configure(state="disabled")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        def probe(port):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.8)
            try:
                return port if sock.connect_ex(
                    (self.target, port)) == 0 else None
            finally:
                sock.close()

        open_ports = []
        with ThreadPoolExecutor(max_workers=64) as pool:
            for port in pool.map(probe, COMMON_PORTS):
                if port:
                    open_ports.append(port)
        lines = [f"Open TCP ports on {self.target}: "
                 f"{len(open_ports)}/{len(COMMON_PORTS)}\n\n"]
        if not open_ports:
            lines.append("  (none of the common ports answered)\n")
        for port in open_ports:
            service = PORT_NAMES.get(port, "?")
            lines.append(f"  {port:>6}/tcp   OPEN   {service}\n")
        if self.winfo_exists():
            self.app.ui_queue.put(("tool_text", (self, self.text,
                                                 "".join(lines))))


class SpeedTestDialog(ToolDialog):
    """Latency / download / upload meter (urllib, no external deps)."""

    def __init__(self, app):
        super().__init__(app, "Speed test", "620x420")
        self.results = {}
        self.busy = False
        self._cancel = False
        body = tk.Frame(self, bg=self.app.theme["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=12)
        self.rows = {}
        for key, label in (("ping", "Latency (1.1.1.1)"),
                           ("down", "Download"),
                           ("up", "Upload")):
            frame = tk.Frame(body, bg=self.app.theme["bg"])
            frame.pack(fill="x", pady=6)
            name = tk.Label(frame, text=label, width=18, anchor="w",
                            bg=self.app.theme["bg"],
                            fg=self.app.theme["dim"])
            name.pack(side="left")
            value = tk.Label(frame, text="—", anchor="w", width=22,
                             bg=self.app.theme["bg"],
                             fg=self.app.theme["fg"],
                             font=("Consolas", 13, "bold"))
            value.pack(side="left")
            bar = ttk.Progressbar(frame, length=200, maximum=100)
            bar.pack(side="left", padx=10)
            self.rows[key] = (value, bar)
        self.note = tk.Label(body, text="", anchor="w",
                             bg=self.app.theme["bg"],
                             fg=self.app.theme["dim"], wraplength=560,
                             justify="left")
        self.note.pack(fill="x", pady=(10, 4))
        row = tk.Frame(body, bg=self.app.theme["bg"])
        row.pack(fill="x", pady=(8, 0))
        self.start_btn = ttk.Button(row, text="Start test",
                                    command=self.start)
        self.start_btn.pack(side="right")
        ttk.Button(row, text="Close", command=self.destroy).pack(
            side="right", padx=6)

    def start(self):
        if self.busy:
            return
        self.busy = True
        self._cancel = False
        self.start_btn.state(["disabled"])
        threading.Thread(target=self._worker, daemon=True).start()

    def _progress(self, key, fraction, text=None):
        value, bar = self.rows[key]
        if self.winfo_exists():
            self.app.ui_queue.put(("speed_ui", (
                self, value, bar, fraction * 100.0, text)))

    def _note(self, text):
        if self.winfo_exists():
            self.app.ui_queue.put(("speed_note", (self, text)))

    def _worker(self):
        try:
            self._test_latency()
            if self._cancel:
                return
            self._test_download()
            if self._cancel:
                return
            self._test_upload()
            self._note("Speed test finished.")
        except Exception as exc:
            self._note(f"Speed test problem: {exc}")
        finally:
            self.busy = False
            if self.winfo_exists():
                self.app.ui_queue.put(("speed_done", self))

    def _test_latency(self):
        self._note("Measuring latency to 1.1.1.1 (10 probes)…")
        samples = []
        for i in range(10):
            if self._cancel:
                return
            ms = core.ping_ms("1.1.1.1", 1, 1)
            if ms is not None:
                samples.append(ms)
            self._progress("ping", (i + 1) / 10)
        if samples:
            avg = sum(samples) / len(samples)
            jitter = (sum(abs(a - b) for a, b in
                          zip(samples, samples[1:])) /
                      max(1, len(samples) - 1))
            self.results["ping"] = avg
            self.results["jitter"] = jitter
            self.results["loss"] = 1 - len(samples) / 10
            self._progress("ping", 100,
                           f"{avg:.1f} ms avg · jitter {jitter:.1f} ms")
        else:
            self.results["ping"] = None
            self._progress("ping", 100, "no answer")

    def _download_once(self, url, limit_seconds):
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 DRD-SpeedTest"})
        start = time.perf_counter()
        total = 0
        with urllib.request.urlopen(req, timeout=20) as resp:
            while True:
                if self._cancel or \
                        time.perf_counter() - start > limit_seconds:
                    break
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                self._progress("down",
                               min(1.0, total / (25 * 1024 * 1024)))
        elapsed = time.perf_counter() - start
        return total, elapsed

    def _test_download(self):
        self._note("Testing download…")
        mbps, used = None, None
        for url in SPEED_URLS:
            try:
                total, elapsed = self._download_once(url, 10.0)
            except Exception as exc:
                self._note(f"download via {url.split('/')[2]} "
                           f"failed: {exc}")
                continue
            if total > 512 * 1024 and elapsed > 0.2:
                mbps = total * 8 / elapsed / 1e6
                used = url
                break
        self.results["down"] = mbps
        if mbps is not None:
            self._progress("down", 100, f"{mbps:6.2f} Mbit/s  "
                                        f"({used.split('/')[2]})")
        else:
            self._progress("down", 100, "unavailable (blocked?)")

    def _test_upload(self):
        self._note("Testing upload (random 4 MB to Cloudflare)…")
        payload = os.urandom(4 * 1024 * 1024)
        try:
            req = urllib.request.Request(
                SPEED_UPLOAD_URL, data=payload, method="POST",
                headers={"User-Agent": "Mozilla/5.0 DRD-SpeedTest",
                         "Content-Type": "application/octet-stream"})
            start = time.perf_counter()
            urllib.request.urlopen(req, timeout=45).read(64)
            elapsed = time.perf_counter() - start
            mbps = len(payload) * 8 / elapsed / 1e6
            self.results["up"] = mbps
            self._progress("up", 100, f"{mbps:6.2f} Mbit/s")
        except Exception as exc:
            self.results["up"] = None
            self._progress("up", 100,
                           f"unavailable ({exc.__class__.__name__})")

    def finish(self):
        self.start_btn.state(["!disabled"])


class AccessControlDialog(ToolDialog):
    """Internet-sharing quota + manual device registration (IP or MAC)."""

    def __init__(self, app):
        super().__init__(app, "Access control & quota", "760x640")
        self.resizable(False, False)
        t = app.theme
        outer = tk.Frame(self, bg=t["bg"])
        outer.pack(fill="both", expand=True)

        # ── quota card ──
        card = tk.Frame(outer, bg=t["panel"],
                        highlightbackground=t["border"],
                        highlightthickness=1)
        card.pack(fill="x", padx=12, pady=(12, 8))
        tk.Label(card, text="INTERNET SHARING LIMIT",
                 bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 10, "bold")).pack(
                     anchor="w", padx=14, pady=(10, 0))
        mid = tk.Frame(card, bg=t["panel"])
        mid.pack(fill="x", padx=14, pady=4)
        self.gauge = tk.Canvas(mid, width=240, height=150, bg=t["panel"],
                               highlightthickness=0)
        self.gauge.pack(side="left")
        side = tk.Frame(mid, bg=t["panel"])
        side.pack(side="left", padx=18, pady=6)
        lim_row = tk.Frame(side, bg=t["panel"])
        lim_row.pack(anchor="w")
        tk.Label(lim_row, text="Devices allowed on internet:",
                 bg=t["panel"], fg=t["fg"]).pack(side="left")
        self.limit_var = tk.IntVar(value=max(1, int(app.cfg["max_devices"])))
        ttk.Spinbox(lim_row, from_=1, to=64, textvariable=self.limit_var,
                    width=5).pack(side="left", padx=8)
        ttk.Button(lim_row, text="Apply", command=self.apply_limit).pack(
            side="left")
        self.quota_note = tk.Label(side, text="", bg=t["panel"],
                                   fg=t["dim"], justify="left", anchor="w",
                                   font=("Consolas", 9))
        self.quota_note.pack(anchor="w", pady=(10, 0))
        self._ticker = None
        self.build_registration_card(outer)
        self.refresh_registrations()

    def apply_limit(self):
        try:
            limit = max(1, min(64, int(self.limit_var.get())))
        except (ValueError, tk.TclError):
            return
        self.app.cfg["max_devices"] = limit
        self.app.cfg.save()
        self.app.say(f"Internet sharing limit set to {limit} devices")
        self.refresh_registrations()

    def draw_gauge(self, online, registered, limit):
        cv = self.gauge
        t = self.app.theme
        cv.delete("all")
        cx, cy, r = 120, 134, 86
        ratio = (online / limit) if limit else 0
        color = (t["green"] if ratio <= 0.75
                 else t["amber"] if ratio <= 1.0 else t["red"])
        cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=180,
                      extent=180, style="arc", outline=t["border"],
                      width=16)
        cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=180,
                      extent=max(0.02, min(1.0, ratio)) * 180,
                      style="arc", outline=color, width=16)
        cv.create_text(cx, cy - 30, text=f"{online}/{limit}",
                       fill=t["fg"], font=("Segoe UI", 24, "bold"))
        cv.create_text(cx, cy + 6, text="registered devices online",
                       fill=t["dim"], font=("Segoe UI", 9))
        self.quota_note.configure(
            text=(f"registered: {registered}\n"
                  f"allowed:    {limit}\n"
                  f"over limit: {'YES' if online > limit else 'no'}"),
            fg=t["red"] if online > limit else t["dim"])

    # ── registration card ──
    def build_registration_card(self, outer):
        t = self.app.theme
        reg = tk.Frame(outer, bg=t["panel"],
                       highlightbackground=t["border"],
                       highlightthickness=1)
        reg.pack(fill="both", expand=True, padx=12, pady=8)
        tk.Label(reg, text="REGISTERED DEVICES  (by LAN IP or MAC)",
                 bg=t["panel"], fg=t["accent3"],
                 font=("Segoe UI", 10, "bold")).pack(
                     anchor="w", padx=14, pady=(10, 4))
        tree_wrap = tk.Frame(reg, bg=t["panel"])
        tree_wrap.pack(fill="both", expand=True, padx=14)
        self.reg_tree = ttk.Treeview(
            tree_wrap, columns=("ip", "mac", "name", "status"),
            show="headings", height=7)
        for col, head, width in (("ip", "LAN IP", 120),
                                 ("mac", "MAC address", 145),
                                 ("name", "Device name", 175),
                                 ("status", "Status", 110)):
            self.reg_tree.heading(col, text=head)
            self.reg_tree.column(col, width=width, anchor="w")
        rsb = ttk.Scrollbar(tree_wrap, orient="vertical",
                            command=self.reg_tree.yview)
        self.reg_tree.configure(yscrollcommand=rsb.set)
        rsb.pack(side="right", fill="y")
        self.reg_tree.pack(fill="both", expand=True)
        self.reg_tree.tag_configure("online",
                                    foreground=self.app.theme["green"])
        self.reg_tree.tag_configure("offline",
                                    foreground=self.app.theme["dim"])

        form = tk.Frame(reg, bg=t["panel"])
        form.pack(fill="x", padx=14, pady=(8, 4))
        self.name_var = tk.StringVar()
        subnet = self.app.model.lan_subnet
        self.ip_var = tk.StringVar(value=f"{subnet}0" if subnet else "")
        self.mac_var = tk.StringVar()
        for label, var, width in (("Name", self.name_var, 15),
                                  ("LAN IP", self.ip_var, 14),
                                  ("MAC (opt.)", self.mac_var, 16)):
            col = tk.Frame(form, bg=t["panel"])
            col.pack(side="left", padx=(0, 8))
            tk.Label(col, text=label, bg=t["panel"],
                     fg=t["dim"]).pack(anchor="w")
            ttk.Entry(col, textvariable=var, width=width).pack(anchor="w")
        btns = tk.Frame(form, bg=t["panel"])
        btns.pack(side="left", fill="y")
        ttk.Button(btns, text="➕ Add device", style="Accent.TButton",
                   command=self.add_device).pack(anchor="w", pady=(2, 4))
        ttk.Button(btns, text="🗑 Remove selected",
                   command=self.remove_selected).pack(anchor="w")
        self.form_msg = tk.Label(reg, text="", bg=t["panel"],
                                 fg=t["dim"], anchor="w")
        self.form_msg.pack(fill="x", padx=14, pady=(0, 10))

    def refresh_registrations(self):
        if not self.winfo_exists():
            return
        app = self.app
        online, registered, limit = app.model.quota_counts()
        self.draw_gauge(online, registered, limit)
        tree = self.reg_tree
        tree.delete(*tree.get_children())
        for reg in app.cfg.devices:
            ip = reg.get("ip", "")
            status, is_on = "—", False
            with app.model.lock:
                dev = app.model.zones["LAN"]["devices"].get(ip)
            if dev:
                is_on = bool(dev["online"])
                status = ("● online" if is_on else "○ offline")
                if not reg.get("mac") and dev.get("mac"):
                    reg["mac"] = dev["mac"]
            tree.insert("", "end", values=(
                ip, reg.get("mac", "") or "—",
                reg.get("name", "") or "—", status),
                tags=("online" if is_on else "offline",))
        self._ticker = self.after(1500, self.refresh_registrations)

    def _fail(self, text):
        self.form_msg.configure(text=f"⚠ {text}",
                                fg=self.app.theme["red"])

    def add_device(self):
        app = self.app
        name = self.name_var.get().strip()
        ip = self.ip_var.get().strip()
        mac = norm_mac(self.mac_var.get())
        if not name:
            self._fail("give the device a name")
            return
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            self._fail("LAN IP looks invalid (example 192.168.0.55)")
            return
        if app.model.lan_subnet and \
                core.subnet_of(ip) != app.model.lan_subnet:
            self._fail(f"IP must be inside {app.model.lan_subnet}0/24")
            return
        if mac and mac in {r.get("mac") for r in app.cfg.devices}:
            self._fail("that MAC is already registered")
            return
        if ip in {r.get("ip") for r in app.cfg.devices}:
            self._fail("that IP is already registered")
            return
        app.cfg.devices.append({"ip": ip, "mac": mac, "name": name})
        app.cfg.save_devices()
        if not app.cfg.aliases.get(ip):
            app.cfg.aliases[ip] = name
            app.cfg.save_aliases()
        app.model.upsert_registered_device(ip, mac, name)
        app.model.say(f"Registered {name} ({ip}) for internet access")
        self.name_var.set("")
        self.mac_var.set("")
        self.ip_var.set(ip.rsplit(".", 1)[0] + ".")
        self.form_msg.configure(text=f"✔ added {name} ({ip})",
                                fg=app.theme["green"])
        self.refresh_registrations()

    def remove_selected(self):
        app = self.app
        sel = self.reg_tree.selection()
        if not sel:
            self._fail("select a row to remove")
            return
        ip = self.reg_tree.item(sel[0], "values")[0]
        app.cfg.devices = [r for r in app.cfg.devices if r.get("ip") != ip]
        app.cfg.save_devices()
        app.model.remove_registered_device(ip)
        app.model.say(f"Removed registration for {ip}")
        self.form_msg.configure(text=f"✔ removed {ip}",
                                fg=app.theme["green"])
        self.refresh_registrations()

    def destroy(self):
        if getattr(self, "_ticker", None) is not None:
            try:
                self.after_cancel(self._ticker)
            except Exception:
                pass
            self._ticker = None
        super().destroy()


# ─────────────────────────────── GUI THEMES ──────────────────────────────
# Vibrant multi-colour palettes — no black anywhere.
THEMES = {
    "sunset": {   # deep violet → magenta → coral
        "bg": "#2a1a4a", "panel": "#3b2568", "panel2": "#33205c",
        "entry": "#42296f", "fg": "#ffe9f7", "dim": "#c9a6ec",
        "accent": "#ff5fa2", "accent2": "#ff8a5c", "violet": "#b06cff",
        "header": "#7b2ff7", "green": "#3ef0a0", "red": "#ff5470",
        "amber": "#ffd166", "orange": "#ff8a5c", "border": "#5a3b93",
        "sel": "#8b3ff0", "row_alt": "#38235f",
    },
    "ocean": {    # deep indigo → cyan → mint
        "bg": "#123a5c", "panel": "#17507c", "panel2": "#14466e",
        "entry": "#1a5a8a", "fg": "#e2f8ff", "dim": "#8fc7e8",
        "accent": "#00d4ff", "accent2": "#2affd5", "violet": "#7c6cff",
        "header": "#0e63b0", "green": "#2affd5", "red": "#ff6b7a",
        "amber": "#ffd166", "orange": "#ffa15c", "border": "#2e7bb0",
        "sel": "#0a84c8", "row_alt": "#164f7a",
    },
    "neon": {     # midnight purple → electric lime & pink pops
        "bg": "#241440", "panel": "#31195c", "panel2": "#2a164f",
        "entry": "#381e69", "fg": "#f4ffe0", "dim": "#b4a3e6",
        "accent": "#c6ff00", "accent2": "#ff44dd", "violet": "#8f5cff",
        "header": "#5b17c9", "green": "#7dff6b", "red": "#ff4d6d",
        "amber": "#ffe14d", "orange": "#ff9046", "border": "#4d2c8a",
        "sel": "#6d28d9", "row_alt": "#2d1754",
    },
    "candy": {    # bright light theme — bubblegum & sky
        "bg": "#fff0f6", "panel": "#ffffff", "panel2": "#ffe4f0",
        "entry": "#ffffff", "fg": "#472d54", "dim": "#a06a9c",
        "accent": "#e83e8c", "accent2": "#ff8f3d", "violet": "#8a4fd3",
        "header": "#ff5fa2", "green": "#12b886", "red": "#fa5252",
        "amber": "#f59f00", "orange": "#ff8f3d", "border": "#ffc2dd",
        "sel": "#ffd6e8", "row_alt": "#fff5fa",
    },
    "forest": {   # rich emerald & gold
        "bg": "#0f3d2e", "panel": "#15573f", "panel2": "#124a37",
        "entry": "#1a6a4d", "fg": "#eafff3", "dim": "#93d9b4",
        "accent": "#ffe066", "accent2": "#38d9a9", "violet": "#63e6be",
        "header": "#0b8457", "green": "#8ff05f", "red": "#ff6b6b",
        "amber": "#ffd43b", "orange": "#ff922b", "border": "#2b8a6b",
        "sel": "#099268", "row_alt": "#145239",
    },
}

# ──────────────────────────── MAIN APPLICATION ───────────────────────────
class App(tk.Tk):
    ZONES = ("LAN", "WAN")
    COLS = ("dot", "ip", "name", "host", "mac", "vendor", "role",
            "ping", "seen", "first")
    HEADS = ("", "IP address", "Name", "Hostname", "MAC", "Vendor", "Role",
             "Ping ms", "Last seen", "First seen")
    WIDS = (24, 112, 120, 130, 140, 90, 110, 62, 66, 66)

    def __init__(self, smoke_path=None):
        super().__init__()
        self.smoke_path = smoke_path
        self.exit_code = 0
        self.ui_queue = queue.Queue()
        self.cfg = Config()
        self.theme_name = self.cfg["theme"]
        self.theme = THEMES.get(self.theme_name,
                                THEMES["sunset"])
        self.model = NetworkModel(self.cfg)
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1280x800")
        self.minsize(1024, 660)
        icon_png = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "app_icon.png")
        if os.path.exists(icon_png):
            try:
                self._icon_img = tk.PhotoImage(file=icon_png)
                self.iconphoto(True, self._icon_img)
            except Exception:
                pass
        self._build_style()
        self._build_header()
        self._build_toolbar()
        self._build_main()
        self._build_statusbar()
        self.bind("<F5>", lambda e: self.model.full_scan())
        self.bind("<Control-e>", lambda e: self.export("csv"))
        self.bind("<Control-j>", lambda e: self.export("json"))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_ids = {}
        self._after_ids["drain"] = self.after(220, self._drain_queue)
        self._after_ids["refresh"] = self.after(480, self._refresh_tables)
        self.model.full_scan()
        if smoke_path:
            self.after(11000, self._smoke_finish)

    # ── style ──
    def _build_style(self):
        t = self.theme
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=t["bg"], foreground=t["fg"],
                        font=("Segoe UI", 10))
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        style.configure("Dim.TLabel", background=t["bg"],
                        foreground=t["dim"])
        style.configure("Head.TLabel", background=t["bg"],
                        foreground=t["fg"], font=("Segoe UI", 12, "bold"))
        style.configure("TButton", background=t["panel"],
                        foreground=t["fg"], borderwidth=0,
                        focusthickness=0, padding=(10, 5))
        style.map("TButton",
                  background=[("active", t["sel"]),
                              ("disabled", t["panel2"])],
                  foreground=[("disabled", t["dim"])])
        style.configure("Treeview", background=t["entry"],
                        foreground=t["fg"], fieldbackground=t["entry"],
                        rowheight=24, borderwidth=0)
        style.map("Treeview",
                  background=[("selected", t["sel"])],
                  foreground=[("selected", t["fg"])])
        style.configure("Treeview.Heading", background=t["panel2"],
                        foreground=t["dim"], relief="flat",
                        font=("Segoe UI", 9, "bold"))
        style.map("Treeview.Heading", background=[("active", t["panel"])])
        style.configure("TNotebook", background=t["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=t["panel"],
                        foreground=t["dim"], padding=(14, 7))
        style.map("TNotebook.Tab",
                  background=[("selected", t["sel"])],
                  foreground=[("selected", t["fg"])])
        style.configure("TProgressbar", troughcolor=t["panel2"],
                        borderwidth=0, background=t["accent"])
        style.configure("TCheckbutton", background=t["bg"],
                        foreground=t["fg"])
        style.map("TCheckbutton", background=[("active", t["bg"])])
        style.configure("TRadiobutton", background=t["bg"],
                        foreground=t["fg"])
        style.configure("TSpinbox", background=t["entry"],
                        foreground=t["fg"], fieldbackground=t["entry"],
                        buttonbackground=t["panel"], arrowcolor=t["fg"])
        style.configure("Vertical.TScrollbar", background=t["panel2"],
                        troughcolor=t["entry"], arrowcolor=t["dim"])
        style.configure("Horizontal.TScrollbar", background=t["panel2"],
                        troughcolor=t["entry"], arrowcolor=t["dim"])

    # ── header ──
    def _build_header(self):
        t = self.theme
        strip = tk.Canvas(self, height=5, highlightthickness=0, bg=t["bg"])
        strip.pack(fill="x")
        bar = tk.Frame(self, bg=t["header"], height=46)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        logo = tk.Label(bar, text="⌂", bg=t["header"], fg="#ffffff",
                        font=("Segoe UI", 18))
        logo.pack(side="left", padx=(12, 4))
        title = tk.Label(bar, text=APP_NAME, bg=t["header"], fg="#ffffff",
                         font=("Segoe UI", 12, "bold"))
        title.pack(side="left", pady=8)
        self.chips = {}
        for key, label in (("pc", "THIS PC"), ("indoor", "INDOOR"),
                           ("outdoor", "OUTDOOR"), ("internet", "INTERNET")):
            chip = tk.Label(bar, text=f"{label} ·", bg=t["header"],
                            fg="#ffffff", font=("Consolas", 10, "bold"))
            chip.pack(side="right", padx=10)
            self.chips[key] = chip
        self.quota_pill = tk.Label(bar, text="📶 — / —", bg=t["accent2"],
                                   fg="#2a1436",
                                   font=("Segoe UI", 10, "bold"),
                                   padx=12, pady=3)
        self.quota_pill.pack(side="right", padx=14)
        self.after(80, lambda: self._paint_gradient(
            strip, (t["header"], t["accent"], t["accent2"])))
        strip.bind("<Configure>", lambda e: self._paint_gradient(
            strip, (t["header"], t["accent"], t["accent2"])))

    def _paint_gradient(self, canvas, colors):
        """Fill a canvas with a horizontal multi-stop colour gradient."""
        canvas.delete("grad")
        width = canvas.winfo_width()
        height = max(1, canvas.winfo_height())
        if width < 2:
            return
        stops = len(colors) - 1
        rgbs = [_hex_rgb(c) for c in colors]
        for x in range(width):
            pos = x / max(1, width - 1) * stops
            i = min(stops - 1, int(pos))
            frac = pos - i
            c0, c1 = rgbs[i], rgbs[i + 1]
            rgb = tuple(int(c0[k] + (c1[k] - c0[k]) * frac)
                        for k in range(3))
            canvas.create_line(x, 0, x, height,
                               fill="#%02x%02x%02x" % rgb, tags="grad")

    # ── toolbar ──
    def _build_toolbar(self):
        bar = tk.Frame(self, bg=self.theme["bg"])
        bar.pack(fill="x", padx=8, pady=(8, 2))
        self.auto_var = tk.BooleanVar(value=self.cfg["autoscan_min"] > 0)
        ttk.Checkbutton(bar, text="Auto-scan", variable=self.auto_var,
                        command=self._apply_autoscan).pack(side="left")
        ttk.Button(bar, text="⟳ Rescan now",
                   command=self.model.full_scan).pack(side="left", padx=6)
        ttk.Button(bar, text="⚡ Speed test",
                   command=self.show_speed_test).pack(side="left")
        ttk.Button(bar, text="Ping monitor",
                   command=self.show_ping_monitor).pack(side="left", padx=6)
        ttk.Button(bar, text="Traceroute",
                   command=self.show_traceroute).pack(side="left")
        ttk.Button(bar, text="Port scan",
                   command=self.show_port_scan).pack(side="left", padx=6)
        ttk.Button(bar, text="ARP table",
                   command=self.show_arp).pack(side="left")
        ttk.Button(bar, text="🌐 Access & quota",
                   command=self.show_access).pack(side="left", padx=6)
        ttk.Button(bar, text="⬇ Export",
                   command=self._export_menu).pack(side="left", padx=6)
        ttk.Button(bar, text="Open router admin",
                   command=self._open_admin).pack(side="left")
        ttk.Button(bar, text="⚙ Settings",
                   command=self.show_settings).pack(side="right")
        ttk.Button(bar, text="About",
                   command=self.show_about).pack(side="right", padx=6)

    def _apply_autoscan(self):
        if self.auto_var.get() and int(self.cfg["autoscan_min"]) <= 0:
            self.cfg["autoscan_min"] = 5
            self.cfg.save()
        self.model.next_autoscan = time.time() + \
            int(self.cfg["autoscan_min"]) * 60 if self.auto_var.get() \
            else time.time() + 86400

    def _export_menu(self):
        menu = tk.Menu(self, tearoff=0,
                       bg=self.theme["panel"], fg=self.theme["fg"],
                       activebackground=self.theme["sel"],
                       activeforeground=self.theme["fg"])
        menu.add_command(label="Save snapshot as CSV…  (Ctrl+E)",
                         command=lambda: self.export("csv"))
        menu.add_command(label="Save snapshot as JSON…  (Ctrl+J)",
                         command=lambda: self.export("json"))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _open_admin(self):
        menu = tk.Menu(self, tearoff=0,
                       bg=self.theme["panel"], fg=self.theme["fg"],
                       activebackground=self.theme["sel"],
                       activeforeground=self.theme["fg"])
        for key, label in (("indoor", "Indoor router admin page"),
                           ("outdoor", "Outdoor router admin page")):
            menu.add_command(
                label=label,
                command=lambda k=key: self._browse_admin(k))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _browse_admin(self, key):
        ip = self.model.routers[key]["ip"]
        if ip:
            webbrowser.open(f"http://{ip}")

    # ── main area ──
    def _build_main(self):
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=4)

        # left: zone tables
        left = ttk.Frame(pane)
        pane.add(left, weight=3)
        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True)
        self.trees = {}
        self.row_ids = {}
        for zone in self.ZONES:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=f" {zone} ")
            tree = ttk.Treeview(frame, columns=self.COLS, show="headings",
                                selectmode="browse")
            for col, head, width in zip(self.COLS, self.HEADS, self.WIDS):
                tree.heading(col, text=head)
                tree.column(col, width=width, anchor="w", stretch=(col != "dot"))
            ysb = ttk.Scrollbar(frame, orient="vertical",
                                command=tree.yview)
            tree.configure(yscrollcommand=ysb.set)
            ysb.pack(side="right", fill="y")
            tree.pack(fill="both", expand=True)
            tree.tag_configure("up", foreground=self.theme["fg"])
            tree.tag_configure("down", foreground=self.theme["dim"])
            tree.tag_configure("router", foreground=self.theme["accent"])
            tree.tag_configure("pc", foreground=self.theme["green"])
            tree.bind("<<TreeviewSelect>>",
                      lambda e, z=zone: self._on_select(z))
            tree.bind("<Double-1>",
                      lambda e, z=zone: self._device_menu(e, z, popup=False))
            tree.bind("<Button-3>",
                      lambda e, z=zone: self._device_menu(e, z, popup=True))
            self.trees[zone] = tree
            self.row_ids[zone] = {}

        # right: detail + graph + log
        right = ttk.Frame(pane)
        pane.add(right, weight=1)
        card = tk.Frame(right, bg=self.theme["panel"],
                        highlightbackground=self.theme["border"],
                        highlightthickness=1)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=self.theme["panel"])
        inner.pack(fill="x", padx=10, pady=8)
        self.detail_title = tk.Label(inner, text="select a device",
                                     bg=self.theme["panel"],
                                     fg=self.theme["fg"],
                                     font=("Segoe UI", 12, "bold"),
                                     anchor="w")
        self.detail_title.pack(fill="x")
        self.detail_info = tk.Label(inner, text="", bg=self.theme["panel"],
                                    fg=self.theme["dim"], justify="left",
                                    anchor="w", font=("Consolas", 9))
        self.detail_info.pack(fill="x", pady=(4, 2))
        alias_row = tk.Frame(inner, bg=self.theme["panel"])
        alias_row.pack(fill="x", pady=(4, 0))
        tk.Label(alias_row, text="Name:", bg=self.theme["panel"],
                 fg=self.theme["dim"]).pack(side="left")
        self.alias_var = tk.StringVar()
        self.alias_entry = ttk.Entry(alias_row, textvariable=self.alias_var)
        self.alias_entry.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(alias_row, text="Save", width=6,
                   command=self.save_alias).pack(side="left")
        self.watch_var = tk.StringVar(value="Graph: selected device")
        watch = ttk.Combobox(inner, textvariable=self.watch_var,
                             state="readonly", values=(
                                 "Graph: selected device",
                                 "Graph: indoor router",
                                 "Graph: outdoor router",
                                 "Graph: internet ping"), width=24)
        watch.pack(fill="x", pady=(8, 2))
        self.graph = tk.Canvas(right, height=190, bg=self.theme["entry"],
                               highlightthickness=1,
                               highlightbackground=self.theme["border"])
        self.graph.pack(fill="x", padx=(0, 0), pady=(8, 0))
        log_head = tk.Label(right, text="EVENTS", bg=self.theme["bg"],
                            fg=self.theme["dim"],
                            font=("Segoe UI", 9, "bold"), anchor="w")
        log_head.pack(fill="x", pady=(10, 2))
        log_frame = tk.Frame(right, bg=self.theme["panel"])
        log_frame.pack(fill="both", expand=True)
        self.logbox = tk.Listbox(log_frame, bg=self.theme["entry"],
                                 fg=self.theme["fg"], relief="flat",
                                 font=("Consolas", 9), activestyle="none")
        lsb = ttk.Scrollbar(log_frame, orient="vertical",
                            command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.logbox.pack(fill="both", expand=True)

    def _build_statusbar(self):
        bar = tk.Frame(self, bg=self.theme["panel"], height=30)
        bar.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(bar, mode="determinate", length=170)
        self.progress.pack(side="left", padx=10, pady=5)
        self.counts = tk.Label(bar, text="starting…",
                               bg=self.theme["panel"],
                               fg=self.theme["dim"], anchor="w")
        self.counts.pack(side="left", padx=8)
        self.msgbar = tk.Label(bar, text="", bg=self.theme["panel"],
                               fg=self.theme["fg"], anchor="e")
        self.msgbar.pack(side="right", padx=10)

    # ── event queue (thread → UI) ──
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "msg":
                    text, level = payload
                    color = {"error": self.theme["red"],
                             "warn": self.theme["amber"],
                             "good": self.theme["green"]}.get(
                                 level, self.theme["dim"])
                    self.msgbar.configure(text=text, fg=color)
                elif kind == "log":
                    self._append_log(payload)
                elif kind == "alert":
                    label, ip, direction = payload
                    if direction == "down":
                        self._toast("Device left the network",
                                    f"{label}  ({ip}) is not responding",
                                    "warn")
                    else:
                        self._toast("Device online",
                                    f"{label}  ({ip}) answers again", "ok")
                elif kind == "scan_state":
                    self.progress.configure(
                        value=0, maximum=100 if payload else 1)
                elif kind == "tick":
                    self._refresh_tables()
                elif kind == "tool_text":
                    dialog, widget, text = payload
                    if dialog.winfo_exists():
                        dialog.write(widget, text)
                elif kind == "tool_append":
                    widget, line = payload
                    if widget.winfo_exists():
                        widget.configure(state="normal")
                        widget.insert("end", line)
                        widget.see("end")
                        widget.configure(state="disabled")
                elif kind == "ping_stats":
                    dialog = payload
                    if dialog.winfo_exists() and dialog.sent:
                        loss = dialog.lost / dialog.sent * 100
                        samples = dialog.samples
                        stats = (f"sent {dialog.sent} · lost "
                                 f"{dialog.lost} ({loss:.0f}%)")
                        if samples:
                            avg = sum(samples) / len(samples)
                            stats += (f" · min {min(samples):.1f}"
                                      f" · avg {avg:.1f}"
                                      f" · max {max(samples):.1f} ms")
                        dialog.stat.configure(text=stats)
                elif kind == "speed_ui":
                    dialog, value, bar, pct, text = payload
                    if dialog.winfo_exists():
                        bar["value"] = pct
                        if text:
                            value.configure(text=text)
                elif kind == "speed_note":
                    dialog, text = payload
                    if dialog.winfo_exists():
                        dialog.note.configure(text=text)
                elif kind == "speed_done":
                    if payload.winfo_exists():
                        payload.finish()
        except queue.Empty:
            pass
        self._after_ids["drain"] = self.after(220, self._drain_queue)

    def _append_log(self, text):
        self.logbox.insert("end", text)
        if self.logbox.size() > 400:
            self.logbox.delete(0, 50)
        self.logbox.see("end")

    def _snapshot(self):
        model = self.model
        with model.lock:
            data = {"pc": model.local_ip, "routers": model.routers,
                    "scanning": model.scanning, "scan_pct": model.scan_pct,
                    "next": model.next_autoscan}
            for zone in self.ZONES:
                zone_data = model.zones[zone]
                data[zone] = {
                    "label": zone_data["label"],
                    "rows": [dict(zone_data["devices"][ip])
                             for ip in zone_data["sorted"]],
                }
            return data

    # ── periodic UI refresh ──
    def _refresh_tables(self):
        try:
            snapshot = self._snapshot()
            self._update_chips(snapshot)
            for zone in self.ZONES:
                self._sync_tree(zone, snapshot[zone]["rows"])
            self._update_detail()
            self._draw_graph()
            self._update_statusbar(snapshot)
            if self.auto_var.get() and not self.model.scanning:
                if time.time() >= self.model.next_autoscan:
                    self.model.full_scan()
        except Exception:
            pass
        self._after_ids["refresh"] = self.after(480, self._refresh_tables)

    def _update_chips(self, snapshot):
        t = self.theme
        routers = snapshot["routers"]
        self.chips["pc"].configure(text=f"THIS PC {snapshot['pc'] or '?'}",
                                   fg=t["fg"])
        for key, name in (("indoor", "INDOOR"), ("outdoor", "OUTDOOR"),
                          ("internet", "INTERNET")):
            router = routers[key]
            ip = router["ip"]
            if not ip:
                text, color = f"{name} · not found", t["red"]
            elif router["online"]:
                lat = router["lat"]
                lat_text = f"{lat:.0f} ms" if lat is not None else "—"
                text, color = f"{name} {ip} {lat_text}", t["green"]
            else:
                text, color = f"{name} {ip} · down", t["red"]
            self.chips[key].configure(text=text, fg=color)

    def _sync_tree(self, zone, rows):
        tree = self.trees[zone]
        ids = self.row_ids[zone]
        want = set()
        for row in rows:
            ip = row["ip"]
            want.add(ip)
            dot = "●" if row["online"] else "○"
            ping = f"{row['lat']:.1f}" if row.get("lat") is not None \
                else "—"
            if ip == self.model.indoor_ip:
                tag = "router"
            elif ip == self.model.local_ip:
                tag = "pc"
            elif row["online"]:
                tag = "up"
            else:
                tag = "down"
            values = (dot, ip, self.cfg.aliases.get(ip, ""),
                      row.get("host", ""), row.get("mac", ""),
                      row.get("vendor", ""), row.get("role", ""),
                      ping, row.get("seen", ""), row.get("first_seen", ""))
            if ip in ids and tree.exists(ids[ip]):
                tree.item(ids[ip], values=values, tags=(tag,))
            else:
                ids[ip] = tree.insert("", "end", values=values,
                                      tags=(tag,))
        for ip, iid in list(ids.items()):
            if ip not in want and tree.exists(iid):
                tree.delete(iid)
                del ids[ip]

    def _update_statusbar(self, snapshot):
        live = sum(1 for z in self.ZONES for r in snapshot[z]["rows"]
                   if r["online"])
        total = sum(len(snapshot[z]["rows"]) for z in self.ZONES)
        if snapshot["scanning"]:
            self.progress.configure(maximum=100)
            self.progress["value"] = snapshot["scan_pct"]
            text = f"scanning… {snapshot['scan_pct']}%"
        else:
            self.progress["value"] = 0
            if self.auto_var.get():
                remain = max(0, int(snapshot["next"] - time.time()))
                text = (f"{live} live / {total} known · "
                        f"auto-scan in {remain // 60}:{remain % 60:02d}")
            else:
                text = f"{live} live / {total} known · auto-scan off"
        self.counts.configure(text=text)
        online, registered, limit = self.model.quota_counts()
        pill_text = f"📶 {online}/{limit} devices on internet"
        pill_color = (self.theme["green"] if online <= limit
                      else self.theme["red"])
        self.quota_pill.configure(
            text=pill_text, bg=pill_color,
            fg="#1c1030" if pill_color != self.theme["red"] else "#ffffff")

    # ── selection + detail ──
    def _selected_ip(self):
        zone = self.ZONES[self.notebook.index(
            self.notebook.select())] if self.notebook.tabs() else "LAN"
        tree = self.trees[zone]
        sel = tree.selection()
        if not sel:
            return None, None
        for ip, iid in self.row_ids[zone].items():
            if iid == sel[0]:
                return zone, ip
        return None, None

    def _on_select(self, zone):
        self._update_detail()

    def _update_detail(self):
        zone, ip = self._selected_ip()
        if not ip:
            self.detail_title.configure(text="select a device")
            self.detail_info.configure(text="")
            return
        with self.model.lock:
            dev = self.model.zones[zone]["devices"].get(ip, {})
        label = self.cfg.aliases.get(ip) or dev.get("host") or ip
        self.detail_title.configure(
            text=label + ("   ● online" if dev.get("online")
                          else "   ○ offline"))
        self.detail_title.configure(
            fg=self.theme["green"] if dev.get("online")
            else self.theme["dim"])
        history = self.model.lat_hist.get(ip)
        stats = ""
        if history:
            vals = [v for v in history if v]
            if vals:
                stats = (f"min {min(vals):.1f} · avg "
                         f"{sum(vals)/len(vals):.1f} · max "
                         f"{max(vals):.1f} ms   ")
        self.detail_info.configure(text=(
            f"zone      {self.model.zones[zone]['label']}\n"
            f"ip        {ip}\n"
            f"mac       {dev.get('mac', '') or '—'}\n"
            f"hostname  {dev.get('host', '') or '—'}\n"
            f"vendor    {dev.get('vendor', '') or '—'}\n"
            f"last ping {stats}last seen {dev.get('seen', '—')}"))
        if self.alias_entry.get() != self.cfg.aliases.get(ip, ""):
            self.alias_var.set(self.cfg.aliases.get(ip, ""))

    def save_alias(self):
        zone, ip = self._selected_ip()
        if not ip:
            return
        name = self.alias_var.get().strip()
        if name:
            self.cfg.aliases[ip] = name
        else:
            self.cfg.aliases.pop(ip, None)
        self.cfg.save_aliases()
        self._append_log(f"{time.strftime('%H:%M:%S')}  alias "
                         f"{'set' if name else 'cleared'} for {ip}\n")
        self._refresh_tables()

    # ── graph ──
    def _draw_graph(self):
        canvas = self.graph
        canvas.delete("all")
        width = canvas.winfo_width() or 300
        height = canvas.winfo_height() or 190
        t = self.theme
        watch = self.watch_var.get()
        if watch.endswith("indoor router"):
            history = self.model.lat_hist.get("R:indoor")
            title, color = "Indoor router latency", t["accent"]
        elif watch.endswith("outdoor router"):
            history = self.model.lat_hist.get("R:outdoor")
            title, color = "Outdoor router latency", t["orange"]
        elif watch.endswith("internet ping"):
            history = self.model.lat_hist.get("R:internet")
            title, color = "Internet latency", t["green"]
        else:
            zone, ip = self._selected_ip()
            history = self.model.lat_hist.get(ip) if ip else None
            title, color = "Selected device latency", t["accent"]
        if not history:
            canvas.create_text(width / 2, height / 2, fill=t["dim"],
                               text="no data yet", font=("Segoe UI", 10))
            return
        vals = list(history)
        peak = max(vals) or 1.0
        if peak < 10:
            peak = 10
        canvas.create_text(8, 6, anchor="nw", fill=t["dim"], text=title,
                           font=("Segoe UI", 9, "bold"))
        canvas.create_text(width - 8, 6, anchor="ne", fill=t["dim"],
                           text=f"peak {peak:.1f} ms",
                           font=("Consolas", 9))
        pad_bottom, pad_top = 18, 24
        span = len(vals) - 1 or 1
        step = (width - 16) / max(1, span)
        pts = []
        for idx, val in enumerate(vals):
            x = 8 + idx * step
            y = height - pad_bottom - (val / peak) * \
                (height - pad_bottom - pad_top)
            pts.extend((x, y))
        if len(pts) >= 4:
            canvas.create_line(*pts, fill=color, width=2, smooth=True)
            last_x, last_y = pts[-2], pts[-1]
            canvas.create_oval(last_x - 3, last_y - 3, last_x + 3,
                               last_y + 3, fill=color, outline="")
        canvas.create_line(8, height - pad_bottom, width - 8,
                           height - pad_bottom, fill=t["border"])
        canvas.create_text(8, height - 6, anchor="sw", fill=t["dim"],
                           text="now", font=("Segoe UI", 8))
        canvas.create_text(width - 8, height - 6, anchor="se", fill=t["dim"],
                           text=f"{len(vals)} samples", font=("Segoe UI", 8))

    # ── toasts ──
    def _toast(self, title, body, kind="ok"):
        if not self.cfg["toasts"]:
            return
        self._append_log(f"{time.strftime('%H:%M:%S')}  [{title}] "
                         f"{body}\n")
        toast = tk.Toplevel(self)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        color = {"warn": self.theme["amber"],
                 "error": self.theme["red"]}.get(kind,
                                                 self.theme["green"])
        toast.configure(bg=self.theme["panel"],
                        highlightbackground=color, highlightthickness=2)
        inner = tk.Frame(toast, bg=self.theme["panel"])
        inner.pack(padx=14, pady=10)
        tk.Label(inner, text=title, bg=self.theme["panel"], fg=color,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
        tk.Label(inner, text=body, bg=self.theme["panel"],
                 fg=self.theme["fg"], font=("Segoe UI", 9),
                 anchor="w", wraplength=330, justify="left").pack(
                     fill="x", pady=(2, 0))
        toast.update_idletasks()
        x = self.winfo_rootx() + self.winfo_width() - \
            toast.winfo_width() - 24
        y = self.winfo_rooty() + 56
        toast.geometry(f"+{x}+{y}")
        toast.after(4200, toast.destroy)

    # ── device actions ──
    def _device_menu(self, event, zone, popup=True):
        tree = self.trees[zone]
        iid = tree.identify_row(event.y) if popup else tree.focus()
        if not iid:
            return
        tree.selection_set(iid)
        ip = None
        for cand, row_iid in self.row_ids[zone].items():
            if row_iid == iid:
                ip = cand
                break
        if not ip:
            return
        menu = tk.Menu(self, tearoff=0, bg=self.theme["panel"],
                       fg=self.theme["fg"],
                       activebackground=self.theme["sel"],
                       activeforeground=self.theme["fg"])
        menu.add_command(label=f"Ping monitor → {ip}",
                         command=lambda: PingMonitorDialog(self, ip))
        menu.add_command(label=f"Traceroute → {ip}",
                         command=lambda: TracerouteDialog(self, ip))
        menu.add_command(label=f"Port scan → {ip}",
                         command=lambda: PortScanDialog(self, ip))
        menu.add_separator()
        if ip.startswith("192.168.") or ip.startswith("10.") or \
                ip.startswith("172."):
            menu.add_command(label="Open in browser",
                             command=lambda: webbrowser.open(
                                 f"http://{ip}"))
        menu.add_command(label="Copy IP address",
                         command=lambda: self._copy_text(ip))
        menu.add_command(label="Copy MAC address",
                         command=lambda: self._copy_text(
                             self._mac_of(zone, ip)))
        if self.cfg.aliases.get(ip):
            menu.add_command(label="Clear custom name",
                             command=lambda: self._set_alias(ip, ""))
        menu.tk_popup(event.x_root, event.y_root)

    def _mac_of(self, zone, ip):
        with self.model.lock:
            dev = self.model.zones[zone]["devices"].get(ip, {})
        return dev.get("mac", "")

    def _set_alias(self, ip, name):
        if name:
            self.cfg.aliases[ip] = name
        else:
            self.cfg.aliases.pop(ip, None)
        self.cfg.save_aliases()
        self._refresh_tables()

    def _copy_text(self, text):
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.msgbar.configure(text=f"copied: {text}",
                              fg=self.theme["dim"])

    # ── tool launchers ──
    def _target_or_selected(self):
        zone, ip = self._selected_ip()
        return ip

    def show_ping_monitor(self):
        ip = self._target_or_selected()
        if ip:
            PingMonitorDialog(self, ip)
        else:
            messagebox.showinfo(APP_NAME, "Select a device first.")

    def show_traceroute(self):
        ip = self._target_or_selected()
        if ip:
            TracerouteDialog(self, ip)
        else:
            messagebox.showinfo(APP_NAME, "Select a device first.")

    def show_port_scan(self):
        ip = self._target_or_selected()
        if ip:
            PortScanDialog(self, ip)
        else:
            messagebox.showinfo(APP_NAME, "Select a device first.")

    def show_access(self):
        if getattr(self, "_access_win", None) is not None and \
                self._access_win.winfo_exists():
            self._access_win.lift()
            return
        self._access_win = AccessControlDialog(self)

    def show_speed_test(self):
        SpeedTestDialog(self)

    def show_arp(self):
        dialog = ToolDialog(self, "ARP table", "640x520")
        text = dialog.add_text()
        rows = core.arp_table()
        lines = ["IP address        MAC address        \n",
                 "-" * 58 + "\n"]
        for ip in sorted(rows, key=core.ip_sort_key):
            lines.append(f"{ip:<18}{rows[ip]:<18}"
                         f"{core.vendor_hint(rows[ip]) or ''}\n")
        dialog.write(text, "".join(lines) or "(arp table empty)")

    # ── export ──
    def export(self, fmt):
        devices = self.model.all_devices()
        if not devices:
            messagebox.showinfo(APP_NAME, "Nothing to export yet — "
                                          "run a scan first.")
            return
        suffix = "csv" if fmt == "csv" else "json"
        path = filedialog.asksaveasfilename(
            defaultextension=f".{suffix}",
            initialfile=f"network_snapshot_{time.strftime('%Y%m%d_%H%M')}"
                        f".{suffix}",
            filetypes=[(f"{fmt.upper()} file", f"*.{suffix}"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            if fmt == "csv":
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["zone", "ip", "name", "hostname",
                                     "mac", "vendor", "online", "ping_ms",
                                     "last_seen", "first_seen"])
                    for zone, dev in devices:
                        writer.writerow([
                            zone, dev.get("ip", ""),
                            self.cfg.aliases.get(dev.get("ip", ""), ""),
                            dev.get("host", ""), dev.get("mac", ""),
                            dev.get("vendor", ""),
                            "yes" if dev.get("online") else "no",
                            "" if dev.get("lat") is None
                            else f"{dev['lat']:.1f}",
                            dev.get("seen", ""), dev.get("first_seen", "")])
            else:
                payload = {
                    "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "indoor_router": self.model.indoor_ip,
                    "outdoor_router": self.model.outdoor_ip,
                    "this_pc": self.model.local_ip,
                    "routers": {k: {"ip": r["ip"], "online": r["online"],
                                    "latency_ms": r["lat"]}
                                for k, r in self.model.routers.items()},
                    "devices": [
                        dict(dev, zone=zone,
                             name=self.cfg.aliases.get(dev.get("ip", ""),
                                                       ""))
                        for zone, dev in devices],
                }
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
            self.model.say(f"Exported {len(devices)} devices → {path}",
                           "good")
            self._append_log(f"{time.strftime('%H:%M:%S')}  exported "
                             f"{len(devices)} devices to {path}\n")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Export failed:\n{exc}")

    # ── settings ──
    def show_settings(self):
        if getattr(self, "_settings_win", None) is not None and \
                self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        win = tk.Toplevel(self)
        self._settings_win = win
        win.title(f"{APP_NAME} — Settings")
        win.configure(bg=self.theme["bg"])
        win.resizable(False, False)
        win.transient(self)
        body = tk.Frame(win, bg=self.theme["bg"])
        body.pack(fill="both", expand=True, padx=16, pady=12)

        def row(label):
            frame = tk.Frame(body, bg=self.theme["bg"])
            frame.pack(fill="x", pady=4)
            tk.Label(frame, text=label, width=24, anchor="w",
                     bg=self.theme["bg"], fg=self.theme["dim"]).pack(
                         side="left")
            return frame

        row("Refresh interval (ms):")
        spin = ttk.Spinbox(body, from_=500, to=10000, increment=250,
                           width=8)
        spin.delete(0, "end")
        spin.insert(0, str(self.cfg["refresh_ms"]))
        spin.pack(anchor="w")

        row("Auto-scan every (minutes, 0=off):")
        scan = ttk.Spinbox(body, from_=0, to=120, width=8)
        scan.delete(0, "end")
        scan.insert(0, str(self.cfg["autoscan_min"]))
        scan.pack(anchor="w")

        row("Indoor router IP (blank = auto):")
        indoor = ttk.Entry(body, width=18)
        indoor.insert(0, self.cfg["indoor_ip"])
        indoor.pack(anchor="w")

        row("Outdoor router IP (blank = auto):")
        outdoor = ttk.Entry(body, width=18)
        outdoor.insert(0, self.cfg["outdoor_ip"])
        outdoor.pack(anchor="w")

        row("Internet probe host:")
        probe = ttk.Entry(body, width=18)
        probe.insert(0, self.cfg["internet_probe"])
        probe.pack(anchor="w")

        row("Appearance:")
        theme_var = tk.StringVar(value=self.cfg["theme"])
        for value, label in (
                ("sunset", "Sunset — plum · magenta · coral"),
                ("ocean", "Ocean — indigo · cyan · mint"),
                ("neon", "Neon — violet · lime · hot pink"),
                ("candy", "Candy — light bubblegum & sky"),
                ("forest", "Forest — emerald & gold")):
            ttk.Radiobutton(body, text=label, value=value,
                            variable=theme_var).pack(anchor="w")

        row("Alerts:")
        sound_var = tk.BooleanVar(value=self.cfg["sound"])
        ttk.Checkbutton(body, text="Sound on router up/down",
                        variable=sound_var).pack(anchor="w")
        toast_var = tk.BooleanVar(value=self.cfg["toasts"])
        ttk.Checkbutton(body, text="Pop-up notifications",
                        variable=toast_var).pack(anchor="w")

        def save():
            try:
                self.cfg["refresh_ms"] = max(500, int(spin.get()))
                self.cfg["autoscan_min"] = max(0, int(scan.get()))
            except ValueError:
                pass
            self.cfg["indoor_ip"] = indoor.get().strip()
            self.cfg["outdoor_ip"] = outdoor.get().strip()
            self.cfg["internet_probe"] = probe.get().strip() or "8.8.8.8"
            self.cfg["theme"] = theme_var.get()
            self.cfg["sound"] = sound_var.get()
            self.cfg["toasts"] = toast_var.get()
            self.cfg.save()
            if self.cfg["indoor_ip"]:
                self.model.indoor_ip = self.cfg["indoor_ip"]
                self.model.lan_subnet = core.subnet_of(self.model.indoor_ip)
                self.model.zones["LAN"]["subnet"] = self.model.lan_subnet
                self.model.routers["indoor"]["ip"] = self.model.indoor_ip
            if self.cfg["outdoor_ip"]:
                self.model.outdoor_ip = self.cfg["outdoor_ip"]
                self.model.wan_subnet = core.subnet_of(
                    self.model.outdoor_ip)
                self.model.zones["WAN"]["subnet"] = self.model.wan_subnet
                self.model.routers["outdoor"]["ip"] = self.model.outdoor_ip
            self.model.routers["internet"]["ip"] = \
                self.cfg["internet_probe"]
            self.model.routers["internet"]["mode"] = \
                self.cfg["internet_probe"] + " probe"
            self.auto_var.set(self.cfg["autoscan_min"] > 0)
            self._apply_autoscan()
            win.destroy()
            if theme_var.get() != self.theme_name:
                messagebox.showinfo(APP_NAME, "Theme changes take effect "
                                              "after restarting the app.")
            self.model.say("Settings saved")

        btns = tk.Frame(body, bg=self.theme["bg"])
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Save", command=save).pack(side="right")
        ttk.Button(btns, text="Cancel",
                   command=win.destroy).pack(side="right", padx=6)

    def show_about(self):
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n\n"
            "Monitors a two-router chain:\n"
            "   outdoor WISP router → indoor LAN router → devices\n\n"
            "• automatic topology discovery\n"
            "• live device list with names, vendors and latency\n"
            "• join/leave alerts, speed test, traceroute, port scan\n"
            "• CSV/JSON export\n\n"
            f"Settings folder:\n{self.cfg.dir}\n\n"
            "Built with Python + tkinter.")

    # ── lifecycle ──
    def _cancel_afters(self):
        for key, aid in list(self._after_ids.items()):
            try:
                self.after_cancel(aid)
            except Exception:
                pass
            self._after_ids.pop(key, None)

    def _smoke_finish(self):
        self._cancel_afters()
        ok = bool(self.model.indoor_ip)
        payload = {"ok": ok, "indoor": self.model.indoor_ip,
                   "outdoor": self.model.outdoor_ip,
                   "lan_devices": len(self.model.zones["LAN"]["devices"]),
                   "wan_devices": len(self.model.zones["WAN"]["devices"])}
        try:
            with open(self.smoke_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except OSError:
            pass
        self.exit_code = 0 if ok else 3
        self.destroy()

    def _on_close(self):
        self._cancel_afters()
        self.model.stop()
        self.destroy()


def main():
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if "--check" in sys.argv:
        return core.main()
    smoke = None
    if "--smoke" in sys.argv:
        idx = sys.argv.index("--smoke")
        smoke = (sys.argv[idx + 1] if idx + 1 < len(sys.argv)
                 else "smoke.json")
    app = App(smoke_path=smoke)
    app.mainloop()
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║   DUAL-ROUTER NETWORK COMMAND CENTER                                 ║
║   Termux · Linux · Windows   —   pure stdlib, zero pip installs      ║
╠══════════════════════════════════════════════════════════════════════╣
║   OUTDOOR router (WISP mode)   :  192.168.1.1                        ║
║   INDOOR  router (LAN gateway) :  192.168.0.1                        ║
╚══════════════════════════════════════════════════════════════════════╝

Topology handled by this tool:

      INTERNET  ~ ~ ~  [ OUTDOOR ROUTER · WISP ]         192.168.1.1
                            │  wireless uplink
      ─────────────────────┴───────────────────────────────────────
        WAN leg between the two boxes : 192.168.1.0/24
        (the indoor router's WAN port lives here, e.g. 192.168.1.2)
      ─────────────────────┬───────────────────────────────────────
                       [ INDOOR ROUTER · router mode ]  LAN 192.168.0.1
                            │
                      192.168.0.0/24   ← this phone + your devices

Discovery order (zero configuration needed):
    indoor router  = default gateway from the routing table
                     (falls back to 192.168.0.1)
    outdoor router = first private traceroute hop behind that gateway
                     (falls back to a ping test of 192.168.1.1)

Environment overrides:
    INDOOR_ROUTER_IP=192.168.0.1 OUTDOOR_ROUTER_IP=192.168.1.1 \
        python dual_router_dashboard.py

Keys:
  ↑ / ↓  select device        TAB  switch zone (LAN / WAN-side)
  R      force rescan         P    ping selected device
  T      traceroute selected  D    device details (ENTER also toggles)
  L      live latency graph   G    gateway / router health view
  A      ARP table            O    open router admin page in browser
  S      toggle sound alerts  Q    quit   (ESC returns to the table view)

Extra CLI mode:
    python dual_router_dashboard.py --check
    → prints the detected topology and exits (no TUI, safe to script)

Linux/Termux needs: `ip` + `ping` (+ optional `traceroute`).
Windows uses the built-in ping / tracert / arp / route.
"""

import os
import re
import sys
import time
import shutil
import signal
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

# ─────────────────────────── TOPOLOGY DEFAULTS ───────────────────────────
# Outdoor router: the WISP box that talks to the tower. Its LAN side is the
# leg the indoor router's WAN port is plugged into.
OUTDOOR_ROUTER_IP = os.environ.get("OUTDOOR_ROUTER_IP", "").strip() or "192.168.1.1"
# Indoor router: the box this phone/PC is attached to — the LAN gateway.
INDOOR_ROUTER_IP = os.environ.get("INDOOR_ROUTER_IP", "").strip() or "192.168.0.1"
INTERNET_PROBE = "8.8.8.8"
# Tried in order when traceroute cannot reveal the outdoor box.
OUTDOOR_CANDIDATES = tuple(dict.fromkeys(
    (OUTDOOR_ROUTER_IP, "192.168.2.1", "192.168.10.1")))
MAX_HIST = 60          # latency samples kept per host (~2 min at 2 s cadence)
SCAN_BATCH = 64        # parallel pings per sweep wave
HOST_CACHE_TTL = 900   # do not retry a failed reverse-DNS lookup for 15 min
IS_WIN = os.name == "nt"

# ───────────────────────────── ANSI PALETTE ──────────────────────────────
ESC = "\033["
class C:
    RESET   = ESC + "0m"
    BOLD    = ESC + "1m"
    DIM     = ESC + "2m"
    BLINK   = ESC + "5m"
    RED     = ESC + "91m"
    GREEN   = ESC + "92m"
    YELLOW  = ESC + "93m"
    BLUE    = ESC + "94m"
    MAGENTA = ESC + "95m"
    CYAN    = ESC + "96m"
    WHITE   = ESC + "97m"
    ORANGE  = ESC + "38;5;208m"
    PINK    = ESC + "38;5;205m"
    LIME    = ESC + "38;5;118m"
    VIOLET  = ESC + "38;5;141m"
    GOLD    = ESC + "38;5;220m"
    TEAL    = ESC + "38;5;51m"
    BG_MAG  = ESC + "45m"
    BG_BLUE = ESC + "44m"
    BG_DARK = ESC + "48;5;232m"

def clr():
    return C.RESET



# ──────────────────────────── PLATFORM LAYER ─────────────────────────────
def enable_virtual_terminal():
    """Best effort: switch on ANSI escape processing on Windows consoles."""
    if not IS_WIN:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def clear_screen():
    os.system("cls" if IS_WIN else "clear")


def sh(cmd, timeout=8):
    """Run a shell command; never raises, always returns plain text."""
    flags = 0x08000000 if IS_WIN else 0      # CREATE_NO_WINDOW on Windows
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, errors="replace", timeout=timeout,
                              creationflags=flags)
        return proc.stdout or ""
    except Exception:
        return ""


def ping_cmd(ip, count=1, wait=1):
    """Platform-correct ping invocation (wait is in seconds)."""
    if IS_WIN:
        return f"ping -n {count} -w {int(wait * 1000)} {ip}"
    return f"ping -c {count} -W {wait} {ip}"


def trace_cmd(ip, max_hops=15, wait=2):
    if IS_WIN:
        return f"tracert -d -h {max_hops} -w {int(wait * 1000)} {ip}"
    tool = shutil.which("traceroute") or "traceroute"
    return f'"{tool}" -n -m {max_hops} -w {wait} -q 1 {ip}'


def ping_ms(ip, count=1, wait=1):
    """Round-trip time in ms, or None when the host did not answer."""
    out = sh(ping_cmd(ip, count, wait), timeout=count * (wait + 1) + 4)
    if not out:
        return None
    match = re.search(r"time[=<]\s*([\d.]+)\s*ms", out, re.I)
    if match:
        return float(match.group(1))
    if re.search(r"(ttl=|bytes from)", out, re.I):
        return 0.0        # answered, but no timing line was printed
    return None


def ping_available():
    """Probe a real host (the gateway) — some sandboxes block loopback."""
    gateway = get_gateway()
    if gateway:
        return ping_ms(gateway, 1, 1) is not None
    return ping_ms("127.0.0.1", 1, 1) is not None


def traceroute_hops(target, max_hops=4, wait=1):
    """Hop IPs on the way to target; None marks an unresponsive hop."""
    out = sh(trace_cmd(target, max_hops, wait),
             timeout=max(10, max_hops * (wait + 1) + 5))
    hops = []
    for line in out.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)$", line)
        if not match:
            continue                      # skips headers / blank lines
        found = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", match.group(2))
        hops.append(found[0] if found else None)
    return hops



def is_private_ip(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        first, second = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return (first == 10 or first == 127 or
            (first == 172 and 16 <= second <= 31) or
            (first == 192 and second == 168) or
            (first == 100 and 64 <= second <= 127))


def find_upstream_hop(gateway, lan_subnet=None):
    """First private hop behind our gateway = the outdoor WISP router.

    TTL 1 dies on the indoor router (answers from 192.168.0.1); TTL 2 dies on
    the outdoor box, which answers from its own LAN side (192.168.1.1).
    Hops that do not answer a direct ping are skipped, so an unresponsive
    hop-2 does not make an ISP router (e.g. 10.x CGNAT) look like the WISP box.
    """
    fallback = None
    for hop in traceroute_hops(INTERNET_PROBE, 4, 1):
        if not hop or hop == gateway or hop == "0.0.0.0":
            continue
        if hop.startswith("169.254.") or not is_private_ip(hop):
            continue
        if lan_subnet and hop.startswith(lan_subnet):
            continue                      # still inside our own LAN leg
        if ping_ms(hop, 1, 1) is not None:
            return hop
        if fallback is None and hop.startswith(("192.168.", "172.")):
            fallback = hop                # plausible but silent — keep as fallback
    return fallback


def discover_outdoor(gateway, lan_subnet=None):
    """Locate the outdoor router. Returns (ip | None, how_it_was_found)."""
    hop = find_upstream_hop(gateway, lan_subnet)
    if hop and hop != gateway:
        return hop, "traceroute hop"
    for candidate in OUTDOOR_CANDIDATES:
        if candidate == gateway:
            continue
        if ping_ms(candidate, 1, 1) is not None:
            return candidate, "known default"
    return None, "not found"


def get_gateway():
    """Default gateway of this phone/PC — the indoor router (192.168.0.1)."""
    match = None
    if IS_WIN:
        match = re.search(r"0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)",
                          sh("route print 0.0.0.0"))
    else:
        match = re.search(r"default via (\d+\.\d+\.\d+\.\d+)",
                          sh("ip route show default"))
        if not match:
            match = re.search(r"^0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)",
                              sh("route -n"), re.M)
    return match.group(1) if match else INDOOR_ROUTER_IP


def local_ip():
    """This device's own LAN address (no packet actually leaves the box)."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((INTERNET_PROBE, 80))
        return sock.getsockname()[0]
    except Exception:
        return ""
    finally:
        if sock:
            sock.close()


def subnet_of(ip):
    return ".".join(ip.split(".")[:3]) + "." if ip else None


def ip_sort_key(ip):
    try:
        return tuple(int(p) for p in ip.split("."))
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)


def resolve_host(ip):
    """Short hostname for an IP, or '' — never raises, never blocks long."""
    try:
        name = socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""
    if not name or name == ip:
        return ""
    return name.split(".")[0][:22]



_arp_cache = {"ts": 0.0, "data": {}}
_arp_lock = threading.Lock()


def arp_table(max_age=3.0):
    """{ip: mac} of the IPv4 neighbours the kernel knows about (cached)."""
    now = time.time()
    with _arp_lock:
        if now - _arp_cache["ts"] < max_age:
            return dict(_arp_cache["data"])
    table = {}
    if IS_WIN:
        pattern = (r"(\d+\.\d+\.\d+\.\d+)\s+"
                   r"([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})")
        for match in re.finditer(pattern, sh("arp -a")):
            table[match.group(1)] = match.group(2).replace("-", ":").lower()
    else:
        for line in sh("ip neigh show").splitlines():
            parts = line.split()
            if "lladdr" in parts:
                try:
                    mac = parts[parts.index("lladdr") + 1]
                except IndexError:
                    continue
                if mac != "00:00:00:00:00:00":
                    table[parts[0]] = mac.lower()
        try:
            with open("/proc/net/arp", encoding="utf-8",
                      errors="replace") as handle:
                next(handle, None)
                for line in handle:
                    cols = line.split()
                    if len(cols) >= 4 and cols[3] != "00:00:00:00:00:00":
                        table.setdefault(cols[0], cols[3].lower())
        except OSError:
            pass
    with _arp_lock:
        _arp_cache["ts"], _arp_cache["data"] = time.time(), table
    return dict(table)


def vendor_hint(mac):
    """Tiny offline OUI table — no network lookups needed."""
    oui = {
        "b0:0f:d5": "Tenda", "c8:3a:35": "Tenda", "d8:32:14": "Tenda",
        "50:2b:73": "Tenda", "04:95:e6": "Tenda", "50:d4:f7": "TP-Link",
        "f4:ec:38": "TP-Link", "50:c7:bf": "TP-Link", "98:da:c4": "TP-Link",
        "b0:be:76": "TP-Link", "9c:d6:43": "Xiaomi", "64:cc:2e": "Xiaomi",
        "50:8f:4c": "Xiaomi", "78:11:dc": "Xiaomi", "ac:cf:85": "Huawei",
        "34:6b:d3": "Huawei", "3c:5a:b4": "Google", "f4:f5:d8": "Google",
        "a4:c1:38": "Apple", "f0:18:98": "Apple", "3c:22:fb": "Apple",
        "dc:a6:32": "Raspberry Pi", "b8:27:eb": "Raspberry Pi",
        "e4:5f:01": "Raspberry Pi", "fc:f5:c4": "Samsung",
        "8c:f5:a3": "Samsung", "a0:40:a0": "Netgear", "20:4e:7f": "Netgear",
        "24:0a:c4": "Espressif", "7c:9e:bd": "Espressif",
        "84:0d:8e": "Espressif", "3c:71:bf": "Espressif",
    }
    return oui.get(mac.lower()[:8], "")


def sweep(subnet, must_have, tick=None, batch=SCAN_BATCH, wait=1):
    """Ping every host of <subnet>x (x = 1..254), return the live set.

    Threads go out in waves so nothing is left pinging after the sweep
    returns; `tick` fires once per probed host (drives the progress bar).
    """
    live, lock = set(), threading.Lock()

    def probe(ip):
        try:
            if ping_ms(ip, 1, wait) is not None:
                with lock:
                    live.add(ip)
        finally:
            if tick:
                tick()

    hosts = [f"{subnet}{i}" for i in range(1, 255)]
    for start in range(0, len(hosts), batch):
        wave = [threading.Thread(target=probe, args=(ip,), daemon=True)
                for ip in hosts[start:start + batch]]
        for thread in wave:
            thread.start()
        for thread in wave:
            thread.join(timeout=wait + 2.5)
    for ip in must_have or ():
        if ip and ip not in live and ping_ms(ip, 1, wait) is not None:
            live.add(ip)
    return live



# ─────────────────────────── DASHBOARD APP ───────────────────────────────
class Dashboard:
    WIDE_TABLE = 90            # terminal width at/above which MAC column shows
    OFFLINE_KEEP_SECONDS = 600  # keep silent-but-known hosts listed for 10 min

    def __init__(self):
        self.running = True
        self.mode = "table"          # table|detail|graph|arp|health
        self.alert = True
        self.zone = "LAN"
        self.sel = {"LAN": 0, "WAN": 0}
        self.msg, self.msg_time = "", 0.0
        self.lat_hist = {}           # ip (or "R:<router>") → latency samples
        self.host_cache = {}         # ip → (hostname, resolved_at)
        self.scanning = False
        self.scan_pct = 0
        self.scan_done = 0
        self.scan_total = 1
        self.wide = True
        self.term_w, self.term_h = shutil.get_terminal_size((100, 40))
        self._lock = threading.RLock()
        self._old_term = None
        self.ping_ok = ping_available()

        # ── topology: indoor router first ──
        self.local_ip = local_ip()
        self.indoor_gw = get_gateway()
        self.lan_subnet = subnet_of(self.indoor_gw)
        print(f"  {C.GOLD}Indoor router (LAN gateway) : {C.WHITE}{self.indoor_gw}"
              f"{clr()}  {C.DIM}[{self.lan_subnet}0/24]{clr()}")
        print(f"  {C.DIM}locating the outdoor WISP router…{clr()}")

        # ── topology: outdoor router second ──
        outdoor, how = discover_outdoor(self.indoor_gw, self.lan_subnet)
        if outdoor and subnet_of(outdoor) == self.lan_subnet:
            outdoor, how = None, "ignored (same subnet as the indoor LAN)"
        self.outdoor_ip = outdoor
        self.wan_subnet = subnet_of(outdoor) if outdoor else None
        print(f"  {C.GOLD}Outdoor router (WISP)       : {C.WHITE}"
              f"{self.outdoor_ip or 'not found — press R to retry'}{clr()}  "
              f"{C.DIM}[{how}]{clr()}")

        # ── live router rows (always visible in the health strip) ──
        self.routers = {
            "indoor":   {"name": "INDOOR ROUTER",
                         "mode": f"router mode · LAN {self.lan_subnet}0/24",
                         "ip": self.indoor_gw, "lat": None, "online": True},
            "outdoor":  {"name": "OUTDOOR ROUTER",
                         "mode": "WISP mode · wireless uplink",
                         "ip": self.outdoor_ip, "lat": None, "online": True},
            "internet": {"name": "INTERNET",
                         "mode": f"{INTERNET_PROBE} probe",
                         "ip": INTERNET_PROBE, "lat": None, "online": True},
        }

        # ── the two zones ──
        wan_label = (f"WAN SIDE {self.wan_subnet}0/24 (between routers)"
                     if self.wan_subnet
                     else "WAN SIDE — outdoor router not located yet")
        self.zones = {
            "LAN": {"label": f"INDOOR LAN {self.lan_subnet}0/24",
                    "subnet": self.lan_subnet, "devices": {}, "sorted": [],
                    "color": C.CYAN},
            "WAN": {"label": wan_label, "subnet": self.wan_subnet,
                    "devices": {}, "sorted": [], "color": C.ORANGE},
        }
        time.sleep(0.8)

    # ── terminal helpers ──
    def raw_on(self):
        if not IS_WIN:
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                self._old_term = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            except Exception:
                self._old_term = None
        sys.stdout.write(ESC + "?25l" + ESC + "?1049h")
        sys.stdout.flush()

    def raw_off(self):
        sys.stdout.write(ESC + "?25h" + ESC + "?1049l" + clr())
        sys.stdout.flush()
        if not IS_WIN and self._old_term:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN,
                                  self._old_term)
            except Exception:
                pass
        self._old_term = None



    # ── input handling ──
    def keypress(self, timeout=0.35):
        """'UP'/'DOWN'/'TAB'/'ESC'/'ENTER'/single char, or None on timeout."""
        if IS_WIN:
            return self._keypress_win(timeout)
        import select
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        char = sys.stdin.read(1)
        if char == "\x1b":
            ready2, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not ready2:
                return "ESC"
            seq = sys.stdin.read(2)
            return {"[A": "UP", "[B": "DOWN"}.get(seq)
        if char == "\t":
            return "TAB"
        if char in "\r\n":
            return "ENTER"
        return char

    @staticmethod
    def _keypress_win(timeout):
        import msvcrt
        deadline = time.time() + timeout
        while time.time() < deadline:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):           # arrow-key prefix
                    code = msvcrt.getwch()
                    return {"H": "UP", "P": "DOWN"}.get(code)
                if char == "\x1b":
                    return "ESC"
                if char == "\t":
                    return "TAB"
                if char == "\r":
                    return "ENTER"
                return char
            time.sleep(0.02)
        return None

    def say(self, text):
        self.msg, self.msg_time = text, time.time()

    def beep(self):
        if self.alert:
            sys.stdout.write("\a")
            sys.stdout.flush()

    # ── scanning ──
    def role_for(self, ip):
        if ip == self.indoor_gw:
            return "INDOOR ROUTER · LAN GW"
        if self.outdoor_ip and ip == self.outdoor_ip:
            return "OUTDOOR ROUTER · WISP"
        if ip == self.local_ip:
            return "THIS DEVICE"
        if ip == INTERNET_PROBE:
            return "INTERNET PROBE"
        return ""

    def ensure_outdoor(self):
        """Re-discover the outdoor router when it is still unknown."""
        if self.outdoor_ip:
            return
        outdoor, how = discover_outdoor(self.indoor_gw, self.lan_subnet)
        if not outdoor or subnet_of(outdoor) == self.lan_subnet:
            return
        self.outdoor_ip = outdoor
        self.wan_subnet = subnet_of(outdoor)
        with self._lock:
            self.routers["outdoor"]["ip"] = outdoor
            self.zones["WAN"]["subnet"] = self.wan_subnet
            self.zones["WAN"]["label"] = (f"WAN SIDE {self.wan_subnet}0/24 "
                                          f"(between routers)")
        self.say(f"Outdoor router located at {outdoor} ({how})")

    def full_scan(self):
        if self.scanning:
            return
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_tick(self):
        with self._lock:
            self.scan_done += 1
            self.scan_pct = int(self.scan_done / self.scan_total * 100)

    def _scan_worker(self):
        self.scanning = True
        self.scan_pct = 0
        try:
            self.ensure_outdoor()
            jobs = [(name, zone["subnet"]) for name, zone in self.zones.items()
                    if zone["subnet"]]
            self.scan_done = 0
            self.scan_total = max(1, len(jobs) * 254)
            for zname, subnet in jobs:
                must = [self.indoor_gw]
                if zname == "WAN" and self.outdoor_ip:
                    must.append(self.outdoor_ip)
                live = sweep(subnet, must, self._scan_tick)
                self.merge_zone(zname, live)
            self.prune_histories()
            count = sum(len(zone["devices"]) for zone in self.zones.values())
            self.say(f"SCAN COMPLETE — {count} devices across both zones")
            self.beep()
        finally:
            self.scanning = False



    # ── device-list maintenance ──
    def cached_host(self, ip):
        """Cached hostname, '' when known-empty, None when never looked up."""
        entry = self.host_cache.get(ip)
        if not entry:
            return None
        name, when = entry
        if name:
            return name
        if time.time() - when < HOST_CACHE_TTL:
            return ""
        return None

    def merge_zone(self, zname, live):
        """Replace a zone's device list, keeping history + silent hosts."""
        arps = arp_table()
        now = time.time()
        need_dns = []
        with self._lock:
            previous = self.zones[zname]["devices"]
            devices = {}
            for ip in live:
                old = previous.get(ip, {})
                mac = arps.get(ip) or old.get("mac", "")
                host = self.cached_host(ip)
                if host is None:
                    host = old.get("host", "")
                    need_dns.append(ip)
                devices[ip] = {
                    "ip": ip, "mac": mac, "host": host,
                    "vendor": vendor_hint(mac) if mac
                              else old.get("vendor", ""),
                    "role": self.role_for(ip),
                    "lat": old.get("lat"), "online": True,
                    "seen": time.strftime("%H:%M:%S"), "up_ts": now,
                    "first_seen": old.get("first_seen")
                                  or time.strftime("%H:%M:%S"),
                }
            for ip, old in previous.items():   # silent but recently known
                if ip in devices:
                    continue
                if now - old.get("up_ts", 0) > self.OFFLINE_KEEP_SECONDS:
                    continue
                ghost = dict(old)
                ghost.update(online=False, lat=None)
                devices[ip] = ghost
            self.zones[zname]["devices"] = devices
            self.zones[zname]["sorted"] = sorted(devices, key=ip_sort_key)
            self.sel[zname] = min(self.sel[zname], max(0, len(devices) - 1))
        self.resolve_hosts(need_dns)

    def resolve_hosts(self, ips):
        """Reverse-DNS in the background so the UI never blocks on it."""
        for ip in ips[:80]:
            threading.Thread(target=self._resolve_one, args=(ip,),
                             daemon=True).start()

    def _resolve_one(self, ip):
        name = resolve_host(ip)
        self.host_cache[ip] = (name, time.time())
        if not name:
            return
        with self._lock:
            for zone in self.zones.values():
                device = zone["devices"].get(ip)
                if device and not device.get("host"):
                    device["host"] = name

    def prune_histories(self):
        with self._lock:
            known = {ip for zone in self.zones.values()
                     for ip in zone["devices"]}
        for key in list(self.lat_hist):
            if not key.startswith("R:") and key not in known:
                self.lat_hist.pop(key, None)



    # ── background latency probes ──
    def latency_loop(self):
        """Ping both routers + internet + every known device every 2 s."""
        pool = ThreadPoolExecutor(max_workers=24)
        try:
            while self.running:
                for key in ("indoor", "outdoor", "internet"):
                    router = self.routers[key]
                    if not router["ip"]:
                        router["online"] = False
                        continue
                    self.apply_router_sample(key, ping_ms(router["ip"], 1, 1))
                with self._lock:
                    targets = [(zname, ip)
                               for zname, zone in self.zones.items()
                               for ip in zone["devices"]]
                if targets:
                    results = pool.map(lambda tgt: ping_ms(tgt[1], 1, 1),
                                       targets)
                    for (zname, ip), ms in zip(targets, results):
                        self.apply_device_sample(zname, ip, ms)
                time.sleep(2)
        finally:
            pool.shutdown(wait=False)

    def apply_router_sample(self, key, ms):
        router = self.routers[key]
        was_up = router["online"]
        router["lat"], router["online"] = ms, ms is not None
        history = self.lat_hist.setdefault("R:" + key, [])
        history.append(ms if ms is not None else 0)
        del history[:-MAX_HIST]
        if was_up and not router["online"]:
            self.say(f"⚠ {router['name']} ({router['ip']}) UNREACHABLE")
            self.beep()
        elif not was_up and router["online"]:
            self.say(f"✔ {router['name']} ({router['ip']}) RECOVERED")
            self.beep()

    def apply_device_sample(self, zname, ip, ms):
        with self._lock:
            device = self.zones[zname]["devices"].get(ip)
            if device is None:
                return
            was_up = device["online"]
            device["lat"], device["online"] = ms, ms is not None
            if ms is not None:
                device["seen"] = time.strftime("%H:%M:%S")
                device["up_ts"] = time.time()
            label = device["host"] or device["role"] or ip
            history = self.lat_hist.setdefault(ip, [])
            history.append(ms if ms is not None else 0)
            del history[:-MAX_HIST]
        if was_up and ms is None:
            self.say(f"⚠ {label} ({ip}) WENT OFFLINE")
            self.beep()
        elif not was_up and ms is not None:
            self.say(f"✔ {label} ({ip}) BACK ONLINE")
            self.beep()



    # ── drawing helpers ──
    def frame(self, title, accent=None):
        accent = accent or C.CYAN
        w = max(60, min(self.term_w - 2, 96))
        title = f" {title} "
        if len(title) > w - 2:
            title = title[:w - 2]
        pad = max(0, w - len(title) - 2)
        top = (accent + "╔═" + C.BOLD + C.GOLD + title + C.RESET + accent
               + "═" * pad + "╗" + clr())
        bot = accent + "╚" + "═" * w + "╝" + clr()
        return top, bot, w

    def spark(self, history, n=40):
        bars = "▁▂▃▄▅▆▇█"
        if not history:
            return ""
        peak = max(max(history), 1)
        out = ""
        for value in history[-n:]:
            index = min(int(value / peak * 7), 7)
            color = (C.LIME if value < 30
                     else (C.YELLOW if value < 100 else C.RED))
            out += color + bars[index]
        return out + clr()

    def lat_col(self, ms):
        if ms is None:
            return C.DIM + C.WHITE
        if ms < 30:
            return C.BOLD + C.LIME
        if ms < 100:
            return C.BOLD + C.YELLOW
        return C.BOLD + C.RED + C.BLINK

    def router_badge(self, key):
        router = self.routers[key]
        if not router["ip"]:
            return f"{C.DIM}{router['name']}: not found{clr()}"
        dot = (C.LIME + "●" if router["online"]
               else C.RED + C.BLINK + "●" + C.RESET)
        latency = f"{router['lat']:.0f}ms" if router["lat"] is not None else "--"
        return (f"{dot} {C.BOLD}{C.WHITE}{router['name']:<15}{clr()} "
                f"{C.GOLD}{router['ip']:<15}{clr()} "
                f"{self.lat_col(router['lat'])}{latency:>7}{clr()}")

    def header_row(self):
        if self.wide:
            return (f" {C.BOLD}{C.CYAN}{' #':<4}{'IP ADDRESS':<16}{'MAC':<18}"
                    f"{'IDENTITY':<24}{' LATENCY':>11}{' STATUS':<12}{clr()}")
        return (f" {C.BOLD}{C.CYAN}{' #':<4}{'IP ADDRESS':<16}"
                f"{'IDENTITY':<20}{' LATENCY':>10}{' STATUS':<12}{clr()}")

    def device_row(self, index, device, selected):
        arrow = (C.BOLD + C.PINK + "▶" + clr()) if selected else " "
        lat_w = 11 if self.wide else 10
        ident_w = 24 if self.wide else 20
        latency = (f"{device['lat']:.1f}ms" if device["lat"] is not None
                   else "--")
        identity = (device["role"] or device["host"] or device["vendor"]
                    or "unknown")[:ident_w]
        ident_col = C.GOLD if device["role"] else C.CYAN
        status = (C.BOLD + C.LIME + "● ONLINE" if device["online"]
                  else C.BOLD + C.RED + "● OFFLINE") + clr()
        row = f"{arrow}{C.GOLD}{index:<3}{C.WHITE}{device['ip']:<16}"
        if self.wide:
            row += f"{C.VIOLET}{(device['mac'] or '--'):<18}"
        row += (f"{ident_col}{identity:<{ident_w}}"
                f"{self.lat_col(device['lat'])}{latency:>{lat_w}}{clr()}"
                f" {status}")
        return C.BG_DARK + row + clr() if selected else row

    def empty_hint(self, zone):
        if zone is self.zones["WAN"] and not zone["subnet"]:
            return (C.DIM + "outdoor router not located yet — press "
                    + C.LIME + "[R]" + clr() + C.DIM
                    + " to rediscover it" + clr())
        return ("no devices — press " + C.LIME + "[R]" + clr()
                + C.DIM + " to scan this zone" + clr())



    # ── screen composition ──
    def draw(self):
        self.term_w, self.term_h = shutil.get_terminal_size((100, 40))
        self.wide = self.term_w >= self.WIDE_TABLE
        w = max(60, min(self.term_w - 2, 96))
        head, body, tail = [], [], []

        head.append(C.MAGENTA + "█" * (w + 2) + clr())
        head.append(C.BG_MAG + C.BOLD + C.WHITE
                    + " DUAL-ROUTER ⚡ NETWORK COMMAND CENTER ".center(w)[:w]
                    + clr())
        head.append(C.MAGENTA + "█" * (w + 2) + clr())

        for key in ("indoor", "outdoor", "internet"):
            head.append(" " + self.router_badge(key))
        head.append(f" {C.DIM}internet trend{clr()} "
                    + self.spark(self.lat_hist.get("R:internet", []),
                                 max(10, w - 26)))

        outdoor = self.outdoor_ip or "not found"
        topo = (f" {C.DIM}INTERNET{clr()} {C.DIM}~►{clr()} "
                f"{C.ORANGE}[OUTDOOR·WISP {outdoor}]{clr()} {C.DIM}─►{clr()} "
                f"{C.CYAN}[INDOOR {self.indoor_gw}]{clr()} {C.DIM}─►{clr()} "
                f"{C.CYAN}LAN {self.lan_subnet}0/24{clr()}")
        if self.wan_subnet:
            topo += f" {C.DIM}│ WAN leg {self.wan_subnet}0/24{clr()}"
        head.append(topo)

        with self._lock:
            lan = [self.zones["LAN"]["devices"][ip]
                   for ip in self.zones["LAN"]["sorted"]]
            wan = [self.zones["WAN"]["devices"][ip]
                   for ip in self.zones["WAN"]["sorted"]]
        online = sum(1 for device in lan + wan if device["online"])
        offline = len(lan) + len(wan) - online
        head.append(f" {C.BOLD}{C.CYAN}LAN {len(lan)}{clr()} · "
                    f"{C.ORANGE}WAN-side {len(wan)}{clr()} · "
                    f"{C.GREEN}● UP {online}{clr()}  "
                    f"{C.RED}● DOWN {offline}{clr()}  "
                    f"{C.ORANGE}{time.strftime('%H:%M:%S')}{clr()}")

        if self.scanning:
            width = 36
            filled = int(width * self.scan_pct / 100)
            bar = (C.LIME + "█" * filled + C.DIM + C.WHITE
                   + "░" * (width - filled))
            head.append(f" {C.BOLD}{C.PINK}SCANNING {bar}{clr()} "
                        f"{C.GOLD}{self.scan_pct}%")
        head.append("")

        view = {"table": self.view_table, "detail": self.view_detail,
                "graph": self.view_graph, "arp": self.view_arp,
                "health": self.view_health}.get(self.mode, self.view_table)
        view(body)

        keys = [("↑↓", "select", C.CYAN), ("TAB", "zone", C.ORANGE),
                ("R", "rescan", C.LIME), ("P", "ping", C.YELLOW),
                ("T", "trace", C.PINK), ("D", "detail", C.VIOLET),
                ("L", "graph", C.TEAL), ("G", "health", C.GOLD),
                ("A", "arp", C.BLUE), ("O", "admin", C.GREEN),
                ("S", "alert", C.MAGENTA), ("Q", "quit", C.RED)]
        bar = " " + "  ".join(
            f"{C.BOLD}{color}[{key}]{C.WHITE}{clr()}{label}"
            for key, label, color in keys)
        tail.append(C.BG_DARK + bar + clr())

        if self.msg and time.time() - self.msg_time < 6:
            tail.insert(0, f" {C.BG_BLUE}{C.BOLD}{C.WHITE} {self.msg} {clr()}")
        else:
            tail.insert(0, "")

        visible = self.term_h - len(head) - len(tail) - 1
        visible = max(3, visible)
        if len(body) > visible:
            hidden = len(body) - visible
            body = body[:visible - 1]
            body.append(f"  {C.DIM}… {hidden} more line(s) — enlarge the "
                        f"terminal to see the whole view{clr()}")

        sys.stdout.write(ESC + "H" + "\n".join(head + body + tail) + "\n"
                         + ESC + "J")
        sys.stdout.flush()



    # ── views ──
    def view_table(self, out):
        zone = self.zones[self.zone]
        accent = zone["color"]
        top, bot, w = self.frame(f"{zone['label']}  ·  TAB switches zone",
                                 accent)
        out.append(top)
        out.append(self.header_row())
        out.append(accent + " ╟" + "─" * (w - 2) + "╢" + clr())
        with self._lock:
            devices = [zone["devices"][ip] for ip in zone["sorted"]]
            selected = self.sel[self.zone]
        for index, device in enumerate(devices):
            out.append(self.device_row(index, device, index == selected))
            trend = self.spark(self.lat_hist.get(device["ip"], []))
            if trend:
                out.append(f"      {C.DIM}trend{clr()} {trend}")
        if not devices:
            out.append("  " + self.empty_hint(zone))
        if self.zone == "WAN" and self.wan_subnet:
            out.append(f"  {C.DIM}note: this leg sits behind NAT — normally "
                       f"only the outdoor box answers pings from here{clr()}")
        out.append(bot)

    def _selected(self):
        zone = self.zones[self.zone]
        with self._lock:
            if not zone["sorted"]:
                return None
            index = min(self.sel[self.zone], len(zone["sorted"]) - 1)
            return zone["devices"][zone["sorted"][index]]

    def view_detail(self, out):
        top, bot, w = self.frame("DEVICE DETAILS")
        out.append(top)
        device = self._selected()
        if not device:
            out.append("  no device selected — press ↑/↓ in the table view")
            out.append(f"\n  {C.DIM}ESC to return{clr()}")
            out.append(bot)
            return
        rows = [
            ("Zone", "INDOOR LAN" if self.zone == "LAN"
             else "WAN SIDE (between routers)", C.TEAL),
            ("IP Address", device["ip"], C.GOLD),
            ("MAC Address", device["mac"] or "—", C.VIOLET),
            ("Hostname", device["host"] or "—", C.CYAN),
            ("Vendor", device["vendor"] or "unknown", C.PINK),
            ("Role", device["role"] or "client device", C.ORANGE),
            ("Latency", (f"{device['lat']:.1f} ms"
                         if device["lat"] is not None else "—"),
             self.lat_col(device["lat"])),
            ("Status", "ONLINE" if device["online"] else "OFFLINE",
             C.LIME if device["online"] else C.RED),
            ("Last seen", device.get("seen", "—"), C.BLUE),
            ("First seen", device.get("first_seen", "—"), C.BLUE),
        ]
        for name, value, color in rows:
            out.append(f"  {C.BOLD}{C.WHITE}{name:<14}{clr()} : "
                       f"{C.BOLD}{color}{value}{clr()}")
        out.append(f"  {C.BOLD}{C.WHITE}{'Trend':<14}{clr()} : "
                   + self.spark(self.lat_hist.get(device["ip"], [])))
        out.append(f"\n  {C.DIM}ESC to return{clr()}")
        out.append(bot)



    def view_graph(self, out):
        top, bot, w = self.frame("LIVE LATENCY GRAPH")
        out.append(top)
        device = self._selected()
        ip = device["ip"] if device else None
        with self._lock:
            history = list(self.lat_hist.get(ip, [])) if ip else []
        name = ((device["role"] or device["host"] or device["vendor"]
                 or "unknown") if device else "")
        out.append(f"  {C.BOLD}{C.GOLD}{ip or '—'}{clr()}  "
                   f"{C.CYAN}{name}{clr()}")
        if history:
            peak = max(max(history), 1)
            height = 8
            data = history[-max(10, w - 14):]
            grid = [[" "] * len(data) for _ in range(height)]
            for x, value in enumerate(data):
                if value <= 0:
                    grid[height - 1][x] = "·"     # missed sample
                    continue
                level = min(int(value / peak * (height - 1)), height - 1)
                for y in range(level + 1):
                    grid[height - 1 - y][x] = "█"
            for row_index, row in enumerate(grid):
                label = peak * (height - 1 - row_index) / (height - 1)
                out.append(f" {C.DIM}{label:5.0f} ms│{clr()}"
                           f"{C.LIME}{''.join(row)}{clr()}")
            live = [v for v in data if v > 0]
            stats = (f"min {min(live):.0f} · avg {sum(live) / len(live):.0f} · "
                     f"max {max(live):.0f} ms" if live else "no live samples")
            out.append(f"       {C.DIM}└{'─' * len(data)}{clr()}")
            out.append(f"  {C.DIM}{stats} · samples {len(data)} · "
                       f"missed {len(data) - len(live)}{clr()}")
        else:
            out.append(f"  {C.DIM}collecting samples — the background loop "
                       f"pings every 2 s{clr()}")
        out.append(f"\n  {C.DIM}ESC to return{clr()}")
        out.append(bot)

    def view_health(self, out):
        top, bot, w = self.frame("ROUTER HEALTH — LIVE")
        out.append(top)
        for key in ("indoor", "outdoor", "internet"):
            router = self.routers[key]
            latency = (f"{router['lat']:.1f} ms"
                       if router["lat"] is not None else "—")
            state = (C.BOLD + C.LIME + "● UP" if router["online"]
                     else C.BOLD + C.RED + "● DOWN")
            out.append(f"  {C.BOLD}{C.WHITE}{router['name']:<16}{clr()}"
                       f"{C.GOLD}{(router['ip'] or '—'):<16}{clr()}"
                       f"{C.DIM}{router['mode']:<34}{clr()}"
                       f"{self.lat_col(router['lat'])}{latency:>9}{clr()}  "
                       f"{state}{clr()}")
            out.append(f"    {self.spark(self.lat_hist.get('R:' + key, []), 56)}")
        out.append("")
        wan_text = (f"{self.wan_subnet}0/24 · outdoor {self.outdoor_ip}"
                    if self.wan_subnet else "unknown — press R to rediscover")
        out.append(f"  {C.DIM}LAN leg   : {self.lan_subnet}0/24 · "
                   f"gateway {self.indoor_gw} · this device "
                   f"{self.local_ip or '—'}{clr()}")
        out.append(f"  {C.DIM}WAN leg   : {wan_text}{clr()}")
        out.append(f"  {C.DIM}Diagnosis : INDOOR down → LAN/WiFi link · "
                   f"OUTDOOR down → WISP uplink lost · "
                   f"both up + INTERNET down → ISP side{clr()}")
        out.append(f"\n  {C.DIM}ESC to return{clr()}")
        out.append(bot)

    def view_arp(self, out):
        top, bot, w = self.frame("NEIGHBOUR / ARP TABLE")
        out.append(top)
        out.append(f"  {C.BOLD}{C.CYAN}{'IP ADDRESS':<17}{'MAC':<19}"
                   f"{'VENDOR':<14}ROLE{clr()}")
        out.append(C.CYAN + "  " + "─" * (w - 4) + clr())
        entries = sorted(arp_table().items(), key=lambda kv: ip_sort_key(kv[0]))
        for ip, mac in entries:
            role, color = "", C.ORANGE
            if ip == self.indoor_gw:
                role, color = "INDOOR ROUTER (LAN GW)", C.GOLD
            elif self.outdoor_ip and ip == self.outdoor_ip:
                role, color = "OUTDOOR ROUTER (WISP)", C.ORANGE
            elif ip == self.local_ip:
                role, color = "THIS DEVICE", C.LIME
            vendor = vendor_hint(mac) or "—"
            out.append(f"  {C.GOLD}{ip:<17}{C.VIOLET}{mac:<19}"
                       f"{C.CYAN}{vendor:<14}{color}{role}{clr()}")
        if not entries:
            out.append(f"  {C.DIM}ARP cache is empty — run a scan first [R]"
                       f"{clr()}")
        out.append(f"\n  {C.DIM}ESC to return{clr()}")
        out.append(bot)



    # ── actions ──
    def action_cmd(self, kind):
        device = self._selected()
        if not device:
            self.say("no device selected")
            return
        ip = device["ip"]
        cmd = (ping_cmd(ip, 6, 1) if kind == "ping"
               else trace_cmd(ip, 15, 2))
        self.raw_off()
        print(C.BOLD + C.GOLD + f"\n⚡ {cmd}\n" + clr())
        os.system(cmd)
        input(C.CYAN + "\n[press ENTER to return to the dashboard]" + clr())
        self.raw_on()
        self.say(f"{kind} {ip} finished")

    def open_admin(self):
        """Open the admin page of the router matching the current zone."""
        ip = (self.indoor_gw if self.zone == "LAN"
              else (self.outdoor_ip or self.indoor_gw))
        url = f"http://{ip}"
        opener = shutil.which("termux-open-url") or shutil.which("xdg-open")
        if opener:
            sh(f'"{opener}" {url}')
            self.say(f"Opened admin page → {url}")
        elif IS_WIN:
            os.startfile(url)
            self.say(f"Opened admin page → {url}")
        else:
            self.say(f"Admin page: {url} (install termux-api for auto-open)")

    # ── main loop ──
    def handle_key(self, key):
        if key in ("q", "Q"):
            self.running = False
        elif key == "UP":
            self.sel[self.zone] = max(0, self.sel[self.zone] - 1)
        elif key == "DOWN":
            last = max(0, len(self.zones[self.zone]["sorted"]) - 1)
            self.sel[self.zone] = min(last, self.sel[self.zone] + 1)
        elif key == "TAB":
            self.zone = "WAN" if self.zone == "LAN" else "LAN"
            self.say(f"Zone → {self.zones[self.zone]['label']}")
        elif key in ("R", "r"):
            self.full_scan()
        elif key in ("P", "p"):
            self.action_cmd("ping")
        elif key in ("T", "t"):
            self.action_cmd("trace")
        elif key in ("D", "d"):
            self.mode = "detail"
        elif key in ("L", "l"):
            self.mode = "graph"
        elif key in ("G", "g"):
            self.mode = "health"
        elif key in ("A", "a"):
            self.mode = "arp"
        elif key in ("O", "o"):
            self.open_admin()
        elif key in ("S", "s"):
            self.alert = not self.alert
            self.say("Alerts ON" if self.alert else "Alerts OFF")
        elif key == "ENTER":
            self.mode = "table" if self.mode == "detail" else "detail"
        elif key == "ESC":
            self.mode = "table"

    def run(self):
        self.raw_on()
        threading.Thread(target=self.latency_loop, daemon=True).start()
        self.full_scan()
        try:
            while self.running:
                self.draw()
                key = self.keypress()
                if key:
                    self.handle_key(key)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self.raw_off()
            print(C.BOLD + C.MAGENTA +
                  "\n ⚡ DUAL-ROUTER COMMAND CENTER — session closed ⚡\n"
                  + clr())



BANNER = (
    "\n"
    " ██████╗ ██╗   ██╗ █████╗ ██╗         ██████╗  ██████╗ ██╗   ██╗████████╗\n"
    " ██╔══██╗██║   ██║██╔══██╗██║         ██╔══██╗██╔═══██╗██║   ██║╚══██╔══╝\n"
    " ██║  ██║██║   ██║███████║██║         ██████╔╝██║   ██║██║   ██║   ██║\n"
    " ██║  ██║██║   ██║██╔══██║██║         ██╔══██╗██║   ██║██║   ██║   ██║\n"
    " ██████╔╝╚██████╔╝██║  ██║███████╗    ██║  ██║╚██████╔╝╚██████╔╝   ██║\n"
    " ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝    ╚═╝  ╚═╝ ╚═════╝  ╚═════╝    ╚═╝\n"
    "        OUTDOOR WISP " + OUTDOOR_ROUTER_IP +
    "  →  INDOOR LAN " + INDOOR_ROUTER_IP + "  ·  ONE SYSTEM\n")


def install_signal_handlers():
    """Restore the terminal instead of leaving it broken on SIGTERM/SIGHUP."""
    def _panic(signum, frame):
        try:
            sys.stdout.write(ESC + "?25h" + ESC + "?1049l" + C.RESET)
            sys.stdout.flush()
        finally:
            os._exit(1)
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _panic)
        except (ValueError, OSError):
            pass


def run_check():
    """--check: print the detected topology and exit (no TUI, scriptable)."""
    enable_virtual_terminal()
    print(C.BOLD + C.MAGENTA + "\n  DUAL-ROUTER DASHBOARD — self check\n"
          + clr())
    print(f"  {C.DIM}platform{clr()}        : {sys.platform} · "
          f"python {sys.version.split()[0]}")
    print(f"  {C.DIM}this device{clr()}     : "
          f"{C.GOLD}{local_ip() or 'unknown'}{clr()}")
    print(f"  {C.DIM}ping works{clr()}      : "
          + (C.LIME + "yes" if ping_available() else C.RED + "NO") + clr())
    gateway = get_gateway()
    print(f"  {C.DIM}indoor router{clr()}   : {C.CYAN}{gateway}{clr()} "
          f"{C.DIM}(LAN {subnet_of(gateway) or '?'}0/24 — expected "
          f"{INDOOR_ROUTER_IP}){clr()}")
    hops = traceroute_hops(INTERNET_PROBE, 4, 1)
    print(f"  {C.DIM}traceroute{clr()}      : "
          + (", ".join(hop or "*" for hop in hops) or "unavailable"))
    outdoor, how = discover_outdoor(gateway, subnet_of(gateway))
    print(f"  {C.DIM}outdoor router{clr()}  : "
          f"{C.ORANGE}{outdoor or 'not found'}{clr()} {C.DIM}[{how}]{clr()}")
    if outdoor and subnet_of(outdoor) == subnet_of(gateway):
        print(f"  {C.RED}warning: outdoor and indoor share a subnet — "
              f"check the WAN port wiring{clr()}")
    rtt = ping_ms(gateway, 3, 1)
    if rtt is not None:
        print(f"  {C.DIM}gateway RTT{clr()}     : {C.LIME}{rtt:.1f} ms{clr()}")
    else:
        print(f"  {C.DIM}gateway RTT{clr()}     : {C.RED}no answer{clr()}")
    table = arp_table()
    print(f"  {C.DIM}arp entries{clr()}     : {len(table)}")
    for ip, mac in sorted(table.items(),
                          key=lambda kv: ip_sort_key(kv[0]))[:12]:
        role = ("INDOOR" if ip == gateway
                else "OUTDOOR" if ip == outdoor else "")
        print(f"      {ip:<16} {mac:<18} {vendor_hint(mac) or '—':<12} {role}")
    print("\n  Run without --check for the live dashboard.\n")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv or "--selftest" in argv:
        return run_check()
    install_signal_handlers()
    enable_virtual_terminal()
    clear_screen()
    print(C.BOLD + C.MAGENTA + BANNER + clr())
    print(C.BOLD + C.CYAN + "      Detecting topology…\n" + clr())
    app = Dashboard()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())



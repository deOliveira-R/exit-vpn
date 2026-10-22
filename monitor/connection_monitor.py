#!/usr/bin/env python3
"""Connection monitor: what this Mac sees of the exit node, logged in enough detail to find
what makes connections drop. Runs until stopped (a launchd agent: see README.md); stdlib only.

One JSON line per record in LOG_DIR/connection-YYYY-MM-DD.jsonl (UTC dates), each with `ts`
(unix seconds) and `kind`:

  probe    every PROBE_EVERY s: a fresh HTTPS request over IPv4 and over IPv6 (curl's exit code
           and timings; the exit's address) and a DNS lookup of a unique name (time, error)
  flow     a long-lived download per family, throttled to a trickle, ended: how long it lived
           and how it ended (curl's exit code and error). An established connection that the
           exit or the path resets or stalls shows up here, which fresh probes do not see.
  tailnet  on change: the exit node's peer (online, direct address or DERP relay), the Mac's
           physical default routes
  node     each event of the exit's /v1/events stream (switches, failovers, IPv6 gate...),
           and its status every STATUS_EVERY s, compact
  gap      the monitor's clock jumped (the Mac slept)
  mark     a note from the owner (mark.sh "what happened")
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

NODE = os.environ.get("EXIT_NODE_NAME", "exit-proton")
API = f"http://{NODE}:{os.environ.get('API_PORT', '8080')}"
LOG_DIR = Path(os.environ.get("LOG_DIR", Path.home() / "Library/Logs/exit-vpn"))
PROBE_EVERY = float(os.environ.get("PROBE_EVERY", "10"))
STATUS_EVERY = float(os.environ.get("STATUS_EVERY", "60"))
FLOW_RATE = os.environ.get("FLOW_RATE", "256")   # bytes/s per long-lived flow
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"
FLOW_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
CURL = "/usr/bin/curl"
TAILSCALE = os.environ.get("TAILSCALE", "/usr/local/bin/tailscale")
TIMINGS = "%{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer} %{time_total}"

_lock = threading.Lock()


def write(kind: str, **data) -> None:
    rec = {"ts": round(time.time(), 3), "kind": kind, **data}
    path = LOG_DIR / f"connection-{time.strftime('%Y-%m-%d', time.gmtime(rec['ts']))}.jsonl"
    with _lock, path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def forever(name: str, fn) -> None:
    """Run fn in a loop on its own thread; an exception is logged, then fn runs again."""
    def loop():
        while True:
            try:
                fn()
            except Exception as e:   # noqa: BLE001 — the monitor must outlive its own bugs
                write("monitor_error", where=name, error=repr(e))
                time.sleep(5)
    threading.Thread(target=loop, name=name, daemon=True).start()


# Fresh requests --------------------------------------------------------------------------

def trace(family: str) -> dict:
    p = subprocess.run([CURL, f"-{family}", "-sS", "--max-time", "10", "-w", f"\n@@{TIMINGS}", TRACE],
                       capture_output=True, text=True)
    body, _, timing = p.stdout.rpartition("\n@@")
    fields = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
    t = [float(x) for x in timing.split()] if timing else []
    out = {"rc": p.returncode, "ip": fields.get("ip"), "loc": fields.get("loc")}
    if len(t) == 5:
        out.update(dns=t[0], connect=t[1], tls=t[2], first_byte=t[3], total=t[4])
    if p.returncode:
        out["error"] = p.stderr.strip()[-160:]
    return out


def dns() -> dict:
    name = f"m{uuid.uuid4().hex[:12]}.example.com"   # never cached: the resolver path every time
    result: dict = {}

    def lookup():
        try:
            socket.getaddrinfo(name, 443)
            result["answer"] = True
        except socket.gaierror as e:
            if e.args[0] == socket.EAI_NONAME:   # NXDOMAIN: the resolver answered
                result["answer"] = True
            else:
                result["error"] = e.args[1] if len(e.args) > 1 else str(e)
    t0 = time.monotonic()
    th = threading.Thread(target=lookup, daemon=True)
    th.start()
    th.join(10)
    result["seconds"] = round(time.monotonic() - t0, 3)
    if th.is_alive():
        result["error"] = "timeout"
    return result


def probes() -> None:
    last = time.time()
    while True:
        now = time.time()
        if now - last > PROBE_EVERY + 30:
            write("gap", seconds=round(now - last - PROBE_EVERY, 1))
        last = now
        res = {}
        ths = [threading.Thread(target=lambda f=f: res.__setitem__(f, trace(f))) for f in ("4", "6")]
        ths.append(threading.Thread(target=lambda: res.__setitem__("dns", dns())))
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        write("probe", v4=res["4"], v6=res["6"], dns=res["dns"])
        time.sleep(max(0.0, PROBE_EVERY - (time.time() - now)))


# Long-lived flows ------------------------------------------------------------------------

def flow(family: str):
    def run():
        t0 = time.time()
        p = subprocess.run([CURL, f"-{family}", "-sS", "-o", "/dev/null", "--limit-rate", FLOW_RATE,
                            "--speed-limit", "1", "--speed-time", "60", "-w", "%{size_download} %{remote_ip}",
                            FLOW_URL], capture_output=True, text=True)
        size, _, ip = p.stdout.partition(" ")
        lived = round(time.time() - t0, 1)
        write("flow", family=family, rc=p.returncode, seconds=lived, bytes=int(size or 0),
              remote=ip or None, error=p.stderr.strip()[-160:] or None)
        time.sleep(1 if lived > 30 else 10)   # a flow that cannot start: retry, but not in a spin
    return run


# Tailnet path ----------------------------------------------------------------------------

def tailnet() -> None:
    prev = None
    while True:
        cur: dict = {}
        try:
            st = json.loads(subprocess.run([TAILSCALE, "status", "--json"], capture_output=True,
                                           text=True, timeout=10).stdout)
            peer = next((p for p in st.get("Peer", {}).values() if p.get("ExitNode")), None)
            cur["exit_node"] = peer and peer.get("HostName")
            if peer:
                cur.update(online=peer.get("Online"), direct=bool(peer.get("CurAddr")),
                           relay=peer.get("Relay"))
            cur["backend"] = st.get("BackendState")
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
            cur["error"] = repr(e)
        routes = subprocess.run(["/usr/sbin/netstat", "-rn", "-f", "inet"], capture_output=True, text=True).stdout
        cur["physical"] = sorted(l.split()[3] for l in routes.splitlines()
                                 if l.startswith("default") and not l.split()[3].startswith("utun"))
        if cur != prev:
            write("tailnet", **cur)
            prev = cur
        time.sleep(5)


# The exit's own view ---------------------------------------------------------------------

def node_events() -> None:
    with urllib.request.urlopen(f"{API}/v1/events", timeout=60) as r:
        write("node", event="stream_connected")
        kind = None
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: ") and kind != "status":
                write("node", event=kind, data=json.loads(line[6:]))
    write("node", event="stream_closed")


def node_status() -> None:
    try:
        with urllib.request.urlopen(f"{API}/v1/status", timeout=10) as r:
            d = json.load(r)
        write("node", event="status", server=d.get("server"), health=d.get("health"),
              ipv6=d.get("ipv6"), exit_ip=d.get("exit_ip"), handshake_age=d.get("handshake_age"),
              errors=d.get("errors"), warnings=d.get("warnings"))
    except OSError as e:
        write("node", event="status", error=repr(e))
    time.sleep(STATUS_EVERY)


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    write("start", pid=os.getpid(), probe_every=PROBE_EVERY, flow_rate=FLOW_RATE)
    forever("probes", probes)
    forever("flow4", flow("4"))
    forever("flow6", flow("6"))
    forever("tailnet", tailnet)
    forever("node_events", node_events)
    forever("node_status", node_status)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()

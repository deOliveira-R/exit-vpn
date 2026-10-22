"""Ending the clients' connections through the former active tunnel, at a handover.

After the route switch, a connection opened through the old tunnel can never work again: its
packets now reach the other server, which ignores them. The kill switch refuses such packets
with a TCP reset (each connection's conntrack mark remembers its tunnel), but only once the
client sends one. A client waiting for data (a download, a streamed API reply) sends nothing
and would stall until its own timeout.

So each such connection gets a nudge: a bare ACK, as if from the server, with a sequence number
the client does not expect. TCP answers an unacceptable segment with an ACK of its own (RFC
9293 3.10.7.4), which the kill switch then refuses with a reset the client accepts (it carries
the sequence number the client itself just announced). The connections are listed with
`conntrack` (the low byte of the mark: 1 wg0, 2 wg1, as killswitch.nft sets it). IPv4 only.

The nudges carry the packet mark `NUDGE_MARK`, which killswitch.nft exempts from connection
tracking: tracked, a nudge's reply tuple would collide with the client's own connection and
NAT would rewrite its source port, so the client would not recognise it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import struct
from dataclasses import dataclass

log = logging.getLogger("exit_vpn")

TUNNEL_MARK = {"wg0": 1, "wg1": 2}
NUDGE_MARK = 0x10000000   # killswitch.nft: `meta mark 0x10000000 notrack` (tailscaled: 0xff0000)
SO_MARK = getattr(socket, "SO_MARK", 36)   # Linux (absent on macOS, where the tests run)
TAILNET = ipaddress.ip_network("100.64.0.0/10")
_FIELD = re.compile(r"(\w+)=(\S+)")


@dataclass(frozen=True)
class Flow:
    """A client's TCP connection as the client sees it: client → server."""
    client: str
    cport: int
    server: str
    sport: int


def parse_conntrack(text: str, mark: int) -> list[Flow]:
    """`conntrack -L -p tcp` lines → the established flows from tailnet clients whose mark's
    low byte is `mark`. The first src/dst/sport/dport are the original direction."""
    out = []
    for line in text.splitlines():
        if " ESTABLISHED " not in f" {line} ":
            continue
        f: dict[str, str] = {}
        for k, v in _FIELD.findall(line):
            f.setdefault(k, v)   # the original tuple comes first
        try:
            if int(f.get("mark", "0"), 0) & 0xFF != mark:
                continue
            client = ipaddress.ip_address(f["src"])
            if client.version != 4 or client not in TAILNET:
                continue
            out.append(Flow(f["src"], int(f["sport"]), f["dst"], int(f["dport"])))
        except (KeyError, ValueError):
            continue
    return out


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def nudge_packet(flow: Flow, seq: int) -> bytes:
    """An IPv4 packet server → client: a bare ACK with sequence number `seq` (and an
    acknowledgement number the client does not expect either)."""
    src, dst = socket.inet_aton(flow.server), socket.inet_aton(flow.client)
    ack = (seq + 0x40000000) & 0xFFFFFFFF
    tcp = struct.pack("!HHIIBBHHH", flow.sport, flow.cport, seq, ack, 5 << 4, 0x10, 1024, 0, 0)
    pseudo = src + dst + struct.pack("!BBH", 0, socket.IPPROTO_TCP, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", _checksum(pseudo + tcp)) + tcp[18:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 0, 0x4000, 64, socket.IPPROTO_TCP,
                     0, src, dst)   # the kernel fills in the IP checksum
    return ip + tcp


async def list_flows(old_ifname: str) -> list[Flow]:
    mark = TUNNEL_MARK.get(old_ifname)
    if mark is None:
        return []
    p = await asyncio.create_subprocess_exec(
        "conntrack", "-L", "-p", "tcp", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    return parse_conntrack(out.decode(errors="replace"), mark)


def send_nudges(flows: list[Flow]) -> int:
    if not flows:
        return 0
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW) as s:
        s.setsockopt(socket.SOL_SOCKET, SO_MARK, NUDGE_MARK)
        for flow in flows:
            try:
                s.sendto(nudge_packet(flow, int.from_bytes(os.urandom(4), "big")),
                         (flow.client, 0))
                sent += 1
            except OSError as e:
                log.warning("could not nudge %s:%d: %s", flow.client, flow.cport, e)
    return sent


async def end_flows(old_ifname: str) -> int:
    """Nudge every client connection still marked for `old_ifname`; returns how many.
    Best effort: a failure here leaves those connections to the clients' timeouts."""
    try:
        return send_nudges(await list_flows(old_ifname))
    except OSError as e:
        log.warning("could not end the connections of %s: %s", old_ifname, e)
        return 0

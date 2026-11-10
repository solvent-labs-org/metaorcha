"""C6: the addresses a pairing code advertises (``a=`` in ``metabot-pair:1``).

Configured ``phone_addresses`` win. Otherwise the host's own IPv4 addresses from
``ip -j -4 addr`` (Linux) or ``ifconfig`` (macOS), without loopback, link-local,
unspecified or multicast; the default-route address first, then LAN ranges, then
carrier-grade NAT (100.64/10, where mesh VPNs live). At most ``LIMIT`` addresses:
the phone tries them in order.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess

from . import paths

LIMIT = 4
CGNAT = ipaddress.ip_network((0x64400000, 10))   # the carrier-grade NAT range, 100.64/10, as an integer
TIMEOUT = 5


def usable(addr) -> bool:
    try:
        ip = ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    return (ip.version == 4 and not ip.is_loopback and not ip.is_link_local
            and not ip.is_unspecified and not ip.is_multicast)


def parse_ip_json(text: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for link in data if isinstance(data, list) else []:
        if not isinstance(link, dict):
            continue
        name = str(link.get("ifname") or "")
        for info in link.get("addr_info") or []:
            if isinstance(info, dict) and info.get("family") == "inet" and isinstance(info.get("local"), str):
                out.append((name, info["local"]))
    return out


_IF_HEAD = re.compile(r"^([A-Za-z0-9_.:-]+): ")
_INET = re.compile(r"^\s+inet (\d+\.\d+\.\d+\.\d+)\b")


def parse_ifconfig(text: str) -> list[tuple[str, str]]:
    out, name = [], ""
    for line in text.splitlines():
        m = _IF_HEAD.match(line)
        if m:
            name = m.group(1)
            continue
        m = _INET.match(line)
        if m:
            out.append((name, m.group(1)))
    return out


def _run(argv) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def interfaces() -> list[tuple[str, str]]:
    ip = paths.find_bin("ip")
    if ip:
        got = parse_ip_json(_run([ip, "-j", "-4", "addr"]))
        if got:
            return got
    ifconfig = paths.find_bin("ifconfig")
    if ifconfig:
        return parse_ifconfig(_run([ifconfig]))
    return []


def default_route() -> str | None:
    """The address the default route leaves from. A UDP connect() sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("203.0.113.1", 9))
            addr = s.getsockname()[0]
    except OSError:
        return None
    return addr if usable(addr) else None


def order(pairs, default=None) -> list[str]:
    seen, lan, cgnat = set(), [], []
    for _, addr in pairs:
        if addr in seen or not usable(addr):
            continue
        seen.add(addr)
        (cgnat if ipaddress.ip_address(addr) in CGNAT else lan).append(addr)
    out = lan + cgnat
    if default in out:
        out.remove(default)
        out.insert(0, default)
    return out


def candidates(configured=None, limit=LIMIT) -> list[str]:
    conf = []
    for a in configured or []:
        if isinstance(a, str) and usable(a) and a not in conf:
            conf.append(a)
    if conf:
        return conf[:limit]
    return order(interfaces(), default_route())[:limit]

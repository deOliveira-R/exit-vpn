"""Proton's server list (`vpn/v1/logicals`) as rows for the controller's catalog push."""

from __future__ import annotations

# `Features` bits (python-proton-vpn-api-core ServerFeatureEnum)
F_SECURE_CORE, F_TOR, F_P2P, F_STREAMING, F_IPV6 = 1, 2, 4, 8, 16


def catalog_rows(logicals: list[dict]) -> list[dict]:
    """One row per physical server (the WireGuard peer), deduplicated by (key, IP)."""
    rows, seen = [], set()
    for lg in logicals:
        feat = int(lg.get("Features") or 0)
        for ps in lg.get("Servers") or []:
            key, ip = ps.get("X25519PublicKey"), ps.get("EntryIP")
            if not key or not ip or (key, ip) in seen:
                continue
            seen.add((key, ip))
            rows.append({
                "name": lg.get("Name", ""), "country_code": lg.get("ExitCountry", ""),
                "city": lg.get("City") or "", "hostname": ps.get("Domain", ""), "ip": ip,
                "pubkey": key, "secure_core": bool(feat & F_SECURE_CORE),
                "tor": bool(feat & F_TOR), "port_forward": bool(feat & F_P2P),
                "stream": bool(feat & F_STREAMING), "ipv6": bool(feat & F_IPV6),
                "free": int(lg.get("Tier", 2)) == 0,
                "load": lg.get("Load"),
                "enabled": lg.get("Status") == 1 and ps.get("Status") == 1})
    return rows

"""Proton's levers: the dashboard's key features (D17). They are bound to the key, so
changing one means a new key. `RandomNAT` is the inverse of "Moderate NAT" (as in the
dashboard)."""

from __future__ import annotations

from ...model import Lever

LEVERS = [
    Lever("netshield", "NetShield", "NetShield", "choice", 0,
          {0: "no filter", 1: "block malware", 2: "block malware, ads and trackers"},
          "DNS filtering by Proton's resolver (10.2.0.1)"),
    Lever("accelerator", "VPN Accelerator", "VPN Accelerator", "bool", True,
          help="Proton's TCP split (Features.SplitTCP)"),
    Lever("moderate_nat", "Moderate NAT", "Moderate NAT", "bool", False,
          help="stable external port mapping (Features.RandomNAT = off)"),
    Lever("port_forward", "NAT-PMP (Port Forwarding)", "NAT-PMP (Port Forwarding)", "bool", False,
          help="NAT-PMP port forwarding on P2P servers"),
]


def features(values: dict) -> dict:
    """Lever values → the certificate's `Features`."""
    return {"NetShieldLevel": int(values["netshield"]),
            "SplitTCP": bool(values["accelerator"]),
            "RandomNAT": not values["moderate_nat"],
            "PortForwarding": bool(values["port_forward"])}


def values_from_features(f: dict) -> dict:
    out = {}
    if "NetShieldLevel" in f:
        out["netshield"] = int(f["NetShieldLevel"])
    if "SplitTCP" in f:
        out["accelerator"] = bool(f["SplitTCP"])
    if "RandomNAT" in f:
        out["moderate_nat"] = not f["RandomNAT"]
    if "PortForwarding" in f:
        out["port_forward"] = bool(f["PortForwarding"])
    return out


def option_lines(values: dict) -> dict[str, str]:
    """The dashboard's `# <Option> = <value>` .conf comments, in its order."""
    by_key = {lv.key: lv for lv in LEVERS}
    return {by_key[k].conf_label: by_key[k].format(values[k])
            for k in ("netshield", "moderate_nat", "port_forward", "accelerator") if k in values}

"""exitctl: `exitctl <provider> <command>` and `exitctl keys`.

Every command takes --json (machine output on stdout; the Phase 5 app drives exitctl
this way). Human messages and prompts go to stderr, so stdout stays parseable.
Private keys are never printed, in either mode.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import providers
from .controller import Controller, ControllerError
from .providers.base import IMPORT, NeedInput, Provider, ProviderError
from .wgkeys import Peer


class TtyPrompt:
    def ask(self, label: str, secret: bool = False) -> str:
        if secret:
            return getpass.getpass(f"{label}: ", stream=sys.stderr)
        print(f"{label}: ", end="", file=sys.stderr, flush=True)
        return sys.stdin.readline().strip()

    def info(self, message: str) -> None:
        print(message, file=sys.stderr)


def node_name(ctl: Controller) -> str:
    host = urlsplit(ctl.base_url).hostname or "exit"
    return os.environ.get("EXITCTL_NODE") or host.split(".")[0]


def device_name(ctl: Controller, slot: str) -> str:
    return f"{node_name(ctl)}-{slot}-{datetime.now(UTC):%Y%m%d}"


# Flows ----------------------------------------------------------------------------
async def slot_values(p: Provider, key_view: dict) -> dict:
    """The slot's current lever values: recorded options if this provider made the key,
    else what its .conf comments say (a dashboard-made key)."""
    vals = p.values_from_conf_options(key_view.get("options") or {})
    meta = key_view.get("meta") or {}
    if meta.get("provider") == p.name:
        vals |= meta.get("options") or {}
    return vals


async def find_cert(p: Provider, wg_public: str) -> dict | None:
    if not wg_public:
        return None
    return next((c for c in await p.list_keys() if c.get("wg_public_key") == wg_public), None)


async def rotate(p: Provider, ctl: Controller, slot: str, overrides: dict,
                 keep_old: bool = False, say=lambda _m: None) -> dict:
    status, cur = await ctl.status(), await ctl.key(slot)
    vals = {o.key: o.default for o in p.options()} | await slot_values(p, cur) | overrides
    old = await find_cert(p, cur.get("public_key") or "")
    if not (status.get("pubkey") and status.get("endpoint")):
        raise ProviderError("the controller reports no current server")
    peer = Peer(status.get("server") or "server", status["pubkey"], status["endpoint"])
    name = device_name(ctl, slot)
    say(f"creating {p.name} key {name} ({', '.join(f'{k}={v}' for k, v in vals.items())})")
    new = await p.create_key(name, vals, peer)
    say(f"uploading to slot {slot}; the controller tests it (up to a minute)")
    try:
        view = await ctl.put_key(slot, new.conf, new.meta)
    except ControllerError:
        say("the controller kept the previous key; deleting the new certificate")
        try:
            await p.delete_key(new.meta["cert_id"])
        except ProviderError as e:
            say(f"could not delete it: {e}")
        raise
    deleted = None
    if old and not keep_old and old["id"] != new.meta.get("cert_id"):
        try:
            await p.delete_key(old["id"])
            deleted = old["id"]
        except ProviderError as e:
            say(f"new key is live, but the old certificate could not be deleted: {e}")
    return {"slot": slot, "key": view, "deleted_old": deleted}


async def renew(p: Provider, ctl: Controller, slot: str, say=lambda _m: None) -> dict:
    cur = await ctl.key(slot)
    meta = cur.get("meta") or {}
    if meta.get("provider") != p.name or not meta.get("client_public_key"):
        cert = await find_cert(p, cur.get("public_key") or "")
        if not cert:
            raise ProviderError(f"slot {slot}'s key is not a {p.name} certificate on this account")
        meta = {"provider": p.name, "cert_id": cert["id"], "device_name": cert["device_name"],
                "options": cert["options"], "client_public_key": cert["client_public_key"]}
    before = meta.get("expires_at")
    meta = await p.renew_key(meta)
    meta["wg_public_key"] = cur.get("public_key")
    view = await ctl.put_meta(slot, meta)
    say(f"slot {slot}: expiry {fmt_time(before)} → {fmt_time(meta.get('expires_at'))}")
    return {"slot": slot, "key": view}


async def list_certs(p: Provider, ctl: Controller | None) -> list[dict]:
    certs = await p.list_keys()
    slots = {}
    if ctl:
        try:
            slots = {k["public_key"]: k["slot"] for k in await ctl.keys() if k.get("public_key")}
        except ControllerError:
            pass
    prefix = f"{node_name(ctl)}-" if ctl else None
    for c in certs:
        c["slot"] = slots.get(c.get("wg_public_key"))
        c["orphan"] = bool(prefix and c["device_name"].startswith(prefix) and not c["slot"])
    return certs


async def import_conf(ctl: Controller, slot: str, path: Path, provider: str) -> dict:
    meta = {"provider": provider, "device_name": path.stem, "created_at": int(time.time())}
    return {"slot": slot, "key": await ctl.put_key(slot, path.read_text(), meta)}


# Output ---------------------------------------------------------------------------
def fmt_time(ts) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d") if ts else "-"


def human(obj) -> str:
    if isinstance(obj, dict) and "slots" in obj:
        return "\n".join(human_slot(k) for k in obj["slots"])
    if isinstance(obj, dict) and "key" in obj:
        out = human_slot(obj["key"])
        if obj.get("deleted_old"):
            out += f"\n  deleted old certificate {obj['deleted_old']}"
        return out
    if isinstance(obj, list):
        return "\n".join(
            f"{c['device_name'] or '(unnamed)':32} {c['id'] or '?':20} expires {fmt_time(c['expires_at'])}"
            f"  {opts(c['options'])}" + (f"  [slot {c['slot']}]" if c.get("slot") else "")
            + ("  [orphan]" if c.get("orphan") else "") for c in obj) or "(none)"
    return json.dumps(obj, indent=1)


def human_slot(k: dict) -> str:
    m = k.get("meta") or {}
    exp = f"{k['expires_in_days']} days" if k.get("expires_in_days") is not None else "unknown"
    return (f"slot {k['slot']} ({k['role']}): {k['source']}, {m.get('provider', 'provider ?')}"
            f" {m.get('device_name', '')}\n  public key {k.get('public_key')}\n"
            f"  options {opts(k.get('options') or {})}\n  certificate expires in {exp}")


def opts(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items()) or "-"


def emit(args, obj) -> None:
    print(json.dumps(obj, indent=1) if args.json else human(obj))


# Parser ---------------------------------------------------------------------------
def add_option_flags(sp: argparse.ArgumentParser, p: Provider) -> None:
    for o in p.options():
        meta = "on|off" if o.kind == "bool" else "|".join(str(c) for c in o.choices)
        sp.add_argument(f"--{o.key.replace('_', '-')}", dest=f"opt_{o.key}", metavar=meta,
                        help=f"{o.label}: {o.help}" if o.help else o.label)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--url", help="controller URL (default $EXITCTL_URL or http://exit-proton:8080)")
    ap = argparse.ArgumentParser(prog="exitctl", parents=[common],
                                 description="VPN provider keys for the exit node (PLAN Phase 3b)")
    sub = ap.add_subparsers(dest="target", required=True)
    sub.add_parser("keys", parents=[common], help="the exit node's key slots")
    sub.add_parser("providers", parents=[common], help="available provider adapters")
    for name in providers.NAMES:
        p = providers.get(name)
        pp = sub.add_parser(name, parents=[common], help=p.title)
        cmds = pp.add_subparsers(dest="cmd", required=True)
        if IMPORT in p.capabilities:
            c = cmds.add_parser("import", parents=[common], help="upload a .conf into a slot")
            c.add_argument("--slot", required=True, choices=["a", "b"])
            c.add_argument("--provider-name", default="manual", help="recorded as the key's provider")
            c.add_argument("file", type=Path)
            continue
        cmds.add_parser("options", parents=[common], help="the levers keys can carry")
        lg = cmds.add_parser("login", parents=[common], help="log in (Keychain)")
        lg.add_argument("--stdin", action="store_true",
                        help="read {username,password,totp,hv_token,hv_type} as JSON from stdin")
        lg.add_argument("--totp", help="2FA code (to finish a login that stopped at 2FA)")
        cmds.add_parser("logout", parents=[common])
        cmds.add_parser("whoami", parents=[common])
        cmds.add_parser("list", parents=[common], help="certificates on the account")
        d = cmds.add_parser("delete", parents=[common], help="delete a certificate")
        d.add_argument("id")
        r = cmds.add_parser("rotate", parents=[common],
                            help="new key for a slot (levers: given, else the slot's current)")
        r.add_argument("--slot", required=True, choices=["a", "b"])
        r.add_argument("--keep-old", action="store_true", help="don't delete the old certificate")
        add_option_flags(r, p)
        n = cmds.add_parser("renew", parents=[common], help="extend a slot's certificate, same key")
        n.add_argument("--slot", required=True, choices=["a", "b"])
    return ap


async def run(args) -> int:
    ctl = Controller(args.url)
    say = (lambda _m: None) if args.json else (lambda m: print(m, file=sys.stderr))
    if args.target == "keys":
        emit(args, {"slots": await ctl.keys()})
        return 0
    if args.target == "providers":
        emit(args, [{"name": n, "title": providers.get(n).title,
                     "capabilities": sorted(providers.get(n).capabilities)} for n in providers.NAMES]
             if args.json else {"providers": list(providers.NAMES)})
        return 0
    p = providers.get(args.target)
    if args.cmd == "import":
        emit(args, await import_conf(ctl, args.slot, args.file, args.provider_name))
    elif args.cmd == "options":
        emit(args, [o.to_dict() for o in p.options()])
    elif args.cmd == "login":
        creds = json.loads(sys.stdin.read()) if args.stdin else {}
        if args.totp:
            creds["totp"] = args.totp
        prompt = None if (args.stdin or args.json) else TtyPrompt()
        emit(args, await p.login(prompt, creds))
    elif args.cmd == "logout":
        await p.logout()
        emit(args, {"logged_in": False})
    elif args.cmd == "whoami":
        emit(args, await p.whoami() or {"logged_in": False})
    elif args.cmd == "list":
        emit(args, await list_certs(p, ctl))
    elif args.cmd == "delete":
        await p.delete_key(args.id)
        emit(args, {"deleted": args.id})
    elif args.cmd == "rotate":
        overrides = {o.key: o.parse(getattr(args, f"opt_{o.key}")) for o in p.options()
                     if getattr(args, f"opt_{o.key}") is not None}
        emit(args, await rotate(p, ctl, args.slot, overrides, args.keep_old, say))
    elif args.cmd == "renew":
        emit(args, await renew(p, ctl, args.slot, say))
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = asyncio.run(run(args))
    except NeedInput as e:
        out = {"error": str(e), "need": e.what, **e.info}
        print(json.dumps(out) if args.json else f"exitctl: {e}", file=sys.stdout if args.json else sys.stderr)
        code = 3
    except (ProviderError, ControllerError) as e:
        print(json.dumps({"error": str(e)}) if args.json else f"exitctl: {e}",
              file=sys.stdout if args.json else sys.stderr)
        code = 2 if isinstance(e, ControllerError) and getattr(e, "status", 0) == 409 else 1
    sys.exit(code)

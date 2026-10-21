"""exitctl: `exitctl <provider> <command>` and `exitctl keys` / `exitctl providers`.

Every command takes --json: machine output on stdout (the app drives exitctl this way;
contracts/exitctl/ and its README list the shapes and exit codes). Human messages and prompts
go to stderr, so stdout stays parseable; errors in --json mode are JSON on stdout too.
Private keys are never printed, in either mode. The work is `keyservice.KeyService` and the
provider adapters; this module only parses, asks and prints.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from . import providers
from .exitnode import ExitNode
from .keyservice import KeyService, fmt_time
from .model import SLOTS, ExitctlError, NeedInput, NodeError
from .providers.base import (CREATE, DELETE, IMPORT, LIST, LOGIN, RENEW, SERVERS, Credentials,
                             Provider)

# Exit codes (contracts/README.md): 0 done, 1 error, 2 the exit node refused, 3 input needed
OK, ERROR, REFUSED, NEED_INPUT = 0, 1, 2, 3


# Asking the user (interactive login) ---------------------------------------------------
class TtyPrompt:
    def ask(self, label: str, secret: bool = False) -> str:
        if secret:
            return getpass.getpass(f"{label}: ", stream=sys.stderr)
        print(f"{label}: ", end="", file=sys.stderr, flush=True)
        return sys.stdin.readline().strip()

    def info(self, message: str) -> None:
        print(message, file=sys.stderr)


async def interactive_login(p: Provider, creds: Credentials, prompt: TtyPrompt):
    """Run login steps, asking for what each NeedInput names, until it is done."""
    while True:
        try:
            return await p.login(creds)
        except NeedInput as need:
            if need.kind == "password":
                creds = Credentials(username=creds.username or prompt.ask(f"{p.title} username"),
                                    password=prompt.ask(f"{p.title} password", secret=True))
            elif need.kind == "totp":
                creds = Credentials(totp=prompt.ask("2FA code"))
            elif need.kind == "human_verification":
                prompt.info(f"{p.title} asks for a CAPTCHA. Open this URL in your browser and "
                            f"solve it:\n  {need.info['url']}")
                prompt.ask("Then press Enter here")
                creds = Credentials(creds.username, creds.password, hv_token=need.info["token"],
                                    hv_type="captcha")
            else:
                raise


# Printing ------------------------------------------------------------------------------
def opts(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items()) or "-"


def human_slot(k: dict) -> str:
    m = k.get("meta") or {}
    exp = f"{k['expires_in_days']} days" if k.get("expires_in_days") is not None else "unknown"
    return (f"slot {k['slot']} ({k['role']}): {k['source']}, {m.get('provider', 'provider ?')}"
            f" {m.get('device_name', '')}\n  public key {k.get('public_key')}\n"
            f"  options {opts(k.get('options') or {})}\n  certificate expires in {exp}")


def human_certs(certs: list[dict]) -> str:
    return "\n".join(
        f"{c['device_name'] or '(unnamed)':32} {c['id'] or '?':20} expires {fmt_time(c['expires_at'])}"
        f"  {opts(c['options'])}" + (f"  [slot {c['slot']}]" if c.get("slot") else "")
        + ("  [orphan]" if c.get("orphan") else "") for c in certs) or "(none)"


def human_key_result(r: dict) -> str:
    out = human_slot(r["key"])
    if r.get("deleted_old"):
        out += f"\n  deleted old certificate {r['deleted_old']}"
    return out


class Output:
    """One result per command: JSON on stdout (--json), or a human rendering."""

    def __init__(self, json_mode: bool):
        self.json = json_mode

    def say(self, message: str) -> None:   # progress, humans only
        if not self.json:
            print(message, file=sys.stderr)

    def result(self, obj, human=None) -> None:
        if self.json:
            print(json.dumps(obj, indent=1))
        else:
            print(human(obj) if human else json.dumps(obj, indent=1))

    def error(self, e: Exception) -> None:
        if self.json:
            print(json.dumps(e.to_json() if isinstance(e, NeedInput) else {"error": str(e)}))
        else:
            print(f"exitctl: {e}", file=sys.stderr)


# Parser --------------------------------------------------------------------------------
def add_lever_flags(sp: argparse.ArgumentParser, p: Provider) -> None:
    for lv in p.levers():
        meta = "on|off" if lv.kind == "bool" else "|".join(str(c) for c in lv.choices)
        sp.add_argument(f"--{lv.key.replace('_', '-')}", dest=f"opt_{lv.key}", metavar=meta,
                        help=f"{lv.label}: {lv.help}" if lv.help else lv.label)


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS: a subcommand's copy of these flags must not reset one given before it
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--url", default=argparse.SUPPRESS,
                        help="controller URL (default $EXITCTL_URL or http://exit-proton:8080)")
    ap = argparse.ArgumentParser(prog="exitctl", parents=[common],
                                 description="VPN provider keys for the exit node")
    sub = ap.add_subparsers(dest="target", required=True)
    sub.add_parser("keys", parents=[common], help="the exit node's key slots")
    sub.add_parser("providers", parents=[common], help="available provider adapters")
    for name in providers.NAMES:
        p = providers.get(name)
        caps = p.capabilities
        cmds = sub.add_parser(name, parents=[common], help=p.title).add_subparsers(dest="cmd", required=True)
        if IMPORT in caps:
            c = cmds.add_parser("import", parents=[common], help="upload a .conf into a slot")
            c.add_argument("--slot", required=True, choices=SLOTS)
            c.add_argument("--provider-name", default="manual", help="recorded as the key's provider")
            c.add_argument("file", type=Path)
        if CREATE in caps:
            cmds.add_parser("options", parents=[common], help="the levers keys can carry")
        if LOGIN in caps:
            lg = cmds.add_parser("login", parents=[common], help="log in (Keychain)")
            lg.add_argument("--stdin", action="store_true",
                            help="read {username,password,totp,hv_token,hv_type} as JSON from stdin")
            lg.add_argument("--totp", help="2FA code (to finish a login that stopped at 2FA)")
            cmds.add_parser("logout", parents=[common])
            cmds.add_parser("whoami", parents=[common])
        if LIST in caps:
            cmds.add_parser("list", parents=[common], help="certificates on the account")
        if DELETE in caps:
            cmds.add_parser("delete", parents=[common], help="delete a certificate").add_argument("id")
        if CREATE in caps:
            r = cmds.add_parser("rotate", parents=[common],
                                help="new key for a slot (levers: given, else the slot's current)")
            r.add_argument("--slot", required=True, choices=SLOTS)
            r.add_argument("--keep-old", action="store_true", help="don't delete the old certificate")
            add_lever_flags(r, p)
        if RENEW in caps:
            n = cmds.add_parser("renew", parents=[common], help="extend a slot's certificate, same key")
            n.add_argument("--slot", required=True, choices=SLOTS)
        if SERVERS in caps:
            sv = cmds.add_parser("servers", parents=[common],
                                 help="the provider's live server list (--push: to the exit node)")
            sv.add_argument("--push", action="store_true")
            sv.add_argument("--revert", action="store_true",
                            help="make the exit node go back to its pinned list")
    return ap


# Commands ------------------------------------------------------------------------------
async def run(args, out: Output) -> int:
    node = ExitNode(getattr(args, "url", None))
    if args.target == "keys":
        out.result({"slots": await node.keys()}, lambda o: "\n".join(human_slot(k) for k in o["slots"]))
        return OK
    if args.target == "providers":
        rows = [{"name": n, "title": providers.get(n).title,
                 "capabilities": sorted(providers.get(n).capabilities)} for n in providers.NAMES]
        out.result(rows, lambda rs: "\n".join(f"{r['name']:8} {r['title']}" for r in rs))
        return OK
    p = providers.get(args.target)
    keys = KeyService(p, node, out.say)
    cmd = args.cmd
    if cmd == "import":
        out.result(await keys.import_conf(args.slot, args.file, args.provider_name), human_key_result)
    elif cmd == "options":
        out.result([lv.to_json() for lv in p.levers()])
    elif cmd == "login":
        creds = Credentials.from_json(json.loads(sys.stdin.read() or "{}")) if args.stdin else Credentials()
        if args.totp:
            creds = Credentials(creds.username, creds.password, args.totp, creds.hv_token, creds.hv_type)
        if args.stdin or args.json:
            account = await p.login(creds)       # one step; NeedInput says what comes next
        else:
            account = await interactive_login(p, creds, TtyPrompt())
        out.result(account.login_json(), lambda a: f"logged in as {a['account']}")
    elif cmd == "logout":
        await p.logout()
        out.result({"logged_in": False}, lambda _a: "logged out")
    elif cmd == "whoami":
        out.result((await p.account()).whoami_json())
    elif cmd == "list":
        out.result(await keys.certificates(), human_certs)
    elif cmd == "delete":
        await p.delete_certificate(args.id)
        out.result({"deleted": args.id})
    elif cmd == "rotate":
        overrides = {lv.key: lv.parse(getattr(args, f"opt_{lv.key}")) for lv in p.levers()
                     if getattr(args, f"opt_{lv.key}") is not None}
        out.result(await keys.rotate(args.slot, overrides, args.keep_old), human_key_result)
    elif cmd == "renew":
        out.result(await keys.renew(args.slot), human_key_result)
    elif cmd == "servers":
        if args.revert:
            out.result({"catalog": await node.delete_catalog()})
        else:
            out.result(await keys.servers(args.push))
    return OK


def exit_code(e: Exception) -> int:
    if isinstance(e, NeedInput):
        return NEED_INPUT
    if isinstance(e, NodeError) and e.refused:
        return REFUSED
    return ERROR


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.json = getattr(args, "json", False)
    out = Output(args.json)
    try:
        code = asyncio.run(run(args, out))
    except ExitctlError as e:
        out.error(e)
        code = exit_code(e)
    except (OSError, ValueError) as e:   # unreadable file, bad --stdin JSON, keyring trouble
        out.error(ExitctlError(f"{type(e).__name__}: {e}"))
        code = ERROR
    sys.exit(code)

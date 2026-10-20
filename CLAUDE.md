# exit-vpn (exit-proton)

ProtonVPN-backed Tailscale exit node (tailnet node `exit-proton`) on the Oracle `fin-intel`
VPS. **`PLAN.md` is the master document**: design decisions (D1–D19), phases, and a
"Phase N results" block after each finished phase. Host facts, access and constraints:
`local/exit-node-handoff.md` (gitignored; site-specific).

## Status (2026-10-20)

Phases 0–4 done and deployed; all acceptance suites green. Remaining, in PLAN order:
**3b** (`exitctl`: Mac-side Proton key/catalog helper, D18), **5** (macOS menu-bar app),
**6–7** (residential app connectors). Open improvement: issue #1 (Lightpanda as a lighter
prober tier). Optional: stop Chromium when the prober is idle (~410 MiB resident now).

## Layout

- `exit/` — exit container: `Dockerfile` (Debian trixie, s6-overlay, tailscale, WireGuard,
  nftables, Python venv), `rootfs/` (s6 services in `etc/s6-overlay/s6-rc.d`, user bundle in
  `user-bundles.d`, scripts in `usr/local/lib/exit-vpn`, `etc/exit-vpn/killswitch.nft`).
  Order: init-sysctl → init-killswitch → init-wg → route-keeper / tailscaled →
  tailscale-up → controller.
- `controller/` — Python package `exit_vpn` (FastAPI): catalog, wg, probes, health,
  failover, state, auth (whois), finder, api. Tests in `controller/tests`.
- `prober/` — prober container (own netns, key B tunnel `wg-probe`, curl_cffi + Debian
  `chromium --headless=new` over CDP), package `prober`, tests in `prober/tests`.
- `compose.yaml` (project `exit-vpn`, services `exit` and `prober`, network `exitnet`
  172.30.42.0/24: exit .2, prober .10), `.env.example`, `tests/acceptance/phase*.sh`.
- Tailnet policy lives in the separate **private** repo `deOliveira-R/tailnet-policy`
  (local clone `~/git/tailnet-policy`), GitOps: PR → CI `test`, merge → CI `apply`
  (federated identity, no stored secrets). Policy `tests` need concrete users as `src`
  (e.g. the owner's login), not autogroups.

## Conventions

- One phase at a time; a phase is done when its acceptance tests pass on the VPS; then
  record a "Phase N results" block in PLAN.
- Never commit secrets (`.env`, `secrets/`, keys) or site-specific values (IPs, tailnet
  name, personal details); use placeholders. The repo is **public**. Hooks:
  `git config core.hooksPath .githooks`; the pre-commit hook rejects strings listed in
  `local/denylist.txt` (gitignored). Note the ignore rule is `/local/` (anchored), so
  `exit/rootfs/usr/local` is tracked.
- Do not touch the `fin-intel` compose project, the host Docker daemon, or the host's
  Tailscale node. List every host-level change in the phase results.
- Avoid disruptive work at 23:30 UTC weekdays, Sun 06:00 UTC, and 04:00 UTC daily.
- Images pinned by digest, downloads checksummed, Python deps hash-locked
  (`uv pip compile <pkg>/pyproject.toml --universal --python-version 3.13 --generate-hashes
  -o <pkg>/requirements.lock`), healthchecks, `restart: unless-stopped`, rotated logs,
  minimal capabilities.
- Proton server catalog: `qdm12/gluetun-servers` → `pkg/servers/protonvpn.json`, **pinned to
  a commit** in `controller/src/exit_vpn/config.py` (a tampered catalog could redirect
  exit traffic); bump deliberately.

## Deploy and test

- VPS deploy dir `/opt/exit-node` (git checkout of this repo, root-owned; `.env` 0600,
  `secrets/` 0700 with `proton_{a,b,c}.conf` and `ts_authkey`). Docker needs sudo.
- **SSH over the tailnet** (public-IP sessions were reset twice during deploys):
  `ssh -o HostName=$(tailscale ip -4 fin-intel) -o HostKeyAlias=$(ssh -G fin-intel | awk '$1=="hostname"{print $2}') fin-intel`
- Deploy: `cd /opt/exit-node && sudo git pull --ff-only && sudo docker compose up -d --build`
  (push from the Mac first).
- Unit tests (Mac): `controller/.venv/bin/pytest -q controller`,
  `prober/.venv/bin/pytest -q prober` (venvs via `uv venv -p 3.13` + `uv pip install -e '.[test]'`).
- Acceptance: on the VPS `sudo bash tests/acceptance/phase1.sh`, `phase2-node.sh`; from the
  Mac (Proton app **off**; scripts select and clear the exit node themselves)
  `tests/acceptance/phase2-client.sh`, `phase3-client.sh`, `phase4-client.sh`.
- Controller API (owner's untagged devices only): `http://exit-proton:8080/v1/{status,servers,rotate,select,prefs,find,events}`.

## Gotchas learned

- Proton completes WireGuard handshakes even for unregistered keys; only traffic proves
  health. Fresh sessions drop TCP for 4–14 s (warm-up).
- A Proton public key is not unique (Secure Core entries reuse the exit's key): identify
  servers by (pubkey, endpoint IP).
- s6-overlay needs `/run` tmpfs with `exec`; `fwd` is an nft keyword; the kernel deletes
  table-100 routes when wg0 goes down (route-keeper restores them); tailscaled warns about
  `src_valid_mark` (read-only /proc/sys; compose sets it — cosmetic).
- Cloudflare's speed endpoint rejects 100 MB downloads (use 25 MB).

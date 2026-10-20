# exit-vpn (exit-proton)

ProtonVPN-backed Tailscale exit node (tailnet node `exit-proton`) on the Oracle `fin-intel`
VPS. Full design and phase plan: `PLAN.md` (the master document). Host facts, access and
constraints: `local/exit-node-handoff.md` (gitignored; site-specific).

## Conventions

- Work one phase at a time; a phase is done only when its acceptance tests pass on the VPS.
- Never commit secrets (`.env`, `secrets/`, keys) or site-specific values (IPs, tailnet
  name, personal details); use placeholders. Enable hooks with
  `git config core.hooksPath .githooks`: the pre-commit hook rejects strings listed in
  `local/denylist.txt` (gitignored).
- `.env` and `secrets/` live next to `compose.yaml` on the VPS, mode 600/700.
- Do not touch the `fin-intel` compose project, the host Docker daemon, or the host's
  Tailscale node. List every host-level change explicitly.
- Deploy dir on the VPS: `/opt/exit-node`. Access: `ssh fin-intel` (sudo for docker).
- Avoid disruptive work at 23:30 UTC weekdays, Sun 06:00 UTC, and 04:00 UTC daily.
- Images pinned by digest, healthchecks, `restart: unless-stopped`, rotated logs,
  minimal capabilities.
- Proton server catalog: `qdm12/gluetun-servers` → `pkg/servers/protonvpn.json`
  (moved out of the gluetun repo; see PLAN D12).

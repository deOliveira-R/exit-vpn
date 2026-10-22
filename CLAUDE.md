# exit-vpn (exit-proton)

ProtonVPN-backed Tailscale exit node (tailnet node `exit-proton`) on the Oracle `fin-intel`
VPS. **`PLAN.md` is the master document**: design decisions (D1–D19), phases, and a
"Phase N results" block after each finished phase. Host facts, access and constraints:
`local/exit-node-handoff.md` (gitignored; site-specific).

## Status (2026-10-22)

Phases 0–5d (incl. 3b, 3b.2, 5a, 5b), R (architecture pass), D (published node images; the VPS runs
node-v1.2.7 from GHCR), RT (tests by concept) and V6 (IPv6 through the exit, behind the IPv6 gate)
done and deployed; all acceptance suites green. Phase Q-1 (standby tunnel wg1, handover with
reset of the old connections) is done; next Q-2 (quality measurement). IPv6 is off (`EXIT_IPV6=off`). The exit runs on a
live catalog pushed from Proton's API (`exitctl proton servers --push`; the app's launchd agent
`org.exit-vpn.catalog-push` refreshes it hourly). ExitControl is installed in `~/Applications` from GitHub Releases and updates itself (Sparkle). Remaining: **6–7** (residential app connectors). Open improvement: issue #1 (Lightpanda as a lighter
prober tier). Optional: stop Chromium when the prober is idle (~410 MiB resident now).

## Layout

`ARCHITECTURE.md` maps every component's concepts and connections; `contracts/` holds the wire
formats between them (samples checked by both sides' tests).

- `exit/`: exit container. `Dockerfile` (Debian trixie, s6-overlay, tailscale, WireGuard,
  nftables, Python venv), `rootfs/` (s6 services in `etc/s6-overlay/s6-rc.d`, user bundle in
  `user-bundles.d`, scripts in `usr/local/lib/exit-vpn`, `etc/exit-vpn/killswitch.nft`).
  Order: init-sysctl → init-killswitch → init-wg → route-keeper / tailscaled → tailscale-up →
  controller.
- `controller/`: Python package `exit_vpn` (FastAPI). Domain: `servers`, `catalog`, `prefs`, `state`,
  `selection`, `health`. Services: `service.ExitService`, `tunnel`, `events`, `slots`, `finder`,
  `catalog_source`. Adapters: `wg`, `probes`, `prober_client`, `auth`, `persistence`, `keys`, `api`.
  `node.build` wires it (tests too: `tests/harness.py`).
- `prober/`: prober container (own netns, key B tunnel `wg-probe`, curl_cffi + Debian `chromium
  --headless=new` over CDP). `probe.Prober`, `engines`, `classify`, `exitcheck`, `tunnel`, `keyfile`.
- `helper/`: `exitctl` (Mac). `model`, `keyservice`, `exitnode`, `sessions`, `providers/` (`base`
  interface, `proton/` package, `manual`), `cli` with `--json`. uv project (`uv.lock`);
  proton-core from git, pinned (never the PyPI `proton-core`).
- `mac/ExitControl/`: the menu-bar app. `ExitControlCore` (vocabulary, wire models, stores,
  ActionRunner, ProcessRunner; `swift test`) and the app target (AppContainer, views, Intents,
  Updater), built from `project.yml` (XcodeGen).
- `compose.yaml` (project `exit-vpn`, services `exit` and `prober` from `EXIT_IMAGE`/`PROBER_IMAGE`,
  network `exitnet` 172.30.42.0/24: exit .2, prober .10), `compose.build.yaml` (build from source),
  `.env.example`, `deploy/` (kit: `install.sh`, `rollout.sh`, README), `tests/acceptance/` (`client/`, `node/`, `lib.sh`),
  `monitor/` (the connection monitor: a launchd agent on the Mac logging what it sees of the exit).
- Tailnet policy lives in the separate **private** repo `deOliveira-R/tailnet-policy`
  (local clone `~/git/tailnet-policy`), GitOps: PR → CI `test`, merge → CI `apply`
  (federated identity, no stored secrets). Policy `tests` need concrete users as `src`
  (e.g. the owner's login), not autogroups.

## Conventions

- Licence: MIT (`LICENSE`), chosen to stay compatible with GPLv2/GPLv3 and permissive code.
  Third-party: `helper/` imports Proton's `python-proton-core` (GPLv3) as a dependency, not
  vendored. Our source stays MIT; a *distributed bundle* that includes proton-core (e.g. a
  packaged app) must be distributed under GPLv3 terms. Don't vendor GPL code into
  the MIT tree.

- One phase at a time; a phase is done when its acceptance tests pass on the VPS; then
  record a "Phase N results" block in PLAN.
- Name things (files, tests, scripts, comments) after concepts in the present code, never after
  phases or steps; PLAN.md alone keeps the phase history.
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
- Proton server catalog: a live list pushed by `exitctl` wins; the fallback is
  `qdm12/gluetun-servers` → `pkg/servers/protonvpn.json`, **pinned to a commit** in
  `controller/src/exit_vpn/config.py` (a tampered catalog could redirect exit traffic); bump
  deliberately. Both paths validate hostnames (`.protonvpn.net`), IPs and keys.

## Deploy and test

- VPS deploy dir `/opt/exit-node` (git checkout of this repo, root-owned; `.env` 0600,
  `secrets/` 0700 with `proton_{a,b,c}.conf` and `ts_authkey`). Docker needs sudo.
- Live keys are in the named volumes `exit-vpn_keys-a` / `exit-vpn_keys-b` (uploaded via
  `exitctl`) and mirrored to `secrets/proton_{a,b}.conf` (the bootstrap fallback), which is
  why the exit mounts `secrets/` read-write.
- **SSH over the tailnet** (public-IP sessions were reset twice during deploys):
  `ssh -o HostName=$(tailscale ip -4 fin-intel) -o HostKeyAlias=$(ssh -G fin-intel | awk '$1=="hostname"{print $2}') fin-intel`
- Deploy: published images, from the Mac: `deploy/rollout.sh <X.Y.Z>` (verifies provenance, pins
  digests in the host's `.env`, waits for healthy); see `deploy/README.md`. Release: tag
  `node-vX.Y.Z` (`node-release.yml`; never marked "latest", that is the app's feed). From a
  checkout instead: `sudo docker compose -f compose.yaml -f compose.build.yaml up -d --build`.
- Release: bump nothing by hand; commit, then `git tag -a vX.Y.Z -m … && git push origin vX.Y.Z`.
  `release.yml` builds, signs, publishes; installed apps update via Check for Updates. CI:
  `ci.yml` on pushes to main and PRs (`gh run list`).
- App: `mac/ExitControl` (SwiftPM core + XcodeGen app; `swift test`, `./build-app.sh`; see its README).
  Update the installed app without its UI: `mac/ExitControl/update-app.sh` (Bash sandbox off).
  Before an action that cuts the exit's traffic (a node rollout), `open 'exitvpn://direct?minutes=10'`;
  after it, `open exitvpn://exitnode`. `open exitvpn://servers|find|settings/<tab>`
  opens windows; `open --env EXITCONTROL_DEBUG=1 --stderr <log> build/app.noindex/ExitControl.app` logs actions and updates. exitctl for
  the app: `uv tool install --force --reinstall --python <Homebrew python3.13> ./helper` (uv's standalone
  Python aborts on libssl; `--reinstall`, else uv reuses a cached build of the same version).
- Tests: `TESTING.md` (kinds, rules, one table per component: file → concept). Unit tests (Mac):
  `controller/.venv/bin/pytest -q controller`, `prober/.venv/bin/pytest -q prober`,
  `(cd helper && .venv/bin/pytest -q)`, `(cd mac/ExitControl && swift test)` (venvs via `uv venv -p 3.13` +
  `uv pip install -e '.[test]'`).
- Acceptance (`tests/acceptance/README.md`): from the Mac `tests/acceptance/run-client.sh [suite ...]`
  (Proton app **off**; the Mac must use an exit node, the scripts restore it; `catalog`, `keys`,
  `secrets` need `helper/.venv/bin/exitctl proton login`, done by the owner in their own terminal:
  password/2FA/CAPTCHA prompts). On the VPS: `sudo bash tests/acceptance/node/{tunnel,tailnet}.sh`.
- Catalog: `exitctl proton servers [--push|--revert]`. Keys: `exitctl keys`, `exitctl proton list|rotate --slot a|b [--netshield 0|1|2 ...]|renew --slot a|b`.
- Controller API (owner's untagged devices only): `http://exit-proton:8080/v1/{status,servers,rotate,select,prefs,find,events,keys,catalog}`.

## Gotchas learned

- Two exit tunnels share one address (Proton gives every config `10.2.0.2`): tell them apart
  by interface (SO_BINDTODEVICE, `oif` rules, tables 102/103), never by address. Bind the device
  *before* connect: httpx applies `socket_options` after it, so the SYN followed rule 110 into the
  active tunnel and every standby probe failed (the probes use their own sockets). Names too: the
  container's resolver forwards to 10.2.0.1 through the *active* tunnel, so each probe resolves its
  hosts through its own tunnel's DNS (else the standby fails whenever the active does). Table 100
  follows the active one; a single-tunnel kill-switch test must take both down, else the
  controller rightly hands over.
- A kill-switch reset reaches only a client that sends; an idle one (a streamed reply) is
  nudged at a handover (`flows.py`: a bare ACK from the server, untracked, else NAT rewrites
  its port). To test it, read like a stream (small receive buffer): `curl --limit-rate`
  buffers hundreds of KB and sees the reset minutes late.
- Proton completes WireGuard handshakes even for unregistered keys; only traffic proves
  health. Fresh sessions drop TCP for 4–14 s (warm-up).
- A Proton public key is not unique (Secure Core entries reuse the exit's key): identify
  servers by (pubkey, endpoint IP).
- IPv6 in the exit: never put an IPv6 address on wg0 (it becomes the source of every unbound
  socket, and rule 110 pulls them into the tunnel); IPv6 routes need `metric 1` to beat the
  unreachable routes (default metric 1024 > 1000); Tailscale clients drop ICMPv6 errors from
  the exit, so refuse with a TCP reset, and only new connections (`ct state new`): rejecting
  every packet resets clients' open streams, e.g. Claude Code's API responses mid-reply. Proton servers without the IPv6 flag drop IPv6 silently.
- s6-overlay needs `/run` tmpfs with `exec`; `fwd` is an nft keyword; the kernel deletes
  table-100 routes when wg0 goes down (route-keeper restores them); tailscaled warns about
  `src_valid_mark` (read-only /proc/sys; compose sets it — cosmetic).
- Cloudflare's speed endpoint rejects 100 MB downloads (use 25 MB).
- FastAPI's default 422 echoes the request body: the controller overrides it (keys!).
- proton-core raises the login CAPTCHA (422/9001) as a plain `ProtonAPIError`.
- tailscaled logs "SetPrivateKey called": grep for `PrivateKey *=`, not the bare word.
- App Intents (Shortcuts) run only for apps signed with an Apple-issued cert with a team ID
  (linkd: "requiresValidatedBundle"); `swift build` emits no App Intents metadata (use the
  XcodeGen/xcodebuild build). Signing needs Apple's WWDR G3 intermediate in the keychain.
- In this zsh, `log` is a builtin: use `/usr/bin/log show`. `codesign` with a keychain identity
  and `shortcuts run` need the Bash sandbox off (keychain/XPC access).
- LaunchServices sends `exitvpn://` and Shortcuts to whichever copy it registered (Shortcuts even
  showed the dev build's icon): keep one registered app. Builds go to `*.noindex` folders, which
  Spotlight skips, and `build-app.sh` unregisters them.

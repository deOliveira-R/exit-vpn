# ExitControl

Menu-bar app for the exit node (PLAN Phase 5, D21). A thin client: the controller
(`http://exit-proton:8080`) over HTTP + server-sent events, and `exitctl --json` for
provider actions (login, keys, levers, server list). MIT; it never bundles exitctl or
python-proton-core (GPLv3).

## Build

Swift package (macOS 14+, Xcode 16+); open the folder in Xcode, or:

    swift test                         # unit tests
    EXITCONTROL_LIVE=http://exit-proton:8080 swift test --filter Live   # read-only, live
    ./build-app.sh                     # build/ExitControl.app (universal, ad-hoc signed)
    cp -R build/ExitControl.app ~/Applications/

`EXITCONTROL_DEBUG=1` logs updates to stderr
(`open --env EXITCONTROL_DEBUG=1 --stderr /tmp/ec.log build/ExitControl.app`).

## exitctl

    uv tool install --python /opt/homebrew/opt/python@3.13/bin/python3.13 ./helper

Use Homebrew's Python: proton-core loads `libssl` through ctypes, and uv's standalone
Python finds only the system library, which macOS refuses to load (the process aborts).
The app looks for `~/.local/bin/exitctl`, then Homebrew and `/usr/local/bin`; Settings →
General takes another path.

## What it does

- Menu: current server, Secure Core route, exit IP, load, health; whether this Mac uses
  the exit node (read-only; choose it in the Tailscale app); new server in the same city /
  country / pinned regions; pinned regions and all countries; Secure Core and automatic
  failover toggles; Server Browser (⌘B), Find Server for URL (⌘F), Settings (⌘,).
- Server Browser: the live list with load and features, filters (country, P2P,
  streaming, IPv6, maintenance), connect to one exact server (failover then keeps its
  features: P2P stays P2P).
- Settings: pinned regions (drag to reorder), provider options (applied as a new key),
  account login (2FA, CAPTCHA), key slots (renew, new key), certificates (delete unused),
  server list (push now, revert, hourly launchd agent `org.exit-vpn.catalog-push`),
  event log, launch at login, notifications.

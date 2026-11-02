# ExitControl

Menu-bar app for the exit node. A thin client: the controller
(`http://exit-proton:8080`) over HTTP + server-sent events, and `exitctl --json` for
provider actions (login, keys, levers, server list). MIT; it never bundles exitctl or
python-proton-core (GPLv3).

## Install

Download `ExitControl-<version>.zip` from the latest GitHub Release, unzip, move
`ExitControl.app` to Applications, then right-click → Open once (signed, not notarized).
Updates: Sparkle checks at launch and once a day (Settings → General turns it off and has
"Check Now"); a
found update shows in the menu as "Version X available", one click downloads it (progress in
the menu), installs and relaunches. `open exitvpn://update` does the same without a click. From a
terminal, without the app: `./update-app.sh [version]` (default: the latest release) makes Sparkle's checks
(the zip's EdDSA signature, the code signature against the installed app's designated
requirement), then quits, replaces and relaunches the app in `~/Applications`.

## Release

Tag `vX.Y.Z` and push the tag: `.github/workflows/release.yml` builds the signed app
(`release.sh`), the appcast, and the GitHub Release. The app reads
`releases/latest/download/appcast.xml`; an update installs only with a valid EdDSA signature
(`SUPublicEDKey`). The build number is the commit count, so tag a new commit.

## Build

Swift package for the code and tests (macOS 14+, Xcode 16+); the app bundle is built by
Xcode from `project.yml` (XcodeGen: `brew install xcodegen`), because only Xcode extracts
the App Intents metadata Shortcuts needs.

    swift test                         # unit tests
    EXITCONTROL_LIVE=http://exit-proton:8080 swift test --filter Live   # read-only, live
    ./build-app.sh                     # build/app.noindex/ExitControl.app (universal)

Signing: without `Local.xcconfig` the app is ad-hoc signed and everything works except
Shortcuts (macOS runs App Intents only for Apple-issued signatures with a team ID). For a
signed build, sign in to Xcode with an Apple ID (a free Personal Team is enough), create an
Apple Development certificate (Settings → Accounts → Manage Certificates), and add the
gitignored `Local.xcconfig`:

    DEVELOPMENT_TEAM = <team ID>       # the certificate's OU
    CODE_SIGN_IDENTITY = Apple Development

If `codesign` fails with `errSecInternalComponent`, Apple's WWDR G3 intermediate is missing
from the keychain (apple.com/certificateauthority, `AppleWWDRCAG3.cer`), or the shell cannot
show the keychain prompt.

`exitvpn://` URLs:
- **Windows:** `servers`, `find`, `settings/general|regions|connection|account|events`.
- **Actions:** `rotate?scope=city|country|any`, `connect?country=…&city=…`, `connect?server=CH%23821`, `securecore?on=1|0`, `refresh` and `update` (checks for an app update and installs it, relaunching; `update-app.sh` does the same without the app). They run queued, in the order sent. `direct?minutes=10` (1–60) takes this Mac off the exit node for that long, as the menu's "Go Direct" does, and `exitnode` puts it back now: for work on the exit node itself that cuts its traffic.

Shortcuts actions: Rotate Exit Server, Connect Exit to Region, Exit Status. They share that queue; menu clicks are refused while something runs.

How the app is built (vocabulary, stores, ActionRunner, AppContainer): see
[ARCHITECTURE.md](../../ARCHITECTURE.md).

`EXITCONTROL_DEBUG=1` also logs actions, URLs and events to stderr (always to Console.app, subsystem
`org.exit-vpn.ExitControl`)
(`open --env EXITCONTROL_DEBUG=1 --stderr /tmp/ec.log build/app.noindex/ExitControl.app`). The build
folder ends in `.noindex` so Spotlight never registers a dev build next to the installed app.

## exitctl

    uv tool install --python /opt/homebrew/opt/python@3.13/bin/python3.13 ./helper

Use Homebrew's Python: proton-core loads `libssl` through ctypes, and uv's standalone
Python finds only the system library, which macOS refuses to load (the process aborts).
The app looks for `~/.local/bin/exitctl`, then Homebrew and `/usr/local/bin`; Settings →
General takes another path.

## What it does

- Menu: current server, Secure Core route, exit IP, load, health; whether this Mac uses
  the exit node, kept on: when this Mac has none, the app selects it again within seconds
  (Settings → General; "Go Direct for 10 Minutes" for a captive portal); new server in the same city /
  country / pinned regions; pinned regions and all countries; Security Options: automatic
  failover, Secure Core and the provider's options (NetShield level, Moderate NAT, NAT-PMP,
  VPN Accelerator; each change makes new keys for both exit tunnels, the standby's first);
  Server Browser (⌘B), Find Server for URL (⌘F), an available update, Settings (⌘,).
- Server Browser: the live list with load and features, filters (country, P2P,
  streaming, IPv6, maintenance), connect to one exact server (failover then keeps its
  features: P2P stays P2P).
- Settings: pinned regions (drag to reorder), provider options (applied as a new key),
  account login (2FA, CAPTCHA), key slots (renew, new key), certificates (delete unused),
  server list (push now, revert, hourly launchd agent `org.exit-vpn.catalog-push`),
  event log, the controller's status JSON, launch at login, notifications.

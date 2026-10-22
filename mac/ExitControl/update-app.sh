#!/usr/bin/env bash
# Update the installed app from the command line: ./update-app.sh [version]
# (default: the latest release). The same checks Sparkle makes, without its UI: the zip's
# EdDSA signature from the release's appcast against the installed app's SUPublicEDKey, and
# the new app's code signature against the installed app's designated requirement. Then the
# app is quit, replaced and relaunched. Needs: gh (logged in), python3 with `cryptography`.
set -euo pipefail
app=${EXITCONTROL_APP:-$HOME/Applications/ExitControl.app}
repo=${GITHUB_REPOSITORY:-$(cd "$(dirname "$0")" && gh repo view --json nameWithOwner -q .nameWithOwner)}
plist() { /usr/libexec/PlistBuddy -c "Print $2" "$1/Contents/Info.plist"; }
[ -d "$app" ] || { echo "no app at $app" >&2; exit 1; }
tag=${1:+v${1#v}}; tag=${tag:-$(gh release view -R "$repo" --json tagName -q .tagName)}
have=$(plist "$app" CFBundleShortVersionString)
[ "v$have" = "$tag" ] && { echo "already on $have"; exit 0; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
gh release download "$tag" -R "$repo" -D "$work" -p 'ExitControl-*.zip' -p appcast.xml
zip=$(echo "$work"/ExitControl-*.zip)
python3 -I - "$work/appcast.xml" "$zip" "$(plist "$app" SUPublicEDKey)" <<'PY'
import base64, sys, xml.etree.ElementTree as ET
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
appcast, zip_path, pub = sys.argv[1:]
enc = ET.parse(appcast).find(".//enclosure")
sig = enc.get("{http://www.andymatuschak.org/xml-namespaces/sparkle}edSignature")
data = open(zip_path, "rb").read()
assert int(enc.get("length")) == len(data), "the zip's length differs from the appcast's"
Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(base64.b64decode(sig), data)
print("EdDSA signature ok")
PY
ditto -x -k "$zip" "$work/new"
new=$work/new/ExitControl.app
req=$(codesign -d -r- "$app" 2>&1 | sed -n 's/^designated => //p')
codesign --verify --deep --strict -R="$req" "$new"
echo "code signature ok: $(plist "$new" CFBundleShortVersionString) ($(plist "$new" CFBundleVersion))"

# Quit (Sparkle's installer too, if one is stuck waiting), swap, relaunch
osascript -e 'tell application id "org.exit-vpn.ExitControl" to quit' 2>/dev/null || true
for _ in $(seq 20); do pgrep -f "$app/Contents/MacOS/ExitControl" >/dev/null || break; sleep 0.5; done
pkill -f "$app/Contents/MacOS/ExitControl" 2>/dev/null || true
pkill -f "Autoupdate org.exit-vpn.ExitControl" 2>/dev/null || true
old=$work/old.app
mv "$app" "$old"
if ! ditto "$new" "$app"; then rm -rf "$app"; mv "$old" "$app"; echo "install failed; kept $have" >&2; exit 1; fi
open "$app"
echo "updated $have → $(plist "$app" CFBundleShortVersionString)"

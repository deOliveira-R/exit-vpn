#!/usr/bin/env bash
# Build ExitControl.app (release, universal) into mac/ExitControl/build/app.noindex/.
# Needs Xcode and XcodeGen (brew install xcodegen). Signed with Local.xcconfig's identity if
# present, else ad-hoc. Install a release from GitHub instead of copying this build.
set -euo pipefail
cd "$(dirname "$0")"
xcodegen generate --quiet
xcodebuild -project ExitControl.xcodeproj -scheme ExitControlApp -configuration Release \
  -derivedDataPath .build/xcode.noindex -destination 'generic/platform=macOS' build -quiet "$@"
# Keep LaunchServices from registering the build next to the installed app: it would send
# exitvpn:// URLs and Shortcuts to either copy, and Shortcuts showed the wrong copy's icon.
# Spotlight skips (and so never registers) anything inside a `.noindex` folder.
# Run a dev build with `open build/app.noindex/ExitControl.app`.
out=.build/xcode.noindex/Build/Products/Release/ExitControl.app
app=build/app.noindex/ExitControl.app
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
rm -rf "$app" && mkdir -p "$(dirname "$app")"
mv "$out" "$app"
"$lsregister" -u "$PWD/$out" 2>/dev/null || true
"$lsregister" -u "$PWD/$app" 2>/dev/null || true
echo "built $app"

#!/usr/bin/env bash
# Build ExitControl.app (release, universal, ad-hoc signed) into mac/ExitControl/build/.
# Needs Xcode and XcodeGen (brew install xcodegen). Install: cp -R build/ExitControl.app ~/Applications/
set -euo pipefail
cd "$(dirname "$0")"
xcodegen generate --quiet
xcodebuild -project ExitControl.xcodeproj -scheme ExitControlApp -configuration Release \
  -derivedDataPath .build/xcode -destination 'generic/platform=macOS' build -quiet "$@"
# Move (not copy) the app out of DerivedData and keep LaunchServices from registering the
# build: it would send exitvpn:// URLs (and Shortcuts) to whichever copy it registered,
# launching a second app next to the installed one. Run a dev build with `open build/…`.
out=.build/xcode/Build/Products/Release/ExitControl.app
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
rm -rf build/ExitControl.app && mkdir -p build
mv "$out" build/
"$lsregister" -u "$PWD/$out" 2>/dev/null || true
"$lsregister" -u "$PWD/build/ExitControl.app" 2>/dev/null || true
echo "built build/ExitControl.app"

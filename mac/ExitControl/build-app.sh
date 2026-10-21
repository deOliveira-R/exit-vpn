#!/usr/bin/env bash
# Build ExitControl.app (release, universal, ad-hoc signed) into mac/ExitControl/build/.
# Needs Xcode and XcodeGen (brew install xcodegen). Install: cp -R build/ExitControl.app ~/Applications/
set -euo pipefail
cd "$(dirname "$0")"
xcodegen generate --quiet
xcodebuild -project ExitControl.xcodeproj -scheme ExitControlApp -configuration Release \
  -derivedDataPath .build/xcode -destination 'generic/platform=macOS' build -quiet "$@"
rm -rf build/ExitControl.app && mkdir -p build
cp -R .build/xcode/Build/Products/Release/ExitControl.app build/
echo "built build/ExitControl.app"

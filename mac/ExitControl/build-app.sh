#!/usr/bin/env bash
# Build ExitControl.app (release, ad-hoc signed) into mac/ExitControl/build/.
# Install: cp -R build/ExitControl.app ~/Applications/  (or /Applications)
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release --arch arm64 --arch x86_64
bin=$(swift build -c release --arch arm64 --arch x86_64 --show-bin-path)/ExitControl
app=build/ExitControl.app
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin" "$app/Contents/MacOS/ExitControl"
cp Resources/Info.plist "$app/Contents/Info.plist"
codesign --force --sign - --options runtime --timestamp=none "$app"
echo "built $app"

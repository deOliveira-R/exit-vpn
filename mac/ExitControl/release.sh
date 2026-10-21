#!/usr/bin/env bash
# Release build (PLAN Phase 5c): ./release.sh <version> <build number>
# Builds the signed app, zips it, and writes an appcast whose single item points at the
# GitHub Release download and carries the EdDSA signature Sparkle checks.
# Needs: Local.xcconfig (signing), SPARKLE_ED_KEY_FILE (the private key file), GITHUB_REPOSITORY.
set -euo pipefail
cd "$(dirname "$0")"
version=${1:?version}; build=${2:?build number}
: "${SPARKLE_ED_KEY_FILE:?}" "${GITHUB_REPOSITORY:?}"
./build-app.sh MARKETING_VERSION="$version" CURRENT_PROJECT_VERSION="$build"
app=build/app.noindex/ExitControl.app
codesign --verify --deep --strict "$app"
[ "$(defaults read "$PWD/$app/Contents/Info" CFBundleShortVersionString)" = "$version" ]
out=build/release; rm -rf "$out"; mkdir -p "$out"
ditto -c -k --sequesterRsrc --keepParent "$app" "$out/ExitControl-$version.zip"
bin=.build/xcode.noindex/SourcePackages/artifacts/sparkle/Sparkle/bin
"$bin/generate_appcast" --ed-key-file "$SPARKLE_ED_KEY_FILE" \
  --download-url-prefix "https://github.com/$GITHUB_REPOSITORY/releases/download/v$version/" \
  --link "https://github.com/$GITHUB_REPOSITORY/releases/tag/v$version" \
  --maximum-versions 1 -o "$out/appcast.xml" "$out"
grep -q 'sparkle:edSignature' "$out/appcast.xml"
echo "release files in $out"

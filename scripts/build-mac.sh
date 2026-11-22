#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
xcodebuild -project apple/Spliff/Spliff.xcodeproj -scheme Spliff \
  -configuration Release -destination 'generic/platform=macOS' \
  -derivedDataPath build/macOS CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY=- build
printf '\nBuilt: %s/build/macOS/Build/Products/Release/Spliff.app\n' "$PWD"

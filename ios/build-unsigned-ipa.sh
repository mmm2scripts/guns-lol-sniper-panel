#!/usr/bin/env bash
# Builds an UNSIGNED GunsSniper.ipa. Needs a Mac with Xcode + XcodeGen (brew install xcodegen).
# The GitHub Actions workflow in .github/workflows/build-ipa.yml runs exactly this script.
set -euo pipefail
cd "$(dirname "$0")"

xcodegen generate

rm -rf build Payload GunsSniper-unsigned.ipa
xcodebuild \
  -project GunsSniper.xcodeproj \
  -scheme GunsSniper \
  -configuration Release \
  -sdk iphoneos \
  -destination 'generic/platform=iOS' \
  -derivedDataPath build \
  CODE_SIGN_IDENTITY="" CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO \
  build

APP="build/Build/Products/Release-iphoneos/GunsSniper.app"
[ -d "$APP" ] || { echo "Build output not found: $APP"; exit 1; }
mkdir Payload
cp -R "$APP" Payload/
zip -qry GunsSniper-unsigned.ipa Payload
rm -rf Payload
echo "Created $(pwd)/GunsSniper-unsigned.ipa"

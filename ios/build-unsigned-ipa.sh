#!/usr/bin/env bash
# Builds an UNSIGNED GunsSniper.ipa. Needs a Mac with Xcode + XcodeGen (brew install xcodegen).
# The GitHub Actions workflow in .github/workflows/build-ipa.yml runs exactly this script.
set -euo pipefail
cd "$(dirname "$0")"

xcodegen generate

# XcodeGen >= 2.44 writes the Xcode 16 project format by default (objectVersion 77),
# which Xcode 15.x refuses to open: "The project cannot be opened because it is in a
# future Xcode project file format (77)". project.yml pins options.projectFormat:
# xcode15_0 (objectVersion 60) on XcodeGen 2.45+; as a safety net, clamp the generated
# objectVersion so the project builds with any Xcode 15.0 or newer.
sed -i.bak -E 's/objectVersion = [0-9]+;/objectVersion = 60;/' GunsSniper.xcodeproj/project.pbxproj
rm -f GunsSniper.xcodeproj/project.pbxproj.bak

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

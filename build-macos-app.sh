#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_root"

swift build -c release --product SBS3ControlMac

app="$project_root/dist/SBS-3 SDR Control.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$project_root/.build/release/SBS3ControlMac" "$app/Contents/MacOS/SBS-3 SDR Control"

cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleDisplayName</key><string>SBS-3 SDR Control</string>
  <key>CFBundleExecutable</key><string>SBS-3 SDR Control</string>
  <key>CFBundleIdentifier</key><string>com.pbkm.sbs3control</string>
  <key>CFBundleName</key><string>SBS-3 SDR Control</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.7</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

codesign --force --deep --sign - "$app"
archive="$project_root/SBS-3-SDR-Control-macOS-arm64.zip"
rm -f "$archive"
ditto -c -k --sequesterRsrc --keepParent "$app" "$archive"
printf '%s\n' "Built: $app"
printf '%s\n' "Packaged: $archive"

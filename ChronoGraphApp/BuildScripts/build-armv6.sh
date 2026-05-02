#!/bin/bash
# build-armv6.sh — ChronArchive armv6 IPA builder
# Uses Xcode 4.3's clang (run via Rosetta) with the iOS 5 SDK to produce an
# armv6 binary deployable on iPhone 2G / 3G / 3GS running iOS 3.1–5.x.
# Does NOT require xcodebuild — calls clang directly to avoid the DVTFoundation
# ObjC-GC crash that prevents Xcode 4.3's IDE tools from running on modern macOS.
#
# Usage: cd ChronoGraphApp/BuildScripts && bash build-armv6.sh
# Override Xcode 4 path: XCODE4=/path/to/Xcode4.app bash build-armv6.sh

set -e
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRCDIR="$SCRIPT_DIR/../Source/ChronoGraph/ChronArchive"
EMBED_DIR="$SRCDIR/www"  # canonical source: edit www/pages/ directly
OUTPUT_DIR="$SCRIPT_DIR/output-armv6"

XCODE4="${XCODE4:-$HOME/Downloads/Xcode4.app}"
CLANG="$XCODE4/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"
SDK="$XCODE4/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS5.0.sdk"

echo "=== ChronArchive armv6 Builder (Xcode 4.3 clang via Rosetta) ==="
echo ""

if [[ ! -f "$CLANG" ]]; then
    echo "ERROR: clang not found at: $CLANG"
    echo "Set XCODE4=/path/to/Xcode4.app and retry."
    exit 1
fi
if [[ ! -d "$SDK" ]]; then
    echo "ERROR: iOS 5 SDK not found at: $SDK"
    exit 1
fi

CLANG_VER=$(arch -x86_64 "$CLANG" --version 2>&1 | head -1)
echo "Compiler: $CLANG_VER"
echo "SDK:      $SDK"

BUILD_DIR="$OUTPUT_DIR/build"
APP_DIR="$OUTPUT_DIR/Payload/ChronArchive.app"
rm -rf "$OUTPUT_DIR"
mkdir -p "$BUILD_DIR" "$APP_DIR"

# ── Step 1: Compile ───────────────────────────────────────────────────────────
echo ""
echo "[1/4] Compiling (armv6, iOS 3.1 minimum)..."
for SRC in main.m AppDelegate.m ViewController.m; do
    OBJ="$BUILD_DIR/${SRC%.m}.o"
    arch -x86_64 "$CLANG" \
        -arch armv6 \
        -isysroot "$SDK" \
        -miphoneos-version-min=3.1 \
        -fno-objc-arc \
        -fobjc-runtime=ios-3.0 \
        -I"$SRCDIR" \
        -c "$SRCDIR/$SRC" -o "$OBJ"
    echo "  OK: $SRC"
done

# ── Step 2: Link ──────────────────────────────────────────────────────────────
echo ""
echo "[2/4] Linking..."
arch -x86_64 "$CLANG" \
    -arch armv6 \
    -isysroot "$SDK" \
    -miphoneos-version-min=3.1 \
    -fobjc-runtime=ios-3.0 \
    -framework UIKit \
    -framework Foundation \
    -framework CoreGraphics \
    -framework AVFoundation \
    "$BUILD_DIR/main.o" \
    "$BUILD_DIR/AppDelegate.o" \
    "$BUILD_DIR/ViewController.o" \
    -o "$APP_DIR/ChronArchive"
echo "  OK: ChronArchive"

# ── Step 3: Assemble .app bundle ──────────────────────────────────────────────
echo ""
echo "[3/4] Assembling .app bundle..."

# www/ resources
mkdir -p "$APP_DIR/www"
cp -r "$EMBED_DIR/." "$APP_DIR/www/"
echo "  Copied www/"

# Icons
for ICON in Icon-29.png Icon-29@2x.png Icon-40.png Icon-40@2x.png \
            Icon-60.png Icon-60@2x.png Icon-60@3x.png \
            Icon-76.png Icon-76@2x.png \
            iTunesArtwork.png iTunesArtwork@2x.png; do
    [[ -f "$SRCDIR/$ICON" ]] && cp "$SRCDIR/$ICON" "$APP_DIR/$ICON"
done
echo "  Copied icons"

# Launch images
for LAUNCH in Default.png Default@2x.png Default-568h@2x.png Default-667h@2x.png Default-736h@3x.png; do
    [[ -f "$SRCDIR/$LAUNCH" ]] && cp "$SRCDIR/$LAUNCH" "$APP_DIR/$LAUNCH"
done
echo "  Copied launch images"

# Info.plist — armv6, iOS 3.1
cat > "$APP_DIR/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>ChronoGraph</string>
    <key>CFBundleExecutable</key>
    <string>ChronArchive</string>
    <key>CFBundleIdentifier</key>
    <string>com.chronarchive.app</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>ChronoGraph</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.9</string>
    <key>CFBundleVersion</key>
    <string>9</string>
    <key>LSRequiresIPhoneOS</key>
    <true/>
    <key>MinimumOSVersion</key>
    <string>3.1</string>
    <key>UIRequiredDeviceCapabilities</key>
    <array>
        <string>armv6</string>
    </array>
    <key>UISupportedInterfaceOrientations</key>
    <array>
        <string>UIInterfaceOrientationPortrait</string>
        <string>UIInterfaceOrientationLandscapeLeft</string>
        <string>UIInterfaceOrientationLandscapeRight</string>
    </array>
    <key>UIStatusBarHidden</key>
    <true/>
    <key>UIViewControllerBasedStatusBarAppearance</key>
    <false/>
    <key>CFBundleIconFile</key>
    <string>Icon-60.png</string>
    <key>CFBundleIconFiles</key>
    <array>
        <string>Icon-29</string>
        <string>Icon-29@2x</string>
        <string>Icon-40</string>
        <string>Icon-40@2x</string>
        <string>Icon-60</string>
        <string>Icon-60@2x</string>
        <string>Icon-60@3x</string>
    </array>
</dict>
</plist>
PLIST
echo "  Wrote Info.plist (armv6, iOS 3.1)"

# ── Step 4: Sign and package ──────────────────────────────────────────────────
echo ""
echo "[4/4] Ad-hoc signing and packaging..."
# Remove AppleDouble metadata files (._*) and clear xattrs before codesign.
find "$OUTPUT_DIR/Payload" -name '._*' -type f -delete 2>/dev/null || true
xattr -cr "$OUTPUT_DIR/Payload" 2>/dev/null || true

if ! codesign -f -s - "$APP_DIR"; then
    echo "ERROR: codesign failed for $APP_DIR"
    echo "Hint: run 'xattr -cr $OUTPUT_DIR/Payload' and retry."
    exit 1
fi
cd "$OUTPUT_DIR"
zip -r ChronArchive-armv6.ipa Payload/ > /dev/null
rm -rf Payload/ build/
cd "$SCRIPT_DIR"

echo ""
echo "=== Done! ==="
echo ""
echo "  IPA → $OUTPUT_DIR/ChronArchive-armv6.ipa"
echo ""
echo "Install via ipainstaller (SSH):"
echo "  scp output-armv6/ChronArchive-armv6.ipa root@<device-ip>:/tmp/"
echo "  ipainstaller /tmp/ChronArchive-armv6.ipa"

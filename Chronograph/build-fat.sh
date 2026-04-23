#!/bin/bash
# build-fat.sh — Combines armv6 + armv7 + armv7s + arm64 into a single fat IPA
# (armv6 = iPhone 2G/3G, armv7 = 3GS/4, armv7s = 4S/5/5c, arm64 = 5s/6/6+)
# Requires both build.sh and build-armv6.sh to have been run first.
# Usage: cd Chronograph && bash build-fat.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARMV7_IPA="$SCRIPT_DIR/output/ChronArchive.ipa"
ARMV6_IPA="$SCRIPT_DIR/output-armv6/ChronArchive-armv6.ipa"
FAT_DIR="$SCRIPT_DIR/output-fat"
WORK_DIR="$FAT_DIR/work"

echo "=== ChronArchive Fat IPA Builder (armv6 + armv7 + armv7s + arm64) ==="
echo ""

if [[ ! -f "$ARMV7_IPA" ]]; then
    echo "ERROR: armv7 IPA not found at $ARMV7_IPA"
    echo "Run:  bash build.sh"
    exit 1
fi
if [[ ! -f "$ARMV6_IPA" ]]; then
    echo "ERROR: armv6 IPA not found at $ARMV6_IPA"
    echo "Run:  bash build-armv6.sh"
    exit 1
fi

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/armv7" "$WORK_DIR/armv6"
mkdir -p "$FAT_DIR"

# ── Step 1: Extract both IPAs ─────────────────────────────────────────────────
echo "[1/4] Extracting IPAs..."
unzip -q "$ARMV7_IPA" -d "$WORK_DIR/armv7"
unzip -q "$ARMV6_IPA" -d "$WORK_DIR/armv6"

APP_NAME=$(ls "$WORK_DIR/armv7/Payload/")   # e.g. ChronArchive.app
ARMV7_APP="$WORK_DIR/armv7/Payload/$APP_NAME"
ARMV6_APP="$WORK_DIR/armv6/Payload/$APP_NAME"
FAT_APP="$WORK_DIR/Payload/$APP_NAME"

echo "  App bundle: $APP_NAME"

# ── Step 2: Merge binaries with lipo ─────────────────────────────────────────
echo ""
echo "[2/4] Creating fat binary..."
mkdir -p "$WORK_DIR/Payload"
cp -r "$ARMV7_APP" "$FAT_APP"   # use armv7 bundle as base (has iOS 7 resources)

BINARY_NAME="${APP_NAME%.app}"

# Include all slices so the fat IPA covers iPhone 2G/3G through iPhone 6/6+.
# If a very old iOS 3.x installer refuses to install the fat IPA due to the
# arm64 slice, use the dedicated armv6 IPA (output-armv6/) instead.
lipo -create \
    "$ARMV7_APP/$BINARY_NAME" \
    "$ARMV6_APP/$BINARY_NAME" \
    -output "$FAT_APP/$BINARY_NAME"

echo "  Architectures: $(lipo -archs "$FAT_APP/$BINARY_NAME")"

# ── Step 3: Update Info.plist ─────────────────────────────────────────────────
echo ""
echo "[3/4] Updating Info.plist..."
INFO="$FAT_APP/Info.plist"

# Lower minimum OS to 3.1 so armv6 devices can install
/usr/libexec/PlistBuddy -c "Set :MinimumOSVersion 3.1" "$INFO"

# Remove armv7-only capability requirement so armv6 devices aren't excluded
/usr/libexec/PlistBuddy -c "Delete :UIRequiredDeviceCapabilities" "$INFO" 2>/dev/null || true

# Add legacy top-level icon keys — iOS 3/4 only understands CFBundleIconFile /
# a flat CFBundleIconFiles array; the nested CFBundleIcons dict is iOS 5+ only.
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconFile"  "$INFO" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add   :CFBundleIconFile string Icon-60.png" "$INFO"

# Bump version
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 0.7" "$INFO"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion 7" "$INFO"

echo "  MinimumOSVersion → 3.1"
echo "  UIRequiredDeviceCapabilities → removed"
echo "  CFBundleIconFile → Icon-60.png"
echo "  Version → 0.7 (build 7)"

# ── Step 4: Sign and package ──────────────────────────────────────────────────
echo ""
echo "[4/4] Signing and packaging..."
xattr -cr "$FAT_APP"
codesign -f -s - "$FAT_APP"

cd "$WORK_DIR"
zip -qr "$FAT_DIR/chronograph.ipa" Payload/

# ── Copy manifest.plist (update IPA URL to fat build) ────────────────────────
MANIFEST_SRC="$SCRIPT_DIR/output/manifest.plist"
MANIFEST_DST="$FAT_DIR/chronograph.plist"
cp "$MANIFEST_SRC" "$MANIFEST_DST"
# Point the manifest at the IPA
/usr/libexec/PlistBuddy -c \
  "Set :items:0:assets:0:url https://beta.chronarchive.com/Chronarchive/Applications/Chronograph/chronograph.ipa" \
  "$MANIFEST_DST"

echo ""
echo "=== Done! ==="
echo ""
echo "  output-fat/chronograph.ipa"
echo "  output-fat/chronograph.plist"
echo "  Architectures: $(lipo -archs "$FAT_APP/$BINARY_NAME")"

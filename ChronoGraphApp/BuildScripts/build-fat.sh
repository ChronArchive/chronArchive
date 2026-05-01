#!/bin/bash
# build-fat.sh — Combines all available architecture slices into a single fat IPA.
#
# Architecture coverage:
#   armv6   = iPhone 2G / 3G / 3GS          (iOS 3.1–5.x)   → build-armv6.sh
#   armv7   = iPhone 3GS / 4                 (iOS 6.0+)       → build.sh
#   armv7s  = iPhone 4S / 5 / 5c             (iOS 6.0+)       → build.sh
#   arm64   = iPhone 5s through X / XR/XS    (iOS 7.0+)       → build-arm64.sh
#   arm64e  = iPhone XS and later (A12+)      (iOS 12.0+)      → build-arm64e.sh (optional)
#
# Required before running:
#   bash build.sh          → output/ChronArchive.ipa          (armv7+armv7s)
#   bash build-armv6.sh    → output-armv6/ChronArchive-armv6.ipa
#   bash build-arm64.sh    → output-arm64/ChronArchive-arm64.ipa
#
# Optional (add arm64e slice if available):
#   bash build-arm64e.sh   → output-arm64e/ChronArchive-arm64e.ipa
#
# Usage: cd ChronoGraphApp/BuildScripts && bash build-fat.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARMV7_IPA="$SCRIPT_DIR/output/ChronArchive.ipa"
ARMV6_IPA="$SCRIPT_DIR/output-armv6/ChronArchive-armv6.ipa"
ARM64_IPA="$SCRIPT_DIR/output-arm64/ChronArchive-arm64.ipa"
ARM64E_IPA="$SCRIPT_DIR/output-arm64e/ChronArchive-arm64e.ipa"
FAT_DIR="$SCRIPT_DIR/output-fat"
WORK_DIR="$FAT_DIR/work"

echo "=== ChronArchive Fat IPA Builder ==="
echo ""

# ── Validate required IPAs ────────────────────────────────────────────────────
if [[ ! -f "$ARMV7_IPA" ]]; then
    echo "ERROR: armv7+armv7s IPA not found at $ARMV7_IPA"
    echo "Run:  bash build.sh"
    exit 1
fi
if [[ ! -f "$ARMV6_IPA" ]]; then
    echo "ERROR: armv6 IPA not found at $ARMV6_IPA"
    echo "Run:  bash build-armv6.sh"
    exit 1
fi
if [[ ! -f "$ARM64_IPA" ]]; then
    echo "ERROR: arm64 IPA not found at $ARM64_IPA"
    echo "Run:  bash build-arm64.sh"
    exit 1
fi

# arm64e is optional
HAS_ARM64E=0
if [[ -f "$ARM64E_IPA" ]]; then
    HAS_ARM64E=1
    echo "  Found arm64e IPA — will include in fat binary."
else
    echo "  No arm64e IPA found (optional — run build-arm64e.sh to add iPhone XS+ native slice)."
fi

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/armv7" "$WORK_DIR/armv6" "$WORK_DIR/arm64"
[[ $HAS_ARM64E -eq 1 ]] && mkdir -p "$WORK_DIR/arm64e"
mkdir -p "$FAT_DIR"

# ── Step 1: Extract all IPAs ──────────────────────────────────────────────────
echo ""
echo "[1/4] Extracting IPAs..."
unzip -q "$ARMV7_IPA" -d "$WORK_DIR/armv7"
unzip -q "$ARMV6_IPA" -d "$WORK_DIR/armv6"
unzip -q "$ARM64_IPA" -d "$WORK_DIR/arm64"
[[ $HAS_ARM64E -eq 1 ]] && unzip -q "$ARM64E_IPA" -d "$WORK_DIR/arm64e"

APP_NAME=$(ls "$WORK_DIR/armv7/Payload/")
ARMV7_APP="$WORK_DIR/armv7/Payload/$APP_NAME"
ARMV6_APP="$WORK_DIR/armv6/Payload/$APP_NAME"
ARM64_APP="$WORK_DIR/arm64/Payload/$APP_NAME"
FAT_APP="$WORK_DIR/Payload/$APP_NAME"
BINARY_NAME="${APP_NAME%.app}"

echo "  App bundle: $APP_NAME"

# ── Step 2: Merge binaries with lipo ─────────────────────────────────────────
echo ""
echo "[2/4] Creating fat binary..."
mkdir -p "$WORK_DIR/Payload"
cp -r "$ARMV7_APP" "$FAT_APP"   # armv7 bundle as base

LIPO_INPUTS=(
    "$ARMV6_APP/$BINARY_NAME"
    "$ARMV7_APP/$BINARY_NAME"
    "$ARM64_APP/$BINARY_NAME"
)
[[ $HAS_ARM64E -eq 1 ]] && LIPO_INPUTS+=("$WORK_DIR/arm64e/Payload/$APP_NAME/$BINARY_NAME")

lipo -create "${LIPO_INPUTS[@]}" -output "$FAT_APP/$BINARY_NAME"

echo "  Architectures: $(lipo -archs "$FAT_APP/$BINARY_NAME")"

# ── Step 3: Update Info.plist ─────────────────────────────────────────────────
echo ""
echo "[3/4] Updating Info.plist..."
INFO="$FAT_APP/Info.plist"

# Set minimum OS to 3.1 so armv6 devices can install the fat IPA.
# iOS 3.x OTA installers may still reject the fat binary — in that case
# use the dedicated output-armv6/ChronArchive-armv6.ipa instead.
/usr/libexec/PlistBuddy -c "Set :MinimumOSVersion 3.1" "$INFO"

# Remove arch capability restriction so all devices can install
/usr/libexec/PlistBuddy -c "Delete :UIRequiredDeviceCapabilities" "$INFO" 2>/dev/null || true

# Flat icon key for iOS 3/4 (doesn't understand the nested CFBundleIcons dict)
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconFile" "$INFO" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string Icon-60.png" "$INFO"

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 0.9" "$INFO"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion 9" "$INFO"

echo "  MinimumOSVersion → 3.1"
echo "  UIRequiredDeviceCapabilities → removed"
echo "  CFBundleIconFile → Icon-60.png"
echo "  Version → 0.9 (build 9)"

# ── Step 4: Inject latest www/, sign, and package ────────────────────────────
echo ""
echo "[4/4] Injecting www/, signing, packaging..."

echo "  Injecting latest www/..."
rm -rf "$FAT_APP/www"
cp -r "$SCRIPT_DIR/../Source/ChronoGraph/ChronArchive/www" "$FAT_APP/www"

xattr -cr "$FAT_APP"
codesign -f -s - "$FAT_APP"

cd "$WORK_DIR"
zip -qr "$FAT_DIR/chronograph.ipa" Payload/

# ── Manifest ──────────────────────────────────────────────────────────────────
MANIFEST_SRC="$SCRIPT_DIR/output/manifest.plist"
MANIFEST_DST="$FAT_DIR/chronograph.plist"
if [[ -f "$MANIFEST_SRC" ]]; then
    cp "$MANIFEST_SRC" "$MANIFEST_DST"
    /usr/libexec/PlistBuddy -c \
      "Set :items:0:assets:0:url https://beta.chronarchive.com/Chronarchive/Applications/Chronograph/chronograph.ipa" \
      "$MANIFEST_DST"
fi

echo ""
echo "=== Done! ==="
echo ""
echo "  output-fat/chronograph.ipa   — Universal fat IPA"
echo "  output-fat/chronograph.plist — OTA manifest"
echo "  Architectures: $(lipo -archs "$FAT_APP/$BINARY_NAME")"
echo ""
echo "Dedicated per-tier IPAs:"
echo "  output-armv6/ChronArchive-armv6.ipa  — iPhone 2G/3G/3GS (iOS 3.1+)"
echo "  output/ChronArchive.ipa              — iPhone 3GS–5c    (iOS 6.0+, armv7+armv7s)"
echo "  output-arm64/ChronArchive-arm64.ipa  — iPhone 5s–XR/XS  (iOS 7.0+)"
if [[ $HAS_ARM64E -eq 1 ]]; then
    echo "  output-arm64e/ChronArchive-arm64e.ipa — iPhone XS+ native (iOS 12.0+)"
fi


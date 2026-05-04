#!/bin/bash
# build-arm64e.sh — ChronArchive arm64e IPA builder (PLACEHOLDER)
# Covers: iPhone XS (A12) and later — iPhone 11, 12, 13, 14, 15, 16, 17...
#         All A12 Bionic and later chips use the arm64e ABI.
#
# REQUIREMENTS: Xcode 11 or later (iOS 12+ SDK with arm64e support)
#   Download Xcode 13/14/15/16 from https://developer.apple.com/download/all/
#   Place it at ~/Downloads/Xcode13.app (or set XCODE_APP env var)
#
# Usage: XCODE_APP=~/Downloads/Xcode13.app bash build-arm64e.sh
#        cd ChronoGraphApp/BuildScripts && bash build-arm64e.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/../Source/ChronoGraph"
OUTPUT_DIR="$SCRIPT_DIR/output-arm64e"

# arm64e requires Xcode 11+; look for a newer Xcode first, fall back to Xcode.app
XCODE_APP="${XCODE_APP:-}"
if [[ -z "$XCODE_APP" ]]; then
    # Auto-detect: prefer numbered Xcode installs in Downloads (newer = higher number)
    for candidate in \
        "$HOME/Downloads/Xcode16.app" \
        "$HOME/Downloads/Xcode15.app" \
        "$HOME/Downloads/Xcode14.app" \
        "$HOME/Downloads/Xcode13.app" \
        "$HOME/Downloads/Xcode12.app" \
        "/Applications/Xcode.app" \
        "$HOME/Downloads/Xcode.app"; do
        if [[ -x "$candidate/Contents/Developer/usr/bin/xcodebuild" ]]; then
            XCODE_APP="$candidate"
            break
        fi
    done
fi

XCODEBUILD="$XCODE_APP/Contents/Developer/usr/bin/xcodebuild"

echo "=== ChronArchive arm64e Builder (iOS 12.0+, iPhone XS and later) ==="
echo ""

if [[ -z "$XCODE_APP" ]] || [[ ! -x "$XCODEBUILD" ]]; then
    echo "ERROR: No suitable Xcode found for arm64e builds."
    echo ""
    echo "Download Xcode 13 or later from:"
    echo "  https://developer.apple.com/download/all/"
    echo ""
    echo "Place it at ~/Downloads/Xcode13.app (or any numbered name) and retry."
    echo "Or: XCODE_APP=/path/to/Xcode.app bash build-arm64e.sh"
    exit 1
fi

DEVELOPER_DIR="$XCODE_APP/Contents/Developer"
export DEVELOPER_DIR

XCODE_VER=$("$XCODEBUILD" -version 2>/dev/null | head -1)
echo "Using: $XCODE_VER  ($XCODEBUILD)"

# Verify this Xcode is new enough (Xcode 11 = build 11xxx)
BUILD_NUM=$("$XCODEBUILD" -version 2>/dev/null | grep "Build version" | awk '{print $3}')
MAJOR_NUM=$(echo "$BUILD_NUM" | sed 's/[^0-9].*//')
if [[ -n "$MAJOR_NUM" ]] && [[ "$MAJOR_NUM" -lt 11 ]]; then
    echo "ERROR: arm64e requires Xcode 11+. Found Xcode $XCODE_VER."
    echo "Download a newer Xcode from https://developer.apple.com/download/all/"
    exit 1
fi

# ── Build ─────────────────────────────────────────────────────────────────────
echo ""
echo "[1/2] Building Release for iphoneos (arm64e, deployment target iOS 12.0)..."
cd "$PROJECT_DIR"
rm -rf build/
mkdir -p build/

"$XCODEBUILD" build \
    -project ChronArchive.xcodeproj \
    -target ChronArchive \
    -configuration Release \
    -sdk iphoneos \
    ARCHS="arm64e" \
    IPHONEOS_DEPLOYMENT_TARGET=12.0 \
    ONLY_ACTIVE_ARCH=NO \
    ENABLE_BITCODE=NO \
    CODE_SIGN_IDENTITY="" \
    CODE_SIGNING_REQUIRED=NO \
    CODE_SIGNING_ALLOWED=NO \
    CONFIGURATION_BUILD_DIR="$PWD/build/Release-iphoneos" \
    > build/build-arm64e.log 2>&1
BUILD_STATUS=$?

if [ $BUILD_STATUS -ne 0 ]; then
    echo ""
    echo "Build FAILED. Relevant errors:"
    grep -E "error:|Build FAILED" build/build-arm64e.log | head -20
    echo ""
    echo "Full log: $PROJECT_DIR/build/build-arm64e.log"
    exit 1
fi
echo "  Build succeeded."

# ── Package IPA ───────────────────────────────────────────────────────────────
echo ""
echo "[2/2] Packaging IPA..."

APP_PATH=$(find build/Release-iphoneos -maxdepth 1 -name "ChronArchive.app" 2>/dev/null | head -1)
if [ -z "$APP_PATH" ]; then
    echo "ERROR: ChronArchive.app not found in build output."
    exit 1
fi

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/Payload"
cp -r "$APP_PATH" "$OUTPUT_DIR/Payload/"

SRCDIR="$PROJECT_DIR"
for LAUNCH in Default.png Default@2x.png Default-568h@2x.png Default-667h@2x.png Default-736h@3x.png Default-812h@3x.png Default-844h@3x.png Default-852h@3x.png Default-874h@3x.png Default-896h@2x.png Default-896h@3x.png Default-912h@3x.png Default-926h@3x.png Default-932h@3x.png Default-956h@3x.png Default-960h@3x.png; do
    [[ -f "$SRCDIR/$LAUNCH" ]] && cp "$SRCDIR/$LAUNCH" "$OUTPUT_DIR/Payload/ChronArchive.app/$LAUNCH"
done
echo "  Injected launch images"

INFO="$OUTPUT_DIR/Payload/ChronArchive.app/Info.plist"
/usr/libexec/PlistBuddy -c "Set :MinimumOSVersion 12.0" "$INFO" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 1.0" "$INFO" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion 10" "$INFO" 2>/dev/null || true
echo "  MinimumOSVersion → 12.0"
echo "  Version → 1.0 (build 10)"

echo "  Stripping extended attributes..."
xattr -cr "$OUTPUT_DIR/Payload/ChronArchive.app"
echo "  Signing with ldid (jailbreak-compatible)..."
BINARY="$OUTPUT_DIR/Payload/ChronArchive.app/ChronArchive"
if command -v ldid &>/dev/null; then
    ldid -S "$BINARY"
    echo "  ldid -S done"
else
    codesign -f -s - "$OUTPUT_DIR/Payload/ChronArchive.app" 2>&1 | sed 's/^/    /'
fi

cd "$OUTPUT_DIR"
zip -r ChronArchive-arm64e.ipa Payload/ > /dev/null
rm -rf Payload/
cd "$SCRIPT_DIR"

echo ""
echo "=== Done! ==="
echo ""
echo "  IPA → $OUTPUT_DIR/ChronArchive-arm64e.ipa"
echo ""
echo "Covers: iPhone XS, XS Max, XR, 11, 11 Pro, SE (2nd/3rd gen),"
echo "        iPhone 12, 13, 14, 15, 16, 17 and all later models."

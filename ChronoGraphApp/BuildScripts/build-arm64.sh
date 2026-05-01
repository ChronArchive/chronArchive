#!/bin/bash
# build-arm64.sh — ChronArchive arm64 IPA builder
# Covers: iPhone 5s (A7), 6/6+ (A8), 6s/6s+ (A9), SE 1st gen (A9),
#         7/7+ (A10), 8/8+ (A11), X (A11), XR/XS/XS Max (A12)
#         and all iPad/iPod touch with arm64 (up to A12).
# Deployment target: iOS 7.0 (arm64 requires >= 7.0)
# Uses Xcode 12.5.1 at ~/Downloads/Xcode.app (iOS 14.5 SDK)
#
# For arm64e devices (iPhone XS and later / A12X and later), these devices
# run arm64 slices fine in compatibility mode, so this IPA covers them too
# until a dedicated arm64e build is added (requires a newer Xcode).
#
# Usage: cd ChronoGraphApp/BuildScripts && bash build-arm64.sh
# Override: XCODE_APP=/path/to/Xcode.app bash build-arm64.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/../Source/ChronoGraph"
OUTPUT_DIR="$SCRIPT_DIR/output-arm64"

XCODE_APP="${XCODE_APP:-$HOME/Downloads/Xcode.app}"
XCODEBUILD="$XCODE_APP/Contents/Developer/usr/bin/xcodebuild"

echo "=== ChronArchive arm64 Builder (iOS 7.0+, iPhone 5s and later) ==="
echo ""

if [[ ! -x "$XCODEBUILD" ]]; then
    echo "ERROR: xcodebuild not found at: $XCODEBUILD"
    echo "Set XCODE_APP=/path/to/Xcode.app and retry."
    exit 1
fi

DEVELOPER_DIR="$XCODE_APP/Contents/Developer"
export DEVELOPER_DIR

XCODE_VER=$("$XCODEBUILD" -version 2>/dev/null | head -1 || echo "Xcode 12.x")
echo "Using: $XCODE_VER  ($XCODEBUILD)"

# ── Build ─────────────────────────────────────────────────────────────────────
echo ""
echo "[1/2] Building Release for iphoneos (arm64, deployment target iOS 7.0)..."
cd "$PROJECT_DIR"
rm -rf build/
mkdir -p build/

"$XCODEBUILD" build \
    -project ChronArchive.xcodeproj \
    -target ChronArchive \
    -configuration Release \
    -sdk iphoneos \
    ARCHS="arm64" \
    IPHONEOS_DEPLOYMENT_TARGET=7.0 \
    ONLY_ACTIVE_ARCH=NO \
    ENABLE_BITCODE=NO \
    CODE_SIGN_IDENTITY="" \
    CODE_SIGNING_REQUIRED=NO \
    CODE_SIGNING_ALLOWED=NO \
    CONFIGURATION_BUILD_DIR="$PWD/build/Release-iphoneos" \
    > build/build-arm64.log 2>&1
BUILD_STATUS=$?

if [ $BUILD_STATUS -ne 0 ]; then
    echo ""
    echo "Build FAILED. Relevant errors:"
    grep -E "error:|Build FAILED" build/build-arm64.log | head -20
    echo ""
    echo "Full log: $PROJECT_DIR/build/build-arm64.log"
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

# Inject launch images
SRCDIR="$PROJECT_DIR"
for LAUNCH in Default.png Default@2x.png Default-568h@2x.png Default-667h@2x.png Default-736h@3x.png; do
    [[ -f "$SRCDIR/$LAUNCH" ]] && cp "$SRCDIR/$LAUNCH" "$OUTPUT_DIR/Payload/ChronArchive.app/$LAUNCH"
done
echo "  Injected launch images"

# Update Info.plist minimum version
INFO="$OUTPUT_DIR/Payload/ChronArchive.app/Info.plist"
/usr/libexec/PlistBuddy -c "Set :MinimumOSVersion 7.0" "$INFO" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 0.9" "$INFO" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion 9" "$INFO" 2>/dev/null || true
echo "  MinimumOSVersion → 7.0"
echo "  Version → 0.9 (build 9)"

echo "  Stripping extended attributes..."
xattr -cr "$OUTPUT_DIR/Payload/ChronArchive.app"
echo "  Ad-hoc signing..."
codesign -f -s - "$OUTPUT_DIR/Payload/ChronArchive.app" 2>&1 | sed 's/^/    /'

cd "$OUTPUT_DIR"
zip -r ChronArchive-arm64.ipa Payload/ > /dev/null
rm -rf Payload/
cd "$SCRIPT_DIR"

echo ""
echo "=== Done! ==="
echo ""
echo "  IPA → $OUTPUT_DIR/ChronArchive-arm64.ipa"
echo ""
echo "Covers: iPhone 5s, 6, 6+, 6s, 6s+, SE (1st gen), 7, 7+, 8, 8+, X, XR, XS, XS Max"
echo "and all equivalent iPads/iPods."
echo ""
echo "Note: arm64e devices (XS/XS Max and later) run this arm64 slice in compat mode."
echo "For full arm64e native support, build-arm64e.sh (to be added when Xcode 13+ is available)."

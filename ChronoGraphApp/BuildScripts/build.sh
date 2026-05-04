#!/bin/bash
# build.sh — ChronArchive iOS IPA builder
# Works on macOS Tahoe by extracting Xcode 12.5.1 from its .xip and using
# the embedded xcodebuild directly (bypasses the "not compatible" GUI check).
# Usage: cd ChronoGraphApp/BuildScripts && bash build.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_DIR="$SCRIPT_DIR/../Source/ChronoGraph"
WWW_DIR="$PROJECT_DIR/ChronArchive/www"
OUTPUT_DIR="$SCRIPT_DIR/output"

XCODE_APP="${XCODE_APP:-$HOME/Downloads/Xcode.app}"
XCODEBUILD="$XCODE_APP/Contents/Developer/usr/bin/xcodebuild"

echo "=== ChronArchive iOS Builder ==="
echo ""

# ── Step 0: Verify Xcode.app exists ──────────────────────────────────────────
if [[ ! -x "$XCODEBUILD" ]]; then
    echo "ERROR: xcodebuild not found at: $XCODEBUILD"
    echo ""
    echo "If Xcode.app is somewhere else, run:"
    echo "  XCODE_APP=/path/to/Xcode.app bash build.sh"
    exit 1
fi

# Accept the embedded license non-interactively (suppresses license prompt)
DEVELOPER_DIR="$XCODE_APP/Contents/Developer"
export DEVELOPER_DIR

XCODE_VER=$("$XCODEBUILD" -version 2>/dev/null | head -1 || echo "Xcode 12.5.1")
echo "Using: $XCODE_VER  ($XCODEBUILD)"

# ── Step 1: Build ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "[1/2] Building Release for iphoneos (armv7 + armv7s, deployment target 6.0)..."
# Dropping arm64 here: arm64 devices run iOS 7+ and will fall back to the armv7 slice.
# This lets us keep IPHONEOS_DEPLOYMENT_TARGET=6.0 so the IPA installs on iOS 6 (iPhone 4S).
# The fat build combines this with the armv6 slice from build-armv6.sh.
cd "$PROJECT_DIR"
rm -rf build/
mkdir -p build/

"$XCODEBUILD" build \
    -project ChronArchive.xcodeproj \
    -target ChronArchive \
    -configuration Release \
    -sdk iphoneos \
    ARCHS="armv7 armv7s" \
    IPHONEOS_DEPLOYMENT_TARGET=6.0 \
    ONLY_ACTIVE_ARCH=NO \
    ENABLE_BITCODE=NO \
    CODE_SIGN_IDENTITY="" \
    CODE_SIGNING_REQUIRED=NO \
    CODE_SIGNING_ALLOWED=NO \
    CONFIGURATION_BUILD_DIR="$PWD/build/Release-iphoneos" \
    > build/build.log 2>&1
BUILD_STATUS=$?

if [ $BUILD_STATUS -ne 0 ]; then
    echo ""
    echo "Build FAILED. Relevant errors:"
    grep -E "error:|Build FAILED" build/build.log | head -20
    echo ""
    echo "Full log: $PROJECT_DIR/build/build.log"
    exit 1
fi
echo "  Build succeeded."

# ── Step 3: Package IPA ──────────────────────────────────────────────────────
echo ""
echo "[2/2] Packaging IPA..."

APP_PATH=$(find build/Release-iphoneos \
    -maxdepth 1 -name "ChronArchive.app" 2>/dev/null | head -1)

if [ -z "$APP_PATH" ]; then
    echo "ERROR: ChronArchive.app not found in build output."
    exit 1
fi

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/Payload"
cp -r "$APP_PATH" "$OUTPUT_DIR/Payload/"

# Inject launch images (not in Xcode project references, copied manually)
SRCDIR="$PROJECT_DIR"
for LAUNCH in Default.png Default@2x.png Default-568h@2x.png Default-667h@2x.png Default-736h@3x.png Default-812h@3x.png Default-844h@3x.png Default-852h@3x.png Default-874h@3x.png Default-896h@2x.png Default-896h@3x.png Default-912h@3x.png Default-926h@3x.png Default-932h@3x.png Default-956h@3x.png Default-960h@3x.png; do
    [[ -f "$SRCDIR/$LAUNCH" ]] && cp "$SRCDIR/$LAUNCH" "$OUTPUT_DIR/Payload/ChronArchive.app/$LAUNCH"
done
echo "  Injected launch images"

INFO="$OUTPUT_DIR/Payload/ChronArchive.app/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 1.1" "$INFO" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion 11" "$INFO" 2>/dev/null || true
echo "  Version → 1.1 (build 11)"

# Ad-hoc sign the app so the Mach-O loader accepts it on the device.
# AppSync Unified / ipainstaller bypass the signature *check*, but the
# binary must still carry a code signature or the kernel refuses to load it.
echo "  Stripping extended attributes..."
xattr -cr "$OUTPUT_DIR/Payload/ChronArchive.app"
echo "  Signing with ldid (jailbreak-compatible)..."
if command -v ldid &>/dev/null; then
    ldid -S "$OUTPUT_DIR/Payload/ChronArchive.app/ChronArchive"
    echo "  ldid -S done"
else
    codesign -f -s - "$OUTPUT_DIR/Payload/ChronArchive.app" 2>&1 | sed 's/^/    /'
fi

cd "$OUTPUT_DIR"
zip -r ChronArchive.ipa Payload/ > /dev/null
rm -rf Payload/
cd "$SCRIPT_DIR"

echo ""
echo "=== Done! ==="
echo ""
echo "  IPA → $OUTPUT_DIR/ChronArchive.ipa"
echo ""
echo "Install via Veteris or AirDrop:"
echo "  • AirDrop the IPA to your iPhone 4S (iOS 6)"
echo "  • Or serve it locally: cd output && python3 -m http.server 8080"
echo "    then open http://<your-mac-ip>:8080/ChronArchive.ipa in Safari/Veteris"

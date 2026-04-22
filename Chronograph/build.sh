#!/bin/bash
# build.sh — ChronArchive iOS IPA builder
# Works on macOS Tahoe by extracting Xcode 12.5.1 from its .xip and using
# the embedded xcodebuild directly (bypasses the "not compatible" GUI check).
# Usage: cd xcode && bash build.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_DIR="$SCRIPT_DIR/ChronArchive"
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

# ── Step 1: Sync Chronograph embed files into www/ ───────────────────────────
echo ""
echo "[1/3] Syncing Chronograph app files..."
EMBED_DIR="$SCRIPT_DIR/chronograph-embed"
rm -rf "$WWW_DIR"
mkdir -p "$WWW_DIR"
cp -r "$EMBED_DIR/." "$WWW_DIR/"
echo "  Copied from: $EMBED_DIR"
echo "  Done."

# ── Step 2: Build ─────────────────────────────────────────────────────────────
echo ""
echo "[2/3] Building Release for iphoneos (armv7 + armv7s)..."
cd "$PROJECT_DIR"
rm -rf build/
mkdir -p build/

"$XCODEBUILD" build \
    -project ChronArchive.xcodeproj \
    -target ChronArchive \
    -configuration Release \
    -sdk iphoneos \
    ARCHS="armv7 armv7s arm64" \
    ONLY_ACTIVE_ARCH=NO \
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
echo "[3/3] Packaging IPA..."

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
SRCDIR="$PROJECT_DIR/ChronArchive"
for LAUNCH in Default.png Default@2x.png Default-568h@2x.png Default-667h@2x.png Default-736h@3x.png; do
    [[ -f "$SRCDIR/$LAUNCH" ]] && cp "$SRCDIR/$LAUNCH" "$OUTPUT_DIR/Payload/ChronArchive.app/$LAUNCH"
done
echo "  Injected launch images"

# Ad-hoc sign the app so the Mach-O loader accepts it on the device.
# AppSync Unified / ipainstaller bypass the signature *check*, but the
# binary must still carry a code signature or the kernel refuses to load it.
echo "  Stripping extended attributes..."
xattr -cr "$OUTPUT_DIR/Payload/ChronArchive.app"
echo "  Ad-hoc signing (codesign -f -s -)..."
codesign -f -s - "$OUTPUT_DIR/Payload/ChronArchive.app" 2>&1 | sed 's/^/    /'

cd "$OUTPUT_DIR"
zip -r ChronArchive.ipa Payload/ > /dev/null
rm -rf Payload/
cd "$SCRIPT_DIR"

echo ""
echo "=== Done! ==="
echo ""
echo "  IPA → $OUTPUT_DIR/ChronArchive.ipa"
echo ""
echo "Install via Veteris:"
echo "  • AirDrop the IPA to your iPhone 4"
echo "  • Or serve it locally: cd output && python3 -m http.server 8080"
echo "    then open http://<your-mac-ip>:8080/ChronArchive.ipa in Veteris"

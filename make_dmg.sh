#!/bin/bash

set -euo pipefail
cd "$(dirname "$0")"

APP="dist/Clop Batch.app"
OUT=""
VOLNAME=""
VERIFY=1
KEEP_STAGE=0

usage() {
    cat <<'EOF'
usage: ./make_dmg.sh ["<path/to/App.app>"] [options]

  --out PATH        where to write the .dmg (default: dist/<App name>.dmg)
  --volname NAME    mounted volume name (default: the app's name)
  --no-verify       skip `hdiutil verify` after creating the image
  --keep-staging    keep the temporary staging folder (debugging)
  -h, --help        show this help

Requires macOS: the image is created with hdiutil.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --out) OUT="${2:?--out needs a path}"; shift 2 ;;
        --volname) VOLNAME="${2:?--volname needs a name}"; shift 2 ;;
        --no-verify) VERIFY=0; shift ;;
        --keep-staging) KEEP_STAGE=1; shift ;;
        -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) APP="$1"; shift ;;
    esac
done

fail() { echo "error: $*" >&2; exit 1; }

if ! command -v hdiutil >/dev/null 2>&1; then
    cat >&2 <<'EOF'
error: hdiutil not found, so this is not macOS (or it is badly broken).

A .dmg is a macOS disk image and can only be produced on macOS. PyInstaller
cannot cross-compile the app either, so building the image anywhere else would
produce a file that looks fine and fails on the Mac. Run this on the target Mac:

    ./build_app.sh --dmg
EOF
    exit 1
fi

[ -d "$APP" ] || fail "no such app bundle: $APP  (build it first with ./build_app.sh)"
[ -f "$APP/Contents/Info.plist" ] || fail "$APP is not an app bundle (no Contents/Info.plist)"

APP_NAME="$(basename "$APP" .app)"
: "${VOLNAME:=$APP_NAME}"
: "${OUT:=dist/$(printf '%s' "$APP_NAME" | tr ' ' '-').dmg}"
OUT_DIR="$(dirname "$OUT")"
mkdir -p "$OUT_DIR"
OUT="$(cd "$OUT_DIR" && pwd)/$(basename "$OUT")"

echo "==> staging $APP_NAME"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/clop-dmg.XXXXXX")"
cleanup() {
    if [ "$KEEP_STAGE" -eq 1 ]; then
        echo "    staging kept: $STAGE"
    else
        rm -rf "$STAGE"
    fi
}
trap cleanup EXIT

if command -v ditto >/dev/null 2>&1; then
    ditto "$APP" "$STAGE/$APP_NAME.app"
else
    cp -R "$APP" "$STAGE/$APP_NAME.app"
fi

if command -v xattr >/dev/null 2>&1; then
    xattr -cr "$STAGE/$APP_NAME.app" 2>/dev/null || true
fi

ln -s /Applications "$STAGE/Applications"

SIZE_APP="$(du -sh "$APP" 2>/dev/null | awk '{print $1}')"
echo "    app: ${SIZE_APP:-?}, volume: $VOLNAME"
echo "==> creating $OUT"
hdiutil create \
    -volname "$VOLNAME" \
    -srcfolder "$STAGE" \
    -fs HFS+ \
    -format UDZO \
    -imagekey zlib-level=9 \
    -ov \
    -quiet \
    "$OUT"

[ -f "$OUT" ] || fail "hdiutil did not produce $OUT"

if [ "$VERIFY" -eq 1 ]; then
    echo "==> verifying"
    hdiutil verify "$OUT" >/dev/null || fail "the image failed verification"
    echo "    image verified"
fi

SIZE_DMG="$(du -h "$OUT" | awk '{print $1}')"
echo ""
echo "Built: $OUT ($SIZE_DMG)"
if command -v shasum >/dev/null 2>&1; then
    echo "SHA-256: $(shasum -a 256 "$OUT" | awk '{print $1}')"
fi
echo ""
echo "Give that file to the Mac. First launch: right-click the app -> Open,"
echo "because it is signed ad-hoc rather than notarised."

#!/bin/bash

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
MAKE_DMG=0

usage() {
    cat <<'EOF'
usage: ./build_app.sh [options]

  --dmg            also build a compressed, verified .dmg via make_dmg.sh
  --no-check       skip the post-build self-check of the merged script
  -h, --help       show this help

Environment:
  PYTHON=/path/to/python3   interpreter used to build (default: python3)

Must run on macOS: PyInstaller cannot cross-compile an app bundle, and the
.dmg needs hdiutil.
EOF
}

BUILD_CHECK=1
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --dmg) MAKE_DMG=1; shift ;;
        --no-check) BUILD_CHECK=0; shift ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

if [ "$(uname -s)" != "Darwin" ]; then
    cat >&2 <<'EOF'
error: this builds a macOS app, so it has to run on macOS.

PyInstaller cannot cross-compile: a bundle built on Linux or Windows will not
launch on a Mac, and a .dmg can only be made with hdiutil. Copy the repository
to the Mac (or clone it there) and run:

    ./build_app.sh --dmg
EOF
    exit 1
fi

command -v "$PYTHON" >/dev/null 2>&1 || \
    die "python3 not found. Install it (brew install python) or set PYTHON=/path/to/python3."

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 or newer is required (found $("$PYTHON" -V 2>&1))."

echo "==> building the single-file script"
if [ "$BUILD_CHECK" -eq 1 ]; then
    "$PYTHON" build.py --check
else
    "$PYTHON" build.py
fi

echo "==> preparing PyInstaller"
mkdir -p .build
if [ ! -x .build/venv/bin/pyinstaller ]; then
    "$PYTHON" -m venv .build/venv
    .build/venv/bin/pip install --quiet --upgrade pip
    .build/venv/bin/pip install --quiet "pyinstaller>=6.0"
fi

echo "==> bundling"
rm -rf "dist/Clop Batch.app" "dist/Clop Batch"
.build/venv/bin/pyinstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name "Clop Batch" \
    --osx-bundle-identifier com.local.clopbatch \
    --distpath dist \
    --workpath .build/pyinstaller \
    --specpath .build \
    dist/clop_batch_gui.py

APP="dist/Clop Batch.app"
[ -d "$APP" ] || die "PyInstaller did not produce $APP"

echo "==> signing (ad-hoc)"
codesign --force --deep --sign - "$APP" 2>/dev/null || \
    echo "    codesign failed; the app still runs locally, Gatekeeper may ask."

echo "==> smoke test"
BINARY="$APP/Contents/MacOS/Clop Batch"
if [ -x "$BINARY" ]; then
    "$BINARY" --help >/dev/null 2>&1 && echo "    the app answers --help" || \
        echo "    warning: the app did not answer --help; check it manually"
else
    echo "    warning: no executable at $BINARY"
fi

if [ "$MAKE_DMG" -eq 1 ]; then
    echo ""
    ./make_dmg.sh "$APP"
    exit 0
fi

echo ""
echo "Built: $APP"
echo ""
echo "Run it:      open \"$APP\""
echo "UI:          http://127.0.0.1:8765 (opened in your browser automatically)"
echo "First run:   right-click the app -> Open (signed ad-hoc, not notarised)."
echo "Move it:     drag it to /Applications; it is self-contained."
echo "Make a .dmg: ./build_app.sh --dmg"

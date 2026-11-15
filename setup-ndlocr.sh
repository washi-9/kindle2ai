#!/bin/sh
# Optional: install NDLOCR-Lite (National Diet Library) for vertical Japanese.
#
# kindle2ai itself needs no Python packages. NDLOCR-Lite does (onnxruntime,
# OpenCV, ...), so it lives in its own virtualenv under build/ and is used only
# when present. Measured against Aozora Bunko it has about a third of Live
# Text's errors on vertical text. Needs ~700MB of disk and Python 3.10+.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

REPO=https://github.com/ndl-lab/ndlocr-lite.git
# The version kindle2ai was measured against; bump deliberately.
COMMIT=a25d4564b4a8884fd9430d1eb416a2ce8d4d910b
SRC=build/ndlocr-lite
VENV=build/ndlocr-venv

PYTHON=
for candidate in python3.12 python3.11 python3.10 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 13))'; then
        PYTHON=$(command -v "$candidate")
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "NDLOCR-Lite needs Python 3.10-3.13 (onnxruntime has no wheels for newer)."
    echo "Install one with:  brew install python@3.12"
    exit 1
fi

echo "==> fetching NDLOCR-Lite ($COMMIT)"
if [ ! -d "$SRC/.git" ]; then
    git clone --filter=blob:none "$REPO" "$SRC"
fi
git -C "$SRC" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$SRC" checkout --quiet "$COMMIT"

echo "==> creating virtualenv with $PYTHON"
[ -x "$VENV/bin/python" ] || "$PYTHON" -m venv "$VENV"
# flet is only for NDLOCR-Lite's GUI.
grep -v '^flet' "$SRC/requirements.txt" > build/ndlocr-requirements.txt
"$VENV/bin/pip" install --quiet --disable-pip-version-check -r build/ndlocr-requirements.txt

echo "==> checking setup"
./bin/kindle2ai doctor || true

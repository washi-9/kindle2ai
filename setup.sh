#!/bin/sh
# kindle2ai setup: build the native helper and fetch the Japanese OCR models.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

echo "==> building native helper"
mkdir -p build
swiftc -O swift/KindleOCR.swift -o build/kindle-ocr
echo "    build/kindle-ocr"
swiftc -O tools/RenderSample.swift -o build/render-sample
echo "    build/render-sample (accuracy evaluation)"

echo "==> building the app"
APP=build/Kindle2AI.app
BIN="$APP/Contents/MacOS/Kindle2AI"
# Rebuild only when the app's sources changed: every rebuild gets a new ad-hoc
# signature, and macOS then silently stops honoring the Accessibility and
# Screen Recording permissions granted to the previous build.
HAS_ID=$(security find-identity -p codesigning 2>/dev/null | grep -c "Kindle2AI Local Signing" || true)
IS_ADHOC=$(codesign -dv "$APP" 2>&1 | grep -c "Signature=adhoc" || true)
if [ -x "$BIN" ] && [ -z "$(find swift/App -newer "$BIN" -type f)" ] &&
   ! { [ "$HAS_ID" -gt 0 ] && [ "$IS_ADHOC" -gt 0 ]; }; then
    echo "    $APP (up to date; not rebuilt, so its permissions stay valid)"
else
mkdir -p "$APP/Contents/MacOS"
swiftc -O -parse-as-library swift/App/Kindle2AIApp.swift -o "$APP/Contents/MacOS/Kindle2AI" 2>&1 \
    | grep -v "deprecated in macOS 14" | grep -E "error" || true
[ -x "$APP/Contents/MacOS/Kindle2AI" ] || { echo "    app build failed"; exit 1; }
cp swift/App/Info.plist "$APP/Contents/Info.plist"
# Sign with the local identity from tools/make-signing-identity.sh when it
# exists, so the permissions granted to the app survive rebuilds; otherwise
# ad-hoc, which macOS ties to this exact build.
if security find-identity -p codesigning 2>/dev/null | grep -q "Kindle2AI Local Signing"; then
    codesign --force --sign "Kindle2AI Local Signing" "$APP" >/dev/null
    echo "    signed with \"Kindle2AI Local Signing\" (permissions survive rebuilds)"
else
    codesign --force --sign - "$APP" >/dev/null 2>&1 || true
    echo "    ad-hoc signed: permissions must be re-granted after each rebuild"
    echo "    (run tools/make-signing-identity.sh once to avoid that)"
fi
echo "    $APP  (open it from Finder, or: open $APP)"
echo "    rebuilt: re-grant Accessibility / Screen Recording to Kindle2AI if asked"
fi

echo "==> vertical Japanese (縦書き) support"
if ! command -v tesseract >/dev/null 2>&1; then
    echo "    tesseract is not installed."
    echo "    Vertical Japanese books need it; horizontal books do not."
    echo "    Install with:  brew install tesseract"
else
    mkdir -p build/tessdata
    for model in jpn jpn_vert; do
        if [ -s "build/tessdata/$model.traineddata" ]; then
            echo "    $model.traineddata (already present)"
            continue
        fi
        echo "    downloading $model.traineddata"
        curl -fsSL -o "build/tessdata/$model.traineddata" \
            "https://github.com/tesseract-ocr/tessdata_best/raw/main/$model.traineddata"
    done
    for model in eng osd; do
        src=$(dirname "$(command -v tesseract)")/../share/tessdata/$model.traineddata
        [ -f "$src" ] && cp -n "$src" build/tessdata/ 2>/dev/null || true
    done
fi

echo "==> checking setup"
./bin/kindle2ai doctor || true

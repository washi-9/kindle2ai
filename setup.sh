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

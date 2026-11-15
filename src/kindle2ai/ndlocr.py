"""NDLOCR-Lite backend, the most accurate option for vertical Japanese.

NDLOCR-Lite (National Diet Library, 2026) is optional: it needs onnxruntime
and OpenCV, which kindle2ai otherwise avoids, so ./setup-ndlocr.sh installs it
into its own virtualenv under build/ and it is used only when present.

Measured against Aozora Bunko on the same preprocessed pages:
    Live Text 1.11% / 0.67%, NDLOCR-Lite 0.28% / 0.17% CER (走れメロス / 人間失格).
It reads a whole directory per run so the models load once (~2.5s a page).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "build" / "ndlocr-lite" / "src"
PYTHON = ROOT / "build" / "ndlocr-venv" / "bin" / "python"
RUNNER = Path(__file__).with_name("ndlocr_runner.py")


class NDLOCRError(RuntimeError):
    pass


def is_ready() -> bool:
    return PYTHON.is_file() and (SRC / "ocr.py").is_file()


def install_hint() -> str:
    return "NDLOCR-Lite is not installed (optional).\n  ./setup-ndlocr.sh"


def ocr_dir(images: Path, outdir: Path, timeout: float = 3600.0) -> None:
    """Run NDLOCR-Lite over every image in `images`, writing <stem>.json to outdir."""
    outdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(PYTHON), str(RUNNER), str(SRC),
         "--sourcedir", str(images), "--output", str(outdir), "--json-only"],
        cwd=SRC, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise NDLOCRError(f"NDLOCR-Lite failed:\n{proc.stderr.strip()[-2000:]}")


def parse_page(data: dict) -> dict:
    """NDLOCR-Lite page JSON -> {'width', 'height', 'lines'} in reading order.

    Lines keep NDLOCR-Lite's order, which comes from its reading-order model and
    already handles tategaki columns and spreads. A column that starts lower
    than the page's text top is indented, so it gets the U+3000 paragraph
    marker the assembler looks for.
    """
    info = data.get("imginfo", {})
    lines = []
    for block in data.get("contents", []):
        for item in block:
            xs = [p[0] for p in item["boundingBox"]]
            ys = [p[1] for p in item["boundingBox"]]
            lines.append({
                "text": item.get("text", ""),
                "conf": float(item.get("confidence", 0.0)),
                "vertical": item.get("isVertical") == "true",
                "x": min(xs), "y": min(ys),
                "w": max(xs) - min(xs), "h": max(ys) - min(ys),
            })

    vertical = [l for l in lines if l["vertical"] and l["w"] > 0]
    if vertical:
        top = min(l["y"] for l in vertical)
        cell = sorted(l["w"] for l in vertical)[len(vertical) // 2]
        for l in vertical:
            if l["y"] - top > cell * 0.6:
                l["text"] = "　" + l["text"]
    return {"width": info.get("img_width", 0), "height": info.get("img_height", 0),
            "lines": lines}


def load_page(path: Path) -> dict:
    return parse_page(json.loads(path.read_text(encoding="utf-8")))

"""Tesseract backend, used for vertical Japanese (縦書き).

Apple's Vision framework cannot read vertical Japanese at all - it returns zero
observations on a cleanly typeset tategaki page. Tesseract ships a dedicated
`jpn_vert` model for it, so vertical pages are routed here while horizontal
pages stay on Vision, which is faster and needs no extra install.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
from pathlib import Path

from .textclean import _CJK

ROOT = Path(__file__).resolve().parents[2]
TESSDATA = ROOT / "build" / "tessdata"

# Page segmentation mode 5 = one uniform block of vertically aligned text,
# which maps each column of a tategaki page to one output line.
VERTICAL_PSM = "5"
HORIZONTAL_PSM = "6"


class TesseractError(RuntimeError):
    pass


def binary() -> str | None:
    return shutil.which("tesseract")


def available_languages() -> list[str]:
    exe = binary()
    if not exe:
        return []
    env = {**os.environ, "TESSDATA_PREFIX": str(TESSDATA)}
    proc = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        return []
    return [l.strip() for l in proc.stdout.splitlines()[1:] if l.strip()]


def is_ready(lang: str = "jpn_vert") -> bool:
    return bool(binary()) and lang in available_languages()


def install_hint(lang: str = "jpn_vert") -> str:
    if not binary():
        return ("tesseract is not installed.\n"
                "  brew install tesseract")
    return (f"the '{lang}' model is missing from {TESSDATA}.\n"
            f"  curl -L -o {TESSDATA}/{lang}.traineddata \\\n"
            f"    https://github.com/tesseract-ocr/tessdata_best/raw/main/"
            f"{lang}.traineddata")


def _join_words(words: list[str]) -> str:
    """Tesseract pads Japanese with spaces that do not belong in the text."""
    out = ""
    for word in words:
        if not word:
            continue
        if not out:
            out = word
        elif _CJK.search(out[-1]) or _CJK.search(word[0]):
            out += word
        else:
            out += " " + word
    return out


def ocr(image: Path, lang: str = "jpn_vert", vertical: bool = True,
        timeout: float = 180.0) -> dict:
    """Return the same shape as `native.ocr`: pixel boxes, origin top-left."""
    exe = binary()
    if not exe:
        raise TesseractError(install_hint(lang))

    env = {**os.environ, "TESSDATA_PREFIX": str(TESSDATA)}
    proc = subprocess.run(
        # The bare `tsv` config argument is silently ignored here; setting the
        # variable directly is what actually switches on the TSV renderer.
        [exe, str(image), "stdout", "-l", lang,
         "--psm", VERTICAL_PSM if vertical else HORIZONTAL_PSM,
         "-c", "tessedit_create_tsv=1"],
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    if proc.returncode != 0:
        raise TesseractError(proc.stderr.strip() or "tesseract failed")

    reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="\t",
                            quoting=csv.QUOTE_NONE)
    # Words carry the text; group them back into their source line.
    grouped: dict[tuple, dict] = {}
    for row in reader:
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(row["conf"])
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
        except (TypeError, ValueError, KeyError):
            continue
        if conf < 0:
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        g = grouped.setdefault(key, {"words": [], "confs": [],
                                     "x0": left, "y0": top,
                                     "x1": left + width, "y1": top + height})
        g["words"].append(text)
        g["confs"].append(conf)
        g["x0"] = min(g["x0"], left)
        g["y0"] = min(g["y0"], top)
        g["x1"] = max(g["x1"], left + width)
        g["y1"] = max(g["y1"], top + height)

    lines = []
    for g in grouped.values():
        text = _join_words(g["words"])
        if not text:
            continue
        lines.append({
            "text": text,
            "conf": sum(g["confs"]) / len(g["confs"]) / 100.0,
            "x": float(g["x0"]), "y": float(g["y0"]),
            "w": float(g["x1"] - g["x0"]), "h": float(g["y1"] - g["y0"]),
        })

    width, height = _image_size(image)
    return {"width": width, "height": height, "lines": lines}


def _image_size(image: Path) -> tuple[int, int]:
    proc = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(image)],
                          capture_output=True, text=True)
    w = h = 0
    for line in proc.stdout.splitlines():
        if "pixelWidth:" in line:
            w = int(line.split(":")[1])
        elif "pixelHeight:" in line:
            h = int(line.split(":")[1])
    return w, h

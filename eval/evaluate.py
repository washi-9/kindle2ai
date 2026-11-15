"""Measure vertical-Japanese OCR accuracy under Kindle-like rendering.

Scores each preprocessing variant by character error rate against text whose
correct transcription is known exactly, because confidence scores alone cannot
tell a confident mistake from a correct read.

Usage: python3 eval/evaluate.py [--sizes 20,24,28,32] [--variants all]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kindle2ai import tess  # noqa: E402

RENDERER = ROOT / "build" / "render-sample"
PREP = ROOT / "build" / "kindle-ocr"
SAMPLE = ROOT / "eval" / "sample_ja.txt"

CROP = ["--crop-top", "0.06", "--crop-bottom", "0.05"]
VARIANTS: dict[str, list[str]] = {
    "baseline": [],
    "crop": CROP,
    "crop+scale2": [*CROP, "--scale", "2"],
    "crop+contrast": [*CROP, "--contrast"],
    "crop+scale2+contrast": [*CROP, "--scale", "2", "--contrast"],
    "crop+scale2+binarize": [*CROP, "--scale", "2", "--binarize"],
    "crop+scale3+contrast": [*CROP, "--scale", "3", "--contrast"],
}


def normalize(s: str) -> str:
    """Compare on content, not on spacing Tesseract invented."""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s)


def edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1,        # deletion
                               current[j - 1] + 1,     # insertion
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def render(size: int, out: Path) -> str:
    subprocess.run([str(RENDERER), str(SAMPLE), str(out), "--size", str(size)],
                   capture_output=True, check=True)
    return normalize(Path(str(out) + ".truth.txt").read_text(encoding="utf-8"))


def recognize(image: Path, opts: list[str], workdir: Path) -> tuple[str, float]:
    target = image
    if opts:
        target = workdir / (image.stem + "_prep.png")
        subprocess.run([str(PREP), "prep", str(image), str(target), *opts],
                       capture_output=True, check=True)
    result = tess.ocr(target)
    text = normalize("".join(l["text"] for l in result["lines"]))
    confs = [l["conf"] for l in result["lines"]]
    return text, (sum(confs) / len(confs) if confs else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="20,24,28,34",
                    help="rendered character sizes in pixels (the real capture "
                         "measured ~24)")
    ap.add_argument("--variants", default="all")
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",")]
    chosen = (VARIANTS if args.variants == "all"
              else {k: VARIANTS[k] for k in args.variants.split(",")})

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        print(f"{'size':>5} {'variant':<22} {'CER':>7} {'精度':>7} {'conf':>6}")
        print("-" * 52)
        best: dict[str, float] = {}
        for size in sizes:
            page = work / f"sample_{size}.png"
            truth = render(size, page)
            for name, opts in chosen.items():
                got, conf = recognize(page, opts, work)
                cer = edit_distance(got, truth) / max(1, len(truth))
                best[f"{size}|{name}"] = cer
                print(f"{size:>5} {name:<22} {cer:>6.1%} {1 - cer:>6.1%} {conf:>6.3f}")
            print("-" * 52)

    top = min(best, key=best.get)
    size, name = top.split("|")
    print(f"最良: size={size} / {name} → CER {best[top]:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

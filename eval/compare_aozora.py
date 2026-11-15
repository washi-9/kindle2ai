"""Score a kindle2ai capture against the Aozora Bunko text of the same work.

This is the only measurement here with real ground truth: the same words, set
by a real publisher, rendered by the real reader, read by the real pipeline.
Everything it reports is an observed error, not a proxy for one.

Usage:
  python3 eval/compare_aozora.py --aozora eval/texts/hashire_merosu.txt \\
                                 --ocr out/走れメロス
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "src"))

import aozora  # noqa: E402
from align import AlignmentFailed, align  # noqa: E402

CLASSES = [
    ("漢字", re.compile(r"[一-龥々〆ヶ]")),
    ("ひらがな", re.compile(r"[ぁ-ん]")),
    ("カタカナ", re.compile(r"[ァ-ヴー]")),
    ("約物", re.compile(r"[、。「」『』（）・…—]")),
    ("英数", re.compile(r"[0-9A-Za-z]")),
]


def classify(ch: str) -> str:
    for name, pattern in CLASSES:
        if pattern.match(ch):
            return name
    return "その他"


def load_ocr(path: Path) -> tuple[str, list[tuple[str, float]]]:
    """Read a capture directory (or a plain text file) as one flat string."""
    if path.is_file():
        return aozora.normalize(path.read_text(encoding="utf-8")), []

    jsonl = path / "ocr.jsonl"
    if jsonl.exists():
        rows = [json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l]
        lines = [(l["text"], l.get("conf", 0.0)) for r in rows for l in r["lines"]]
        return aozora.normalize("".join(t for t, _ in lines)), lines

    candidates = sorted(path.glob("*.txt"))
    if not candidates:
        raise SystemExit(f"no ocr.jsonl or *.txt in {path}")
    return aozora.normalize(candidates[0].read_text(encoding="utf-8")), []


def score(truth: str, seen: str, anchor_n: int) -> dict:
    ops, (start, end) = align(truth, seen, anchor_n=anchor_n, strict=True)
    counts = Counter(o.kind for o in ops)
    gap_truth = sum(len(o.truth) for o in ops if o.kind == "gap")
    covered = sum(1 for o in ops if o.kind in ("equal", "sub", "del"))
    errors = counts["sub"] + counts["ins"] + counts["del"]
    return {
        "ops": ops, "counts": counts, "range": (start, end),
        "covered": covered, "errors": errors,
        "cer": errors / covered if covered else 1.0,
        "gap_truth": gap_truth,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aozora", required=True, help="Aozora .txt (Shift-JIS)")
    ap.add_argument("--ocr", required=True, help="capture directory or .txt")
    ap.add_argument("--anchor", type=int, default=14,
                    help="n-gram length used to pin the two texts together")
    ap.add_argument("--top", type=int, default=25, help="confusion pairs to show")
    args = ap.parse_args()

    variants = aozora.parse(Path(args.aozora))
    seen, lines = load_ocr(Path(args.ocr))
    if not seen:
        raise SystemExit("OCR text is empty")

    print(f"正解テキスト : {Path(args.aozora).name}")
    print(f"OCR テキスト : {args.ocr}  ({len(seen):,} 文字)")
    print()

    # Which ground-truth reading the OCR actually matches tells us whether the
    # reader's ruby ended up in the body text.
    # A noisier capture breaks long n-grams, so retry with shorter anchors
    # before concluding the texts are unrelated.
    scored = None
    used_anchor = args.anchor
    for anchor in (args.anchor, 10, 8, 6):
        if anchor > args.anchor:
            continue
        try:
            scored = {name: score(text, seen, anchor)
                      for name, text in variants.items()}
            used_anchor = anchor
            break
        except AlignmentFailed as last:
            error = last
    if scored is None:
        e = error
        print(f"照合できません: {e}\n")
        print("考えられる原因:")
        print("  - Kindle 版の底本が青空文庫と異なる（旧字旧仮名／新字新仮名の違いなど）")
        print("  - 別の作品、または作品集の違う部分をキャプチャしている")
        print("  - OCR がほとんど本文を取れていない（kindle2ai doctor を確認）")
        print("  - OCR の誤り率が高すぎてアンカーが作れない")
        return 2
    if used_anchor != args.anchor:
        print(f"注: アンカー長を {args.anchor} → {used_anchor} に下げて照合しました"
              "（OCR の誤りが多めです）\n")
    best = min(scored, key=lambda k: scored[k]["cer"])
    print("■ ルビの扱い")
    for name, result in scored.items():
        label = "ルビを含む正解" if name == "with_ruby" else "ルビを除いた正解"
        mark = " ←採用" if name == best else ""
        print(f"   {label:<16} CER {result['cer']:6.2%}{mark}")
    if best == "with_ruby":
        print("   → OCR がルビを本文に取り込んでいます")
    else:
        print("   → OCR はルビを拾っていません（望ましい）")
    print()

    result = scored[best]
    truth = variants[best]
    start, end = result["range"]
    counts = result["counts"]

    print("■ 全体精度")
    print(f"   照合範囲      正解の {start:,} 〜 {end:,} 文字目 "
          f"({(end - start) / len(truth):.0%} をカバー)")
    print(f"   照合文字数    {result['covered']:,}")
    print(f"   文字誤り率    {result['cer']:.2%}   （精度 {1 - result['cer']:.2%}）")
    print(f"   置換 {counts['sub']:,} / 脱落 {counts['del']:,} / 余分 {counts['ins']:,}")
    if result["gap_truth"]:
        print(f"   未照合        {result['gap_truth']:,} 文字（キャプチャ欠落とみなし除外）")
    print()

    ops = result["ops"]

    print("■ 文字種別の誤り率")
    seen_by_class: Counter[str] = Counter()
    err_by_class: Counter[str] = Counter()
    for op in ops:
        if op.kind == "gap":
            continue
        if op.truth:
            cls = classify(op.truth)
            seen_by_class[cls] += 1
            if op.kind in ("sub", "del"):
                err_by_class[cls] += 1
    for cls, total in seen_by_class.most_common():
        rate = err_by_class[cls] / total if total else 0
        print(f"   {cls:<6} {total:>7,} 文字中 {err_by_class[cls]:>5,} 誤り  {rate:6.2%}")
    print()

    print(f"■ 誤変換の内訳（上位 {args.top}）")
    confusions = Counter((op.truth, op.seen) for op in ops if op.kind == "sub")
    for (a, b), n in confusions.most_common(args.top):
        print(f"   {a} → {b}   {n:>4}")
    print()

    print("■ 脱落しやすい文字（上位 15）")
    for ch, n in Counter(op.truth for op in ops if op.kind == "del").most_common(15):
        print(f"   {ch}   {n:>4}")
    print()

    print("■ 余分に出た文字（上位 15）")
    for ch, n in Counter(op.seen for op in ops if op.kind == "ins").most_common(15):
        print(f"   {ch}   {n:>4}")
    print()

    if lines:
        print("■ 信頼度との関係")
        text_to_conf = {}
        for text, conf in lines:
            if text.strip():
                text_to_conf.setdefault(aozora.normalize(text), conf)
        print(f"   OCR 行数 {len(lines):,} / 平均信頼度 "
              f"{sum(c for _, c in lines) / len(lines):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

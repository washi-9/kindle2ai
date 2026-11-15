"""Normalize OCR output and reassemble it into paragraphs.

OCR gives one string per visual line. An LLM wants paragraphs, so lines have to
be rejoined -- and in Japanese that means joining with no space at all, which is
the opposite of what every English-centric text pipeline does.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# Characters Vision occasionally emits that carry no meaning.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

_FULLWIDTH_ALNUM = {c: c - 0xFEE0 for c in range(0xFF10, 0xFF5B)}  # ０-９ Ａ-Ｚ ａ-ｚ
_FULLWIDTH_PUNCT = {"，": ",", "．": ".", "：": ":", "；": ";", "（": "(", "）": ")"}

_DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "—"}

# Kindle's own on-screen furniture, which lands in the capture alongside the text.
_UI_PATTERNS = [
    re.compile(r"^位置\s*No\.?\s*[\d,]+(\s*(of|/)\s*[\d,]+)?$"),
    re.compile(r"^\s*\d{1,3}\s*%\s*$"),
    re.compile(r"^(この本|本章)?残り\s*\d+\s*(時間|分|秒).*$"),
    re.compile(r"^\d+\s*(時間|分)\s*(残り)?$"),
    re.compile(r"^ページ\s*[\d,]+(\s*(/|of)\s*[\d,]+)?$"),
    re.compile(r"^(Loc\.?|Location)\s*[\d,]+.*$", re.I),
    re.compile(r"^Page\s*[\divxlc,]+(\s*of\s*[\d,]+)?$", re.I),
    re.compile(r"^[\s\d.,:ivxlc\-—–|]+$", re.I),  # bare page / section numbers
]

_HEADING = re.compile(
    r"^(第\s*[0-9０-９一二三四五六七八九十百]+\s*[章節部編話]"
    r"|[0-9]+(\.[0-9]+){0,3}\s+\S"
    r"|(Chapter|CHAPTER|Part|Section)\s+[\dIVXLC]+)"
)

_CJK = re.compile(
    r"[　-〿぀-ゟ゠-ヿ㐀-䶿"
    r"一-鿿豈-﫿＀-￯]"
)
_LATIN_END = re.compile(r"[A-Za-z0-9,;:)\]\"']$")
_LATIN_START = re.compile(r"^[A-Za-z0-9(\[\"']")


def normalize(text: str) -> str:
    """Width/dash/space cleanup that preserves Japanese punctuation."""
    s = text.translate(_INVISIBLE)
    # Half-width katakana -> full-width, without touching anything else.
    s = "".join(
        unicodedata.normalize("NFKC", ch) if "｡" <= ch <= "ﾟ" else ch
        for ch in s
    )
    s = s.translate(_FULLWIDTH_ALNUM)
    s = s.translate(str.maketrans(_DASHES))
    # Full-width punctuation is only wrong when it sits inside Latin text.
    for fw, hw in _FULLWIDTH_PUNCT.items():
        s = re.sub(rf"(?<=[A-Za-z0-9]){re.escape(fw)}", hw, s)
        s = re.sub(rf"{re.escape(fw)}(?=[A-Za-z0-9])", hw, s)
    s = re.sub(r"[ \t ]+", " ", s)
    return s.strip(" \t")


# Tesseract's vertical model reads the stacked dots of "……" as colons or a
# mix of colons and ellipses (":::", ":……", "::…:"). Measured against Aozora
# Bunko, every such run of two or more was a "……" in the source.
_ELLIPSIS_RUN = re.compile(r"[:：…‥](?:・?[:：…‥])+・?")

# It also reads one kana twice or three times at both sizes ("ぃい", "いぃ",
# "いぃい" for a single い). Neither Aozora text contains a full-size kana next
# to its own small form. っ/ッ are left alone: "ッツ" is real (ガッツ), and so
# is the occasional "っつ".
_KANA_PAIRS = list(zip("ぁぃぅぇぉゃゅょァィゥェォャュョ",
                       "あいうえおやゆよアイウエオヤユヨ"))
_SMALL_KANA_ECHO = re.compile(
    "|".join(f"{l}{s}{l}|{l}{s}|{s}{l}" for s, l in _KANA_PAIRS))
_SMALL_TO_FULL = {s + l: l for s, l in _KANA_PAIRS}


def _collapse_echo(match: re.Match) -> str:
    text = match.group(0)
    return _SMALL_TO_FULL.get(text) or text[0]


def fix_tesseract_artifacts(line: str) -> str:
    """Undo misreadings that Tesseract's jpn_vert model makes systematically."""
    line = _ELLIPSIS_RUN.sub("……", line)
    return _SMALL_KANA_ECHO.sub(_collapse_echo, line)


# Live Text reads each dot of "……" as a middle dot, so "……" becomes "・・・・・・".
# A real 中黒 stands alone (アート・ギャラリー), so only runs are rewritten.
_DOT_RUN = re.compile(r"[・•·]{2,}")


def fix_livetext_artifacts(line: str) -> str:
    """Undo misreadings that Live Text makes systematically."""
    return _DOT_RUN.sub(lambda m: "…" * max(2, round(len(m.group(0)) / 3)), line)


def livetext_lines(transcript: str) -> list[dict]:
    """Turn a Live Text transcript into the line records the pipeline expects.

    Live Text gives no geometry, so how far a line reaches cannot be measured.
    Its line breaks fall mostly at paragraph ends but sometimes mid-sentence,
    so every line is marked short (fill 0) and build_paragraphs only breaks
    where the line also ends a sentence.
    """
    return [{"text": normalize(t), "fill": 0.0, "conf": 1.0}
            for t in transcript.splitlines() if t.strip()]


# NDLOCR-Lite puts an ASCII space after Japanese punctuation ("激怒した。 必ず、
# かの"). Japanese has no word spacing, so a space touching a CJK character is
# dropped; one between two Latin words stays. U+3000 is not touched: it is the
# paragraph marker.
_SPACE_BY_CJK = re.compile(
    r"(?<=[　-〿぀-ゟ゠-ヿ一-鿿＀-￯]) +|"
    r" +(?=[　-〿぀-ゟ゠-ヿ一-鿿＀-￯])")


def fix_ndlocr_artifacts(line: str) -> str:
    """Undo NDLOCR-Lite's output conventions that do not belong in the text."""
    return _SPACE_BY_CJK.sub("", line)


# Characters that body text almost never contains, but that OCR produces from
# rules, borders and figure edges.
_SYMBOLS = re.compile(r"[|_\\~^`<>{}\[\]=+*#@$]")

_JA_SCRIPT = re.compile(r"[ぁ-んァ-ヶ一-龥]")


def is_symbol_noise(line: str, conf: float = 1.0,
                    japanese_document: bool = False) -> bool:
    """True for lines that are OCR debris rather than text.

    Two signals, both measured on a real book: a line that is half punctuation
    marks is a figure border, and in a Japanese document a short line with no
    Japanese in it at all is a misread rule. Together they removed 19% of the
    stray characters while costing 0.03% of the real text.
    """
    text = line.strip()
    if not text:
        return True
    if len(_SYMBOLS.findall(text)) / len(text) >= 0.5:
        return True
    if (japanese_document and not _JA_SCRIPT.search(text)
            and len(text) <= 20 and conf < 0.6):
        return True
    return False


def is_japanese_document(samples: list[str], threshold: float = 0.3) -> bool:
    """Whether to trust the 'no Japanese means noise' rule at all."""
    joined = "".join(samples)
    if not joined:
        return False
    return len(_JA_SCRIPT.findall(joined)) / len(joined) >= threshold


def is_ui_noise(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    return any(p.match(s) for p in _UI_PATTERNS)


_SENTENCE_END = re.compile(r"[。．\.!?！？」』]$")


def _fingerprint(line: str) -> str:
    """Collapse digits so 'page 12' and 'page 13' count as the same header."""
    return re.sub(r"\d+", "#", line.strip())


def _can_be_running(line: str) -> bool:
    """Running heads are labels, not sentences - never strip a finished sentence."""
    s = line.strip()
    return bool(s) and not _SENTENCE_END.search(s)


def find_running_elements(pages: list[list[str]], edge: int = 2,
                          min_ratio: float = 0.25) -> set[str]:
    """Fingerprints that recur at the top or bottom of many pages."""
    if len(pages) < 4:
        return set()
    counts: Counter[str] = Counter()
    for lines in pages:
        candidates = lines[:edge] + lines[-edge:]
        for fp in {_fingerprint(l) for l in candidates if _can_be_running(l)}:
            counts[fp] += 1
    threshold = max(3, int(len(pages) * min_ratio))
    return {fp for fp, n in counts.items() if n >= threshold and len(fp) <= 80}


def keep_indices(lines: list[str], running: set[str], edge: int = 2) -> list[int]:
    """Indices of body-text lines, with chrome and running heads dropped."""
    kept: list[int] = []
    last = len(lines) - 1
    for i, line in enumerate(lines):
        at_edge = i < edge or i > last - edge
        if at_edge and is_ui_noise(line):
            continue
        if at_edge and _can_be_running(line) and _fingerprint(line) in running:
            continue
        if is_ui_noise(line) and len(line.strip()) <= 12:
            continue
        kept.append(i)
    return kept


def clean_page_lines(lines: list[str], running: set[str], edge: int = 2) -> list[str]:
    return [lines[i] for i in keep_indices(lines, running, edge)]


def _starts_paragraph(line: str, prev: str, prev_fill: float,
                      fill_threshold: float) -> bool:
    if not prev:
        return True
    if line.startswith("　") or line.startswith("  "):
        return True
    if _HEADING.match(line) or _HEADING.match(prev):
        return True
    # A line that stops well short of the margin ended its paragraph.
    if prev_fill < fill_threshold and re.search(r"[。．.!?！？」』）\)]$", prev):
        return True
    return False


def _join(prev: str, line: str) -> str:
    if prev.endswith("-") and _LATIN_START.match(line):
        return prev[:-1] + line  # de-hyphenate a wrapped English word
    if _CJK.search(prev[-1:]) or _CJK.search(line[:1]):
        return prev + line
    if _LATIN_END.search(prev) or _LATIN_START.match(line):
        return prev + " " + line
    return prev + line


def build_paragraphs(entries: list[tuple[str, float]],
                     fill_threshold: float = 0.90) -> list[str]:
    """entries: (line_text, fill). Returns paragraph strings."""
    paragraphs: list[str] = []
    current = ""
    prev_fill = 1.0
    for text, fill in entries:
        # Strip ASCII whitespace only: a leading U+3000 is the paragraph marker.
        text = text.strip(" \t\r\n")
        if not text:
            continue
        if _starts_paragraph(text, current, prev_fill, fill_threshold):
            if current:
                paragraphs.append(current)
            current = text.lstrip("　")
        else:
            current = _join(current, text)
        prev_fill = fill
    if current:
        paragraphs.append(current)
    return paragraphs


def as_markdown(paragraphs: list[str]) -> list[str]:
    """Promote chapter/section lines to Markdown headings."""
    out: list[str] = []
    for p in paragraphs:
        if _HEADING.match(p) and len(p) <= 60:
            out.append("## " + p)
        else:
            out.append(p)
    return out

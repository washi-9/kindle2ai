"""Run NDLOCR-Lite with a retry for lines it reads short.

Executed by NDLOCR-Lite's own virtualenv, not by kindle2ai's interpreter:

    <venv>/bin/python ndlocr_runner.py <ndlocr-lite/src> --sourcedir IN --output OUT

NDLOCR-Lite reads each line with one of three recognizers sized for 30, 50 or
100 characters, chosen from a predicted length, and only escalates when the
output fills the smaller model. A full Kindle column (~46 characters) goes to
the 50-character model, which sometimes skips a span -- typically right after
a repeated phrase -- and the shortened output never triggers escalation.
Against Aozora Bunko those skips were most of NDLOCR-Lite's remaining errors.

The opposite failure also occurs: the decoder loops and repeats a phrase
("この子は、この子は、その証拠には、この子は、その証拠には") while dropping the
text it should have read, so the length looks right.

Full-width characters are square, so a vertical line box should hold about
height/width characters. A reading well short of that, or one that repeats a
phrase, is read again as two overlapping halves -- which the models read
without skipping or looping -- and the halves are joined where their overlap
matches. Re-reading the whole line with the 100-character model does not
help: it skips the same span.
"""

from __future__ import annotations

import sys

# Below this share of the box-implied length, a reading is suspected of
# skipping text. Measured: intact lines sit near 1.0, lines with a skipped span
# at 0.53-0.77, and short spaced-out lines (verse) at 0.84-0.92.
SHORT_RATIO = 0.85
# Lines shorter than this many character cells are too short to judge.
MIN_CELLS = 8


# A phrase this long seen twice in one line is taken as a decoder loop.
LOOP_SPAN = 5

# How far, in character cells, the two halves overlap. Enough that at least
# one whole character appears in both even when the cut lands mid-glyph.
OVERLAP_CELLS = 1.5


def _visible_len(text: str) -> int:
    return len(text.replace(" ", ""))


def repeated_span(text: str) -> int:
    """Length of the longest phrase that occurs twice without overlapping."""
    t = text.replace(" ", "")
    best = 0
    for size in range(LOOP_SPAN, len(t) // 2 + 1):
        if not any(t[i:i + size] in t[i + size:] for i in range(len(t) - 2 * size + 1)):
            break
        best = size
    return best


def join_overlapping(first: str, second: str, max_overlap: int = 8,
                     max_junk: int = 2) -> str | None:
    """Join two readings whose ends overlap; None if no overlap is found.

    A half can still end on a sliver of a glyph that reads as junk ("少し・"
    next to "一少しも"), so up to `max_junk` characters at the seam may be
    dropped from either side. At least two characters must match: a lone "、"
    lines up by accident far too often.
    """
    a, b = first.replace(" ", ""), second.replace(" ", "")
    for k in range(min(max_overlap, len(a), len(b)), 1, -1):
        for junk in range(max_junk + 1):
            for ja in range(junk + 1):
                jb = junk - ja
                end = len(a) - ja
                if end - k < 0 or jb + k > len(b):
                    continue
                if a[end - k:end] == b[jb:jb + k]:
                    return a[:end] + b[jb + k:]
    return None


def _quiet_cut(image, target: int, cell: int, axis: int) -> int:
    """The row (or column) nearest `target` with the least ink, i.e. a gap
    between two characters, so neither half ends on part of a glyph."""
    import numpy as np
    gray = image.mean(axis=2) if image.ndim == 3 else image
    ink = (gray < 128).sum(axis=1 - axis)
    lo = max(1, target - cell // 2)
    hi = min(len(ink) - 1, target + cell // 2)
    if hi <= lo:
        return target
    return lo + int(np.argmin(ink[lo:hi]))


def read_in_halves(image, recognizer) -> str | None:
    h, w = image.shape[:2]
    cell = min(h, w)
    overlap = int(cell * OVERLAP_CELLS)
    if h >= w:
        mid = h // 2
        end = _quiet_cut(image, mid + overlap, cell, 0)
        start = _quiet_cut(image, mid - overlap, cell, 0)
        first, second = image[:end], image[start:]
    else:
        mid = w // 2
        end = _quiet_cut(image, mid + overlap, cell, 1)
        start = _quiet_cut(image, mid - overlap, cell, 1)
        first, second = image[:, :end], image[:, start:]
    return join_overlapping(recognizer.read(first), recognizer.read(second))


def install_retry(ocr) -> None:
    original = ocr.process_cascade

    def process_cascade(alllineobj, recognizer30, recognizer50, recognizer100,
                        is_cascade=True):
        texts = original(alllineobj, recognizer30, recognizer50, recognizer100,
                         is_cascade)
        for i, line in enumerate(sorted(alllineobj)):
            if i >= len(texts) or line.npimg is None or line.npimg.size == 0:
                continue
            h, w = line.npimg.shape[:2]
            if min(h, w) == 0:
                continue
            cells = max(h, w) / min(h, w)
            if cells < MIN_CELLS:
                continue
            short = _visible_len(texts[i]) < cells * SHORT_RATIO
            looped = repeated_span(texts[i]) > 0
            if not (short or looped):
                continue
            retry = read_in_halves(line.npimg, recognizer50)
            # Never accept a reading that overruns the box, which would mean
            # the halves were joined wrongly.
            if not retry or len(retry) > cells * 1.15:
                continue
            if short and len(retry) > _visible_len(texts[i]):
                texts[i] = retry
            elif looped and repeated_span(retry) < repeated_span(texts[i]) \
                    and len(retry) >= cells * SHORT_RATIO:
                texts[i] = retry
        return texts

    ocr.process_cascade = process_cascade


def main() -> None:
    src = sys.argv[1]
    sys.path.insert(0, src)
    import ocr  # NDLOCR-Lite's module

    install_retry(ocr)
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    ocr.main()


if __name__ == "__main__":
    main()

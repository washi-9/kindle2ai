"""Turn a bag of OCR boxes into reading order.

Vision returns text observations in no particular order. Japanese books add two
wrinkles the naive top-to-bottom sort gets wrong: vertical writing (tategaki),
which reads right column first, and two-page spreads, which need the gutter
found before anything else.
"""

from __future__ import annotations

from statistics import median

Line = dict  # {"text", "conf", "x", "y", "w", "h"}


def _is_vertical_box(line: Line) -> bool:
    text = line["text"].strip()
    if len(text) < 2:
        return False
    return line["h"] > line["w"] * 1.4


def detect_orientation(lines: list[Line]) -> str:
    """'vertical' when most recognized characters sit in tall, narrow boxes."""
    total = sum(len(l["text"]) for l in lines)
    if not total:
        return "horizontal"
    vertical = sum(len(l["text"]) for l in lines if _is_vertical_box(l))
    return "vertical" if vertical / total > 0.5 else "horizontal"


def find_gutter(lines: list[Line], page_width: float,
                min_frac: float = 0.035) -> float | None:
    """Locate the empty vertical band of a two-page spread, if there is one."""
    if len(lines) < 6:
        return None
    spans = sorted((l["x"], l["x"] + l["w"]) for l in lines)
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    best = None
    for left, right in zip(merged, merged[1:]):
        gap_start, gap_end = left[1], right[0]
        width = gap_end - gap_start
        center = (gap_start + gap_end) / 2
        # Only trust a gap that is wide and sits near the middle of the page.
        if width < page_width * min_frac:
            continue
        if not (0.33 * page_width < center < 0.67 * page_width):
            continue
        if best is None or width > best[0]:
            best = (width, center)
    return best[1] if best else None


def _cluster(lines: list[Line], key, tol: float) -> list[list[Line]]:
    """Group lines whose `key` centres fall within `tol` of the running group."""
    groups: list[list[Line]] = []
    for line in sorted(lines, key=key):
        if groups and abs(key(line) - key(groups[-1][-1])) <= tol:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def _cx(l: Line) -> float:
    return l["x"] + l["w"] / 2


def _cy(l: Line) -> float:
    return l["y"] + l["h"] / 2


def order_block(lines: list[Line], orientation: str) -> list[Line]:
    if not lines:
        return []
    if orientation == "vertical":
        tol = median(l["w"] for l in lines) * 0.7
        columns = _cluster(lines, _cx, tol)
        columns.reverse()  # tategaki reads right to left
        return [l for col in columns for l in sorted(col, key=lambda x: x["y"])]

    tol = median(l["h"] for l in lines) * 0.7
    rows = _cluster(lines, _cy, tol)
    return [l for row in rows for l in sorted(row, key=lambda x: x["x"])]


def annotate_fill(lines: list[Line], orientation: str) -> None:
    """Attach `fill`: how much of the text block this line spans (0..1).

    A short last line is the strongest signal of a paragraph break, which is
    what the text assembler uses it for.
    """
    if not lines:
        return
    if orientation == "vertical":
        extent = max(l["y"] + l["h"] for l in lines) - min(l["y"] for l in lines)
        for l in lines:
            l["fill"] = l["h"] / extent if extent else 1.0
    else:
        extent = max(l["x"] + l["w"] for l in lines) - min(l["x"] for l in lines)
        for l in lines:
            l["fill"] = l["w"] / extent if extent else 1.0


def analyze(page: dict, orientation: str = "auto",
            columns: str = "auto") -> dict:
    """page: the JSON emitted by `kindle-ocr ocr`. Returns ordered lines."""
    lines = [l for l in page.get("lines", []) if l["text"].strip()]
    width = float(page.get("width") or 1)

    if orientation == "auto":
        orientation = detect_orientation(lines)

    gutter = None
    if columns in ("auto", "2") and lines:
        gutter = find_gutter(lines, width)
        if columns == "2" and gutter is None:
            gutter = width / 2
    if columns == "1":
        gutter = None

    if gutter is None:
        blocks = [lines]
    else:
        left = [l for l in lines if _cx(l) < gutter]
        right = [l for l in lines if _cx(l) >= gutter]
        # Vertical Japanese spreads read right page first.
        blocks = [right, left] if orientation == "vertical" else [left, right]

    ordered: list[Line] = []
    for block in blocks:
        annotate_fill(block, orientation)
        ordered.extend(order_block(block, orientation))

    return {
        "orientation": orientation,
        "spread": gutter is not None,
        "lines": ordered,
    }

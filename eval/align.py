"""Anchor-based alignment of OCR output against a known-correct text.

A full edit-distance matrix over a whole book is too large to hold, so exact
matching n-grams are found first and used as fixed points; only the short,
genuinely different stretches between them go through dynamic programming.
That also handles the usual case where the capture covers part of the work.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Op:
    kind: str          # "equal" | "sub" | "ins" | "del"
    truth: str = ""    # character in the correct text
    seen: str = ""     # character the OCR produced
    pos: int = 0       # index into the correct text


def _unique_ngrams(text: str, n: int) -> dict[str, int]:
    seen: dict[str, int] = {}
    duplicated: set[str] = set()
    for i in range(len(text) - n + 1):
        gram = text[i:i + n]
        if gram in seen:
            duplicated.add(gram)
        else:
            seen[gram] = i
    for gram in duplicated:
        del seen[gram]
    return seen


def _longest_increasing(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Keep the largest subset of anchors consistent with one reading order."""
    if not pairs:
        return []
    import bisect
    tails: list[int] = []
    tail_index: list[int] = []
    previous = [-1] * len(pairs)
    for i, (_, b) in enumerate(pairs):
        j = bisect.bisect_left(tails, b)
        if j == len(tails):
            tails.append(b)
            tail_index.append(i)
        else:
            tails[j] = b
            tail_index[j] = i
        previous[i] = tail_index[j - 1] if j > 0 else -1
    result = []
    k = tail_index[-1]
    while k != -1:
        result.append(pairs[k])
        k = previous[k]
    return result[::-1]


def find_anchors(truth: str, seen: str, n: int = 14,
                 min_gap: int = 24) -> list[tuple[int, int]]:
    truth_grams = _unique_ngrams(truth, n)
    seen_grams = _unique_ngrams(seen, n)
    pairs = sorted((truth_grams[g], seen_grams[g])
                   for g in truth_grams.keys() & seen_grams.keys())
    pairs = _longest_increasing(pairs)

    spaced: list[tuple[int, int]] = []
    for a, b in pairs:
        if not spaced or (a - spaced[-1][0] >= min_gap
                          and b - spaced[-1][1] >= min_gap):
            spaced.append((a, b))
    return spaced


def _dp_align(truth: str, seen: str, offset: int) -> list[Op]:
    """Classic edit distance with backtrace over a short segment."""
    n, m = len(truth), len(seen)
    if n == 0:
        return [Op("ins", "", c, offset) for c in seen]
    if m == 0:
        return [Op("del", c, "", offset + i) for i, c in enumerate(truth)]

    cost = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        cost[i][0] = i
    for j in range(m + 1):
        cost[0][j] = j
    for i in range(1, n + 1):
        row, previous = cost[i], cost[i - 1]
        ti = truth[i - 1]
        for j in range(1, m + 1):
            row[j] = min(previous[j] + 1,
                         row[j - 1] + 1,
                         previous[j - 1] + (ti != seen[j - 1]))

    ops: list[Op] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            same = truth[i - 1] == seen[j - 1]
            if cost[i][j] == cost[i - 1][j - 1] + (0 if same else 1):
                ops.append(Op("equal" if same else "sub",
                              truth[i - 1], seen[j - 1], offset + i - 1))
                i, j = i - 1, j - 1
                continue
        if i > 0 and cost[i][j] == cost[i - 1][j] + 1:
            ops.append(Op("del", truth[i - 1], "", offset + i - 1))
            i -= 1
        else:
            ops.append(Op("ins", "", seen[j - 1], offset + i))
            j -= 1
    return ops[::-1]


MAX_SEGMENT = 400  # keep each DP block small enough to stay fast

# Below this many anchors per thousand characters the two texts are not the
# same work -- or not the same edition of it -- and any score computed from
# them would be meaningless.
MIN_ANCHOR_DENSITY = 0.5


class AlignmentFailed(RuntimeError):
    """The two texts could not be pinned together at all."""


def align(truth: str, seen: str, anchor_n: int = 14,
          strict: bool = True) -> tuple[list[Op], tuple[int, int]]:
    """Align `seen` against `truth`. Returns the ops and the covered range."""
    anchors = find_anchors(truth, seen, n=anchor_n)
    density = len(anchors) / max(1, len(seen) / 1000)
    if strict and density < MIN_ANCHOR_DENSITY:
        raise AlignmentFailed(
            f"only {len(anchors)} anchor(s) over {len(seen):,} characters "
            f"({density:.2f} per 1000). The texts do not line up."
        )
    if not anchors:
        return _dp_align(truth[:MAX_SEGMENT], seen[:MAX_SEGMENT], 0), (0, 0)

    ops: list[Op] = []
    first_truth = anchors[0][0]
    prev_t, prev_s = anchors[0]
    # Everything before the first anchor and after the last one is outside the
    # matched range. The tail matters as much as the head: a colophon or the
    # app's end-of-book page would otherwise be scored as hundreds of extras.
    last_t, last_s = anchors[-1]
    tail = [(last_t + anchor_n, last_s + anchor_n)]
    for a, b in anchors[1:] + tail:
        t_seg, s_seg = truth[prev_t:a], seen[prev_s:b]
        if len(t_seg) > MAX_SEGMENT or len(s_seg) > MAX_SEGMENT:
            # An unmatched stretch this long is a gap in the capture, not an
            # error; record it without pretending to align it.
            ops.append(Op("gap", t_seg, s_seg, prev_t))
        else:
            ops.extend(_dp_align(t_seg, s_seg, prev_t))
        prev_t, prev_s = a, b
    if prev_t < len(truth) or prev_s < len(seen):
        ops.append(Op("gap", truth[prev_t:], seen[prev_s:], prev_t))
    return ops, (first_truth, min(prev_t, len(truth)))

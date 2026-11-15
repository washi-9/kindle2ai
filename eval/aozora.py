"""Parse an Aozora Bunko text file into ground truth for OCR evaluation.

Aozora ships Shift-JIS text with ruby written inline as 漢字《かんじ》 and
editorial notes in ［＃...］. A Kindle rendering of the same work shows the ruby
as small glyphs beside the column, so OCR may skip it or splice it into the
body. Both readings are produced here, and whichever the OCR matches better
tells us which of the two actually happened.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# The header ends at the second row of these box-drawing rules.
_RULE = re.compile(r"^-{10,}$", re.M)
_FOOTER = re.compile(r"^底本[：:]", re.M)

_ANNOTATION = re.compile(r"［＃.*?］")
_GAIJI = re.compile(r"※［＃(.*?)］")
_RUBY = re.compile(r"｜([^｜《》]+)《([^《》]+)》")
_RUBY_BARE = re.compile(r"([一-龥々〆ヶぁ-んァ-ヶ]+)《([^《》]+)》")


def _strip_frame(raw: str) -> str:
    """Drop Aozora's own header block and its colophon."""
    rules = list(_RULE.finditer(raw))
    body = raw[rules[1].end():] if len(rules) >= 2 else raw
    footer = _FOOTER.search(body)
    if footer:
        body = body[:footer.start()]
    return body.strip()


def _resolve_gaiji(text: str) -> str:
    """Keep the character an Aozora gaiji note describes, when it names one."""
    def pick(match: re.Match) -> str:
        note = match.group(1)
        # Notes look like "「廴＋囘」、第4水準2-12-11" - there is no portable
        # character, so leave a placeholder that will not match anything.
        inner = re.search(r"「([^」]+)」", note)
        if inner and len(inner.group(1)) == 1:
            return inner.group(1)
        return "〓"
    return _GAIJI.sub(pick, text)


def parse(path: Path) -> dict[str, str]:
    raw = path.read_bytes().decode("cp932", errors="replace")
    body = _strip_frame(raw)
    body = _resolve_gaiji(body)

    # Ruby inlined: the reading follows its base word, as it would if OCR read
    # the small glyphs as part of the column.
    inlined = _RUBY.sub(lambda m: m.group(1) + m.group(2), body)
    inlined = _RUBY_BARE.sub(lambda m: m.group(1) + m.group(2), inlined)

    # Ruby dropped: only the base text, as it would be if OCR skipped it.
    dropped = _RUBY.sub(lambda m: m.group(1), body)
    dropped = _RUBY_BARE.sub(lambda m: m.group(1), dropped)

    out = {}
    for name, text in (("with_ruby", inlined), ("without_ruby", dropped)):
        text = _ANNOTATION.sub("", text)
        text = text.replace("｜", "").replace("《", "").replace("》", "")
        out[name] = normalize(text)
    return out


def normalize(text: str) -> str:
    """Collapse everything that is not a content character.

    OCR cannot be scored on line breaks or indentation, and full/half width is
    a rendering choice rather than a reading, so both sides are compared after
    the same flattening.
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    return text


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        parsed = parse(Path(arg))
        print(f"{Path(arg).name}")
        for key, value in parsed.items():
            print(f"  {key:<14} {len(value):>7,} 文字")

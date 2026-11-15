"""OCR captured pages, then assemble them into AI-ready text."""

from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import layout, native, ndlocr, tess, textclean

# How many pages vote on whether a book is vertical. Judging from a few is
# unreliable: covers, chapter openers and figure pages misrepresent the body.
PROBE_PAGES = 7
# A probe page with less text than this from either reader does not vote.
PROBE_MIN_CHARS = 80

# Tesseract must win by this margin to displace Vision, which is faster and
# needs no extra install.
ENGINE_MARGIN = 1.3

# Vision language tag -> Tesseract model name.
_TESS_LANG = {"ja": "jpn", "en": "eng", "ko": "kor",
              "zh-Hans": "chi_sim", "zh-Hant": "chi_tra", "ru": "rus"}


# Measured against the Kindle for Mac window: the toolbar ends by 6% down the
# page and the progress bar starts at 95%. Cropping them is the single biggest
# accuracy win, because Tesseract stops trying to fit them into the layout.
DEFAULT_CROP = (0.06, 0.05, 0.0, 0.0)

# Upscaling looks like it should help -- the glyphs are small -- and it does
# raise Tesseract's confidence. Measured against known text it makes accuracy
# worse (CER 1.5% -> 2.3%): interpolation invents detail the screenshot never
# had, and the model grows confident about it. Contrast stretching is the part
# that genuinely helps, because the page is muted brown on cream.
DEFAULT_SCALE = 1.0


def prepare_image(image: Path, workdir: Path,
                  crop: tuple[float, float, float, float],
                  scale: float, contrast: bool, binarize: bool,
                  strip_ruby: bool = False) -> Path:
    if (not any(crop) and scale == 1.0 and not contrast and not binarize
            and not strip_ruby):
        return image
    dest = workdir / (image.stem + ("_noruby" if strip_ruby else "_prep") + ".png")
    try:
        native.prep(image, dest, crop=crop, scale=scale,
                    contrast=contrast, binarize=binarize, strip_ruby=strip_ruby)
    except native.NativeError:
        if not strip_ruby:
            raise
        # Ruby removal is an accuracy gain, not a requirement: one odd page
        # must not stop the whole book.
        native.prep(image, dest, crop=crop, scale=scale,
                    contrast=contrast, binarize=binarize)
    return dest


def _tess_lang(langs: str, vertical: bool) -> str:
    for tag in (l.strip() for l in langs.split(",")):
        name = _TESS_LANG.get(tag) or _TESS_LANG.get(tag.split("-")[0])
        if name:
            return name + "_vert" if (vertical and name == "jpn") else name
    return "jpn_vert" if vertical else "eng"


def _char_count(lines: list[dict]) -> int:
    return sum(len(l["text"]) for l in lines)


def _vertical_engine(tlang: str) -> tuple[str, str]:
    """NDLOCR-Lite if installed, else Live Text, else Tesseract's jpn_vert.

    Against Aozora Bunko (人間失格, same preprocessing) the CER was 0.17%,
    0.67% and 1.96% respectively. Live Text needs no install, so it is the
    default; NDLOCR-Lite is opt-in through ./setup-ndlocr.sh.
    """
    if ndlocr.is_ready():
        return "ndlocr", tlang
    if native.livetext_available():
        return "livetext", tlang
    return "tesseract", tlang


def choose_engine(images: list[Path], langs: str, engine: str,
                  orientation: str, prepare=None) -> tuple[str, str]:
    """Decide once which recognizer this book needs.

    Rather than thresholding Vision's output, both engines are run on a few
    sample pages and the one that actually reads more text wins. Thresholds
    were fragile: Vision scrapes ~100 characters of toolbar text off a vertical
    page, which is enough to look like success while the body is lost.
    """
    vertical = orientation == "vertical"
    tlang = _tess_lang(langs, vertical=True)

    if engine == "tesseract":
        return "tesseract", _tess_lang(langs, vertical=vertical or True)
    if engine == "vision":
        return "vision", tlang
    if engine == "livetext":
        return "livetext", tlang
    if engine == "ndlocr":
        if not ndlocr.is_ready():
            raise native.NativeError(ndlocr.install_hint())
        return "ndlocr", tlang
    if vertical and (ndlocr.is_ready() or native.livetext_available()):
        return _vertical_engine(tlang)
    if vertical:  # explicitly told the book is tategaki
        if not tess.is_ready(tlang):
            raise native.NativeError(
                "--orientation vertical needs Tesseract, but "
                + tess.install_hint(tlang)
            )
        return "tesseract", tlang

    probes = _probe_pages(images)
    if prepare:
        probes = [prepare(p) for p in probes]

    # A second reader that copes with vertical text, to compare Vision with.
    if native.livetext_available():
        def vertical_reader(p):
            return len(native.livetext(p, langs=langs)["text"])
    elif tess.is_ready(tlang):
        def vertical_reader(p):
            return _char_count(tess.ocr(p, lang=tlang, vertical=True)["lines"])
    else:
        vertical_reader = None

    if vertical_reader is None:
        vision_chars = sum(_char_count(native.ocr(p, langs=langs)["lines"])
                           for p in probes)
        if vision_chars < 40 * len(probes):
            raise native.NativeError(
                f"Vision recognized only {vision_chars} characters across "
                f"{len(probes)} pages, which usually means this book is "
                "vertical Japanese (縦書き).\n"
                "Vision cannot read vertical text; kindle2ai uses Tesseract "
                f"for it, but {tess.install_hint(tlang)}\n"
                "Or pass --engine vision to force Vision anyway."
            )
        return "vision", tlang

    # Each page votes on its own. Summing characters across the probes once
    # sent a vertical book to Vision: its cover and a figure page full of
    # horizontal labels outweighed a body page Vision could not read at all
    # (37 characters, against 432 from a vertical reader).
    vertical_votes = horizontal_votes = 0
    for p in probes:
        vision = _char_count(native.ocr(p, langs=langs)["lines"])
        other = vertical_reader(p)
        if max(vision, other) < PROBE_MIN_CHARS:
            continue  # blank, a picture, or a chapter title: says nothing
        if other > vision * ENGINE_MARGIN:
            vertical_votes += 1
        else:
            horizontal_votes += 1
    if vertical_votes > horizontal_votes:
        return _vertical_engine(tlang)
    return "vision", tlang


def _probe_pages(images: list[Path]) -> list[Path]:
    """Pages spread over the body of the book, skipping the front and back
    matter (covers, title pages, colophons) that misrepresent it."""
    if len(images) <= PROBE_PAGES:
        return list(images)
    lo, hi = int(len(images) * 0.15), int(len(images) * 0.85)
    step = (hi - lo) / (PROBE_PAGES - 1)
    return [images[min(len(images) - 1, lo + round(i * step))]
            for i in range(PROBE_PAGES)]


def ocr_pages(outdir: Path, langs: str = "ja-JP,en-US", min_conf: float = 0.0,
              orientation: str = "auto", columns: str = "auto",
              crop: tuple[float, float, float, float] | None = None,
              jobs: int = 4, fast: bool = False, engine: str = "auto",
              scale: float = DEFAULT_SCALE, contrast: bool = True,
              binarize: bool = False, strip_ruby: bool = True,
              progress=None, on_engine=None) -> Path:
    """Run OCR over outdir/pages/*.png, writing outdir/ocr.jsonl."""
    pages_dir = Path(outdir) / "pages"
    images = sorted(pages_dir.glob("page_*.png"))
    if not images:
        raise FileNotFoundError(f"no captured pages in {pages_dir}")

    crop = DEFAULT_CROP if crop is None else crop

    with tempfile.TemporaryDirectory() as td:
        work = Path(td)

        def prepare(img: Path, ruby: bool = False) -> Path:
            return prepare_image(img, work, crop, scale, contrast, binarize,
                                 strip_ruby=ruby)

        chosen, tlang = choose_engine(images, langs, engine, orientation,
                                      prepare=prepare)
        if on_engine:
            on_engine(chosen, tlang)

        def one(idx_img):
            idx, img = idx_img
            vertical = chosen == "livetext" or (
                chosen == "tesseract" and tlang.endswith("_vert"))
            prepped = prepare(img, ruby=vertical and strip_ruby)
            if chosen == "livetext":
                # No geometry to lay out: Live Text already reads in order.
                text = native.livetext(prepped, langs=langs)["text"]
                return {
                    "index": idx,
                    "file": img.name,
                    "engine": chosen,
                    "orientation": "vertical",
                    "spread": False,
                    "lines": textclean.livetext_lines(text),
                }
            if chosen == "tesseract":
                raw = tess.ocr(prepped, lang=tlang,
                               vertical=tlang.endswith("_vert"))
                if min_conf:
                    raw["lines"] = [l for l in raw["lines"]
                                    if l["conf"] >= min_conf]
            else:
                raw = native.ocr(prepped, langs=langs, min_conf=min_conf,
                                 fast=fast)
            result = layout.analyze(raw, orientation=orientation,
                                    columns=columns)
            return {
                "index": idx,
                "file": img.name,
                "engine": chosen,
                "orientation": result["orientation"],
                "spread": result["spread"],
                "lines": [
                    {"text": textclean.normalize(l["text"]),
                     "fill": round(l.get("fill", 1.0), 3),
                     "conf": round(l.get("conf", 0.0), 3)}
                    for l in result["lines"]
                ],
            }

        if chosen == "ndlocr":
            results = _ndlocr_pages(images, work, crop, scale, contrast,
                                    binarize, strip_ruby, jobs, progress)
        else:
            with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
                results = []
                for n, page in enumerate(pool.map(one, enumerate(images, 1)), 1):
                    results.append(page)
                    if progress:
                        progress(n, len(images), page["file"])

    results.sort(key=lambda p: p["index"])
    dest = Path(outdir) / "ocr.jsonl"
    with dest.open("w", encoding="utf-8") as fh:
        for page in results:
            fh.write(json.dumps(page, ensure_ascii=False) + "\n")
    return dest


def _ndlocr_pages(images: list[Path], work: Path, crop, scale: float,
                  contrast: bool, binarize: bool, strip_ruby: bool, jobs: int,
                  progress=None) -> list[dict]:
    """Prepare every page, then run NDLOCR-Lite once over the lot.

    One run per book rather than per page, because loading its models costs
    more than reading a page.
    """
    src = work / "ndlocr-in"
    out = work / "ndlocr-out"
    src.mkdir()

    def prep(img: Path) -> Path:
        return prepare_image(img, src, crop, scale, contrast, binarize,
                             strip_ruby=strip_ruby)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        prepped = list(pool.map(prep, images))
    if any(p.parent != src for p in prepped):  # no preprocessing was asked for
        for img in images:
            (src / img.name).symlink_to(img.resolve())
        prepped = [src / img.name for img in images]

    ndlocr.ocr_dir(src, out)

    results = []
    for n, (img, prepped_img) in enumerate(zip(images, prepped), 1):
        page = ndlocr.load_page(out / (prepped_img.stem + ".json"))
        lines = [l for l in page["lines"] if l["text"].strip()]
        orientation = layout.detect_orientation(lines)
        layout.annotate_fill(lines, orientation)
        results.append({
            "index": n,
            "file": img.name,
            "engine": "ndlocr",
            "orientation": orientation,
            "spread": False,
            "lines": [
                {"text": textclean.normalize(l["text"]),
                 "fill": round(l.get("fill", 1.0), 3),
                 "conf": round(l["conf"], 3)}
                for l in lines
            ],
        })
        if progress:
            progress(n, len(images), img.name)
    return results


def load_ocr(outdir: Path) -> list[dict]:
    path = Path(outdir) / "ocr.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - run the `ocr` step first")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def assemble(pages: list[dict], fill_threshold: float = 0.90,
             keep_running: bool = False, title: str = "") -> list[dict]:
    """Return per-page paragraph lists with headers/footers removed.

    `title` is the book's title, which Kindle prints atop every page and OCR
    reads in too many variants for running-head detection to catch.
    """
    if title and not keep_running:
        pages = [{**p, "lines": _without_title(p["lines"], title)} for p in pages]
    raw_lines = [[l["text"] for l in p["lines"]] for p in pages]
    running = set() if keep_running else textclean.find_running_elements(raw_lines)
    japanese = textclean.is_japanese_document(
        [t for page in raw_lines for t in page])

    out: list[dict] = []
    for page in pages:
        kept_lines = [
            l for l in page["lines"]
            if not textclean.is_symbol_noise(l["text"], l.get("conf", 1.0),
                                             japanese)
        ]
        texts = [l["text"] for l in kept_lines]
        if page.get("engine") == "tesseract":
            texts = [textclean.fix_tesseract_artifacts(t) for t in texts]
        elif page.get("engine") == "livetext":
            texts = [textclean.fix_livetext_artifacts(t) for t in texts]
        elif page.get("engine") == "ndlocr":
            texts = [textclean.fix_ndlocr_artifacts(t) for t in texts]
        fills = [l["fill"] for l in kept_lines]
        kept = textclean.keep_indices(texts, running)
        entries = [(texts[i], fills[i]) for i in kept]
        paragraphs = textclean.build_paragraphs(entries, fill_threshold)
        out.append({"index": page["index"], "paragraphs": paragraphs})
    return out


def _without_title(lines: list[dict], title: str) -> list[dict]:
    """Line records minus the running title, keeping each line's fill/conf."""
    cuts = textclean.title_cuts([l["text"] for l in lines], title)
    return [{**line, "text": text} for line, text in zip(lines, cuts) if text is not None]


def stitch(page_paragraphs: list[dict]) -> list[dict]:
    """Merge a paragraph that was split across a page boundary."""
    merged: list[dict] = []
    for page in page_paragraphs:
        paras = list(page["paragraphs"])
        if merged and paras:
            prev = merged[-1]
            last = prev["text"]
            first = paras[0]
            # Only join when the previous page clearly ran out mid-sentence.
            if last and not last.startswith("## ") and not first.startswith("## ") \
               and not last.rstrip().endswith(("。", "．", ".", "!", "?", "！", "？",
                                               "」", "』", "”", '"', ")", "）")):
                prev["text"] = textclean._join(last, first)
                prev["page_end"] = page["index"]
                paras = paras[1:]
        for p in paras:
            merged.append({"text": p, "page_start": page["index"],
                           "page_end": page["index"]})
    return merged

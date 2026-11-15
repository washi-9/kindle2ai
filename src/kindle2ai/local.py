"""Convert DRM-free files already on disk (EPUB / PDF / TXT) with the same
output shape as a Kindle capture, so downstream tooling does not care which
path a book came through."""

from __future__ import annotations

import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from . import native, textclean

BLOCK_TAGS = {"p", "div", "br", "li", "tr", "section", "article", "blockquote",
              "h1", "h2", "h3", "h4", "h5", "h6", "pre", "td", "figcaption"}
HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
SKIP_TAGS = {"script", "style", "head", "title", "rt", "rp"}  # rt/rp = ruby readings


class _Extractor(HTMLParser):
    """Flatten XHTML to blocks, keeping heading level and dropping ruby glosses."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[int, str]] = []  # (heading_level, text)
        self._buf: list[str] = []
        self._skip = 0
        self._heading = 0

    def _flush(self) -> None:
        text = textclean.normalize("".join(self._buf))
        self._buf.clear()
        if text.strip():
            self.blocks.append((self._heading, text.strip()))

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self._heading = HEADING_TAGS.get(tag, 0)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self._heading = 0

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _xhtml_blocks(html: str) -> list[tuple[int, str]]:
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    return parser.blocks


def _opf_spine(zf: zipfile.ZipFile) -> list[str]:
    """Resolve the reading order declared in the EPUB package document."""
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = container.find(".//c:rootfile", ns)
    opf_path = rootfile.get("full-path")
    base = str(Path(opf_path).parent)

    opf = ET.fromstring(zf.read(opf_path))
    o = {"o": "http://www.idpf.org/2007/opf"}
    manifest = {
        item.get("id"): item.get("href")
        for item in opf.findall(".//o:manifest/o:item", o)
    }
    order: list[str] = []
    for ref in opf.findall(".//o:spine/o:itemref", o):
        href = manifest.get(ref.get("idref"))
        if not href:
            continue
        full = unquote(str(Path(base) / href) if base not in ("", ".") else unquote(href))
        order.append(full.lstrip("./"))
    return order


def _opf_meta(zf: zipfile.ZipFile) -> dict:
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    opf_path = container.find(".//c:rootfile", ns).get("full-path")
    opf = ET.fromstring(zf.read(opf_path))
    dc = {"dc": "http://purl.org/dc/elements/1.1/"}
    def first(tag):
        el = opf.find(f".//dc:{tag}", dc)
        return (el.text or "").strip() if el is not None and el.text else ""
    return {"title": first("title"), "author": first("creator"),
            "language": first("language")}


def read_epub(path: Path) -> tuple[dict, list[dict]]:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        meta = _opf_meta(zf)
        try:
            spine = [n for n in _opf_spine(zf) if n in names]
        except Exception:
            spine = []
        if not spine:  # malformed package document: fall back to file order
            spine = sorted(n for n in names
                           if n.lower().endswith((".xhtml", ".html", ".htm")))

        blocks: list[dict] = []
        for page_no, name in enumerate(spine, 1):
            try:
                html = zf.read(name).decode("utf-8", errors="replace")
            except KeyError:
                continue
            for level, text in _xhtml_blocks(html):
                blocks.append({
                    "text": ("#" * (level + 1) + " " + text) if level else text,
                    "page_start": page_no,
                    "page_end": page_no,
                })
    meta["pages"] = len(spine)
    return meta, blocks


def read_pdf(path: Path, ocr_fallback: bool = True, langs: str = "ja-JP,en-US",
             dpi: int = 200, workdir: Path | None = None,
             progress=None) -> tuple[dict, list[dict]]:
    """Use the embedded text layer; OCR rendered pages when there isn't one."""
    doc = native.pdf_text(path)
    pages = doc.get("pages", [])
    chars = sum(len(p["text"].strip()) for p in pages)

    if chars < 20 * max(1, len(pages)) and ocr_fallback:
        from . import layout
        workdir = Path(workdir or (path.parent / (path.stem + "_render")))
        if progress:
            progress(f"no text layer; rendering {len(pages)} pages at {dpi}dpi")
        native.pdf_render(path, workdir, dpi=dpi)
        blocks: list[dict] = []
        for n, img in enumerate(sorted(workdir.glob("page_*.png")), 1):
            result = layout.analyze(native.ocr(img, langs=langs))
            entries = [(textclean.normalize(l["text"]), l.get("fill", 1.0))
                       for l in result["lines"]]
            for para in textclean.build_paragraphs(entries):
                blocks.append({"text": para, "page_start": n, "page_end": n})
            if progress:
                progress(f"ocr page {n}")
        meta = {"title": doc.get("title") or path.stem,
                "author": doc.get("author", ""), "pages": doc.get("page_count", 0)}
        return meta, blocks

    blocks = []
    for page in pages:
        text = textclean.normalize(page["text"])
        for para in re.split(r"\n\s*\n+", text):
            para = re.sub(r"\s*\n\s*", "", para).strip()
            if para:
                blocks.append({"text": para, "page_start": page["index"],
                               "page_end": page["index"]})
    meta = {"title": doc.get("title") or path.stem,
            "author": doc.get("author", ""), "pages": doc.get("page_count", 0)}
    return meta, blocks


def read_text(path: Path) -> tuple[dict, list[dict]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    blocks = []
    for para in re.split(r"\n\s*\n+", raw):
        para = textclean.normalize(para.replace("\n", ""))
        if para.strip():
            blocks.append({"text": para.strip(), "page_start": 1, "page_end": 1})
    return {"title": path.stem, "author": "", "pages": 1}, blocks


def read_any(path: Path, **kwargs) -> tuple[dict, list[dict]]:
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return read_epub(path)
    if suffix == ".pdf":
        return read_pdf(path, **kwargs)
    if suffix in (".txt", ".md", ".text"):
        return read_text(path)
    if suffix in (".html", ".htm", ".xhtml"):
        html = path.read_text(encoding="utf-8", errors="replace")
        blocks = [{"text": ("#" * (lv + 1) + " " + t) if lv else t,
                   "page_start": 1, "page_end": 1}
                  for lv, t in _xhtml_blocks(html)]
        return {"title": path.stem, "author": "", "pages": 1}, blocks
    raise ValueError(
        f"unsupported file type: {suffix}\n"
        "Supported: .epub .pdf .txt .md .html\n"
        "DRM-protected Kindle books (.azw/.kfx) are not readable as files - "
        "use the `capture` workflow instead."
    )

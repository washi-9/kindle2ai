"""kindle2ai - turn books you own into files an AI can read."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import capture, export, library, local, native, ndlocr, pipeline, tess

DEFAULT_FORMATS = "md,jsonl,txt"


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _formats(raw: str) -> list[str]:
    allowed = {"md", "jsonl", "txt"}
    picked = [f.strip() for f in raw.split(",") if f.strip()]
    bad = [f for f in picked if f not in allowed]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown format(s): {', '.join(bad)} (choose from md, jsonl, txt)")
    return picked


# --------------------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    ok = True
    print("kindle2ai doctor")
    print("-" * 52)

    try:
        native.ensure_binary()
        print(f"[ok]   native helper   {native.BINARY}")
    except native.NativeError as e:
        print(f"[FAIL] native helper   {e}")
        return 1

    langs = native.supported_languages()
    print(f"[ok]   OCR languages   {len(langs)} available"
          f" ({'ja-JP ' if 'ja-JP' in langs else ''}{'en-US' if 'en-US' in langs else ''})")

    perms = native.request_permissions() if args.request else native.permissions()
    for key, label, why in (
        ("screen_recording", "Screen & System Audio Recording",
         "captures come out blank without it"),
        ("accessibility", "Accessibility", "page turns are ignored without it"),
    ):
        if perms.get(key):
            print(f"[ok]   permission      {label}")
        else:
            ok = False
            print(f"[FAIL] permission      {label} - {why}")
            print(f"       System Settings > Privacy & Security > {label}")
            print(f"       Add and enable your terminal app, then RESTART the terminal.")

    if ndlocr.is_ready():
        print("[ok]   vertical JP     NDLOCR-Lite (most accurate for 縦書き)")
    elif native.livetext_available():
        print("[ok]   vertical JP     Live Text (縦書き supported, no install needed)")
        print("       Optional, about 1/3 the errors: ./setup-ndlocr.sh")
    elif tess.is_ready("jpn_vert"):
        print("[ok]   vertical JP     Tesseract jpn_vert ready (縦書き supported)")
    else:
        print("[warn] vertical JP     " + tess.install_hint("jpn_vert").replace(
            "\n", "\n       "))
        print("       Horizontal books work without this; vertical ones do not.")

    wins = native.windows(capture.KINDLE_BUNDLE)
    if wins:
        w = wins[0]
        print(f"[ok]   Kindle window   id={w['id']} {int(w['width'])}x{int(w['height'])}"
              f" {w.get('title') or '(title hidden)'}")
    else:
        ok = False
        print("[FAIL] Kindle window   not found - open the Kindle app with a book open")

    if library.BOOK_DB.exists():
        try:
            books = library.list_books()
            print(f"[ok]   library index   {len(books)} books")
        except Exception as e:
            print(f"[warn] library index   {e}")
    else:
        print("[warn] library index   not found (only affects `books` listing)")

    print("-" * 52)
    print("ready" if ok else "fix the [FAIL] items above, then re-run `kindle2ai doctor`")
    if not args.request and not ok:
        print("tip: `kindle2ai doctor --request` opens the permission prompts for you")
    return 0 if ok else 1


# --------------------------------------------------------------------------- books

def cmd_books(args) -> int:
    try:
        books = library.find(args.query, args.dictionaries) if args.query \
            else library.list_books(args.dictionaries)
    except FileNotFoundError as e:
        return _err(str(e))

    if args.json:
        print(json.dumps([b.to_dict() for b in books], ensure_ascii=False, indent=2))
        return 0

    if not books:
        print("no matching books")
        return 0
    print(f"{'ASIN':<12} {'DL':<3} {'LOC':>7}  TITLE")
    for b in books:
        print(f"{b.asin:<12} {'yes' if b.downloaded else '-':<3} "
              f"{b.max_location:>7}  {b.title}")
    print(f"\n{len(books)} books. Open one in the Kindle app, then run `kindle2ai run`.")
    return 0


# --------------------------------------------------------------------------- capture

def _progress(n, name):
    print(f"  page {n:>5}  {name}", flush=True)


def _resolve_book(query: str) -> dict:
    """Identify the book being captured so the output is not named 'Kindle'.

    The app's window title is just "Kindle", but the library database tracks
    last-access time, so the book opened most recently is the one on screen.
    """
    try:
        if query:
            matches = library.find(query)
            if not matches:
                print(f"note: no library entry matches {query!r}; "
                      "using it as the title")
                return {"title": query, "asin": ""}
            if len(matches) > 1:
                print(f"note: {len(matches)} books match {query!r}; "
                      f"using {matches[0].title!r}")
            book = matches[0]
        else:
            books = library.list_books()
            if not books:
                return {}
            book = books[0]  # most recently accessed
            print(f"book: {book.title}  ({book.asin})")
            print("      wrong book? pass --book '<title>' to override")
            return {"title": book.title, "asin": book.asin}
    except Exception as e:
        print(f"note: could not read the library index ({e})")
        return {"title": query, "asin": ""} if query else {}
    print(f"book: {book.title}  ({book.asin})")
    return {"title": book.title, "asin": book.asin}


def cmd_capture(args) -> int:
    outdir = Path(args.out)
    session = capture.CaptureSession.load(outdir) if args.resume \
        else capture.CaptureSession(outdir=outdir)
    session.next_key = args.next_key
    session.delay = args.delay
    session.end_repeats = args.end_repeats
    if not session.book:
        session.book = _resolve_book(
            getattr(args, "book", "") or getattr(args, "title", ""))

    if not args.resume and session.pages_dir.exists() and any(session.pages_dir.iterdir()):
        return _err(f"{session.pages_dir} already has pages; "
                    "pass --resume to continue or choose another --out")

    try:
        window = capture.find_window()
        print(f"Kindle window {window.id} ({int(window.width)}x{int(window.height)})")
        turn = ("auto-detecting the page-turn key"
                if session.next_key == "auto" else f"'{session.next_key}' to turn")
        print(f"Capturing up to {args.pages} pages, {turn}.")
        print("Do not touch the keyboard or move the window while this runs.\n")
        pages = session.run(args.pages, window=window, progress=_progress)
    except capture.CaptureError as e:
        return _err(str(e))
    except KeyboardInterrupt:
        session.save()
        print(f"\ninterrupted - {len(session.pages)} pages kept; "
              f"resume with: kindle2ai capture --out {outdir} --resume")
        return 130

    print(f"\n{len(pages)} pages in {session.pages_dir}")
    return 0


# --------------------------------------------------------------------------- ocr

def cmd_ocr(args) -> int:
    outdir = Path(args.out)

    def prog(n, total, name):
        if n % 10 == 0 or n == total:
            print(f"  ocr {n}/{total}", flush=True)

    def announce(engine, tlang):
        detail = {"tesseract": f"Tesseract ({tlang})",
                  "livetext": "Live Text (vertical)",
                  "ndlocr": "NDLOCR-Lite (vertical)"}.get(engine, "Vision")
        steps = []
        if args.crop_top or args.crop_bottom:
            steps.append("crop")
        if args.scale != 1.0:
            steps.append(f"x{args.scale:g}")
        if args.contrast:
            steps.append("contrast")
        if args.binarize:
            steps.append("binarize")
        if args.strip_ruby and engine != "vision":
            steps.append("strip-ruby")
        print(f"engine: {detail}" + (f"  prep: {'+'.join(steps)}" if steps else ""))

    try:
        dest = pipeline.ocr_pages(
            outdir, langs=args.langs, min_conf=args.min_conf,
            orientation=args.orientation, columns=args.columns,
            crop=(args.crop_top, args.crop_bottom, args.crop_left, args.crop_right),
            jobs=args.jobs, fast=args.fast, engine=args.engine,
            scale=args.scale, contrast=args.contrast, binarize=args.binarize,
            strip_ruby=args.strip_ruby, progress=prog, on_engine=announce,
        )
    except (FileNotFoundError, native.NativeError) as e:
        return _err(str(e))
    print(f"wrote {dest}")
    return 0


# --------------------------------------------------------------------------- build

def cmd_build(args) -> int:
    outdir = Path(args.out)
    try:
        pages = pipeline.load_ocr(outdir)
    except FileNotFoundError as e:
        return _err(str(e))

    per_page = pipeline.assemble(pages, fill_threshold=args.fill_threshold,
                                 keep_running=args.keep_headers)
    blocks = pipeline.stitch(per_page)
    if not blocks:
        return _err("no text recovered - check out/pages/*.png and OCR settings")

    title, asin = args.title, args.asin
    state = outdir / "state.json"
    if state.exists():
        data = json.loads(state.read_text())
        book = data.get("book") or {}
        title = title or book.get("title") or ""
        asin = asin or book.get("asin") or ""
        window_title = data.get("window_title") or ""
        # "Kindle" is the app's own window title, not a book.
        if not title and window_title and window_title != "Kindle":
            title = window_title
    title = title or outdir.name

    meta = export.build_meta(title, "kindle-app-capture", len(pages), asin)
    written = export.write_all(outdir, meta, blocks, _formats(args.formats),
                               args.chunk_chars, args.chunk_overlap)
    chars = sum(len(b["text"]) for b in blocks)
    print(f"{len(blocks)} paragraphs, {chars:,} characters from {len(pages)} pages")
    confs = [l["conf"] for p in pages for l in p["lines"] if l.get("conf")]
    if confs:
        low = sum(1 for c in confs if c < 0.7) / len(confs)
        print(f"OCR confidence: mean {sum(confs) / len(confs):.3f}, "
              f"{low:.1%} of lines below 0.70")
    for p in written:
        print(f"  {p}")
    return 0


# --------------------------------------------------------------------------- run

def cmd_run(args) -> int:
    rc = cmd_capture(args)
    if rc not in (0, 130):
        return rc
    print()
    rc = cmd_ocr(args)
    if rc != 0:
        return rc
    print()
    return cmd_build(args)


# --------------------------------------------------------------------------- local

def cmd_local(args) -> int:
    src = Path(args.file).expanduser()
    if not src.exists():
        return _err(f"{src} not found")
    outdir = Path(args.out) if args.out else src.parent / (src.stem + "_ai")

    try:
        meta, blocks = local.read_any(
            src, **({"langs": args.langs, "dpi": args.dpi,
                     "progress": lambda m: print(f"  {m}", flush=True)}
                    if src.suffix.lower() == ".pdf" else {}))
    except (ValueError, native.NativeError) as e:
        return _err(str(e))

    if not blocks:
        return _err("no text found in file")

    full = export.build_meta(args.title or meta.get("title") or src.stem,
                             f"local:{src.suffix.lstrip('.')}",
                             meta.get("pages", 0), "",
                             {"author": meta.get("author", "")})
    written = export.write_all(outdir, full, blocks, _formats(args.formats),
                               args.chunk_chars, args.chunk_overlap)
    chars = sum(len(b["text"]) for b in blocks)
    print(f"{len(blocks)} blocks, {chars:,} characters")
    for p in written:
        print(f"  {p}")
    return 0


# --------------------------------------------------------------------------- parser

def _add_output_args(p):
    p.add_argument("--formats", default=DEFAULT_FORMATS,
                   help=f"comma list of md,jsonl,txt (default: {DEFAULT_FORMATS})")
    p.add_argument("--chunk-chars", type=int, default=2000,
                   help="target characters per JSONL chunk (default: 2000)")
    p.add_argument("--chunk-overlap", type=int, default=200,
                   help="characters of overlap between chunks (default: 200)")
    p.add_argument("--title", default="", help="override the book title")


def _add_capture_args(p):
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--pages", type=int, default=2000, help="max pages (default: 2000)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds to wait after a page turn (default: 1.0)")
    p.add_argument("--next-key", default="auto",
                   choices=["auto", *sorted(capture.KEY_CODES)],
                   help="key that turns the page; 'auto' tries right, left, "
                        "space and pagedown and keeps whichever works "
                        "(default: auto)")
    p.add_argument("--end-repeats", type=int, default=3,
                   help="identical pages before assuming the end (default: 3)")
    p.add_argument("--resume", action="store_true", help="continue a previous capture")
    p.add_argument("--book", default="",
                   help="title or ASIN of the book being captured; "
                        "defaults to the most recently opened book")


def _add_ocr_args(p):
    p.add_argument("--langs", default="ja-JP,en-US",
                   help="OCR languages, priority order (default: ja-JP,en-US)")
    p.add_argument("--min-conf", type=float, default=0.0,
                   help="drop recognitions below this confidence (default: 0.0)")
    p.add_argument("--orientation", default="auto",
                   choices=["auto", "horizontal", "vertical"],
                   help="writing direction (default: auto)")
    p.add_argument("--columns", default="auto", choices=["auto", "1", "2"],
                   help="page layout; 2 = two-page spread (default: auto)")
    p.add_argument("--crop-top", type=float, default=pipeline.DEFAULT_CROP[0],
                   help="crop this fraction off the top before OCR; the "
                        "default clears the Kindle toolbar")
    p.add_argument("--crop-bottom", type=float, default=pipeline.DEFAULT_CROP[1],
                   help="crop this fraction off the bottom (progress bar)")
    p.add_argument("--crop-left", type=float, default=0.0)
    p.add_argument("--crop-right", type=float, default=0.0)
    p.add_argument("--scale", type=float, default=pipeline.DEFAULT_SCALE,
                   help="upscale pages before OCR; Kindle body text is about "
                        "half the size the models expect, but measured accuracy "
                        "drops when upscaled (default: 1.0)")
    p.add_argument("--no-contrast", dest="contrast", action="store_false",
                   help="skip contrast stretching (the page is muted brown on "
                        "cream, which costs accuracy)")
    p.add_argument("--binarize", action="store_true",
                   help="threshold to pure black and white before OCR")
    p.add_argument("--keep-ruby", dest="strip_ruby", action="store_false",
                   help="do not blank out ruby before vertical OCR (ruby "
                        "otherwise corrupts the kanji it annotates)")
    p.add_argument("--engine", default="auto",
                   choices=["auto", "vision", "livetext", "ndlocr", "tesseract"],
                   help="OCR engine; 'auto' uses Vision for horizontal text "
                        "and, for vertical Japanese, NDLOCR-Lite if installed "
                        "(./setup-ndlocr.sh), else Live Text, else Tesseract "
                        "(default: auto)")
    p.add_argument("--jobs", type=int, default=4, help="parallel OCR workers (default: 4)")
    p.add_argument("--fast", action="store_true",
                   help="fast (lower quality) recognition")


def _add_build_args(p):
    p.add_argument("--fill-threshold", type=float, default=0.90,
                   help="line-length ratio below which a line ends a paragraph "
                        "(default: 0.90)")
    p.add_argument("--keep-headers", action="store_true",
                   help="do not strip repeated headers/footers")
    p.add_argument("--asin", default="", help="record the book's ASIN in metadata")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kindle2ai",
        description="Convert books you own into AI-readable text on macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  kindle2ai doctor --request          grant permissions and check the setup
  kindle2ai books ネットワーク          find a book in your Kindle library
  kindle2ai run --out out/mybook --pages 400 --crop-bottom 0.06
  kindle2ai run --out out/novel --next-key left --orientation vertical
  kindle2ai local ~/Downloads/doc.pdf --out out/doc
""")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check permissions and setup")
    d.add_argument("--request", action="store_true",
                   help="trigger the macOS permission prompts")
    d.set_defaults(func=cmd_doctor)

    b = sub.add_parser("books", help="list books in the local Kindle library")
    b.add_argument("query", nargs="?", default="", help="filter by title or ASIN")
    b.add_argument("--dictionaries", action="store_true", help="include dictionaries")
    b.add_argument("--json", action="store_true")
    b.set_defaults(func=cmd_books)

    c = sub.add_parser("capture", help="screenshot the Kindle app page by page")
    _add_capture_args(c)
    c.set_defaults(func=cmd_capture)

    o = sub.add_parser("ocr", help="OCR previously captured pages")
    o.add_argument("--out", required=True, help="capture directory")
    _add_ocr_args(o)
    o.set_defaults(func=cmd_ocr)

    bu = sub.add_parser("build", help="assemble OCR results into output files")
    bu.add_argument("--out", required=True, help="capture directory")
    _add_build_args(bu)
    _add_output_args(bu)
    bu.set_defaults(func=cmd_build)

    r = sub.add_parser("run", help="capture + ocr + build in one go")
    _add_capture_args(r)
    _add_ocr_args(r)
    _add_build_args(r)
    _add_output_args(r)
    r.set_defaults(func=cmd_run)

    l = sub.add_parser("local", help="convert a DRM-free EPUB / PDF / TXT file")
    l.add_argument("file")
    l.add_argument("--out", default="", help="output directory")
    l.add_argument("--langs", default="ja-JP,en-US", help="OCR languages for scanned PDFs")
    l.add_argument("--dpi", type=int, default=200, help="render DPI for scanned PDFs")
    _add_output_args(l)
    l.set_defaults(func=cmd_local)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

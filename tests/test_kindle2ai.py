"""Offline tests - no Kindle app, no screen-recording permission required.

Run: python3 -m unittest discover -s tests -v
"""

import json
import struct
import sys
import tempfile
import unittest
import zlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from kindle2ai import (capture, export, layout, local, native, ndlocr,  # noqa: E402
                       pipeline, tess, textclean)


def write_gray_png(path: Path, rows: list[list[int]]) -> None:
    """Minimal 8-bit grayscale PNG writer, so tests need no image library."""
    height, width = len(rows), len(rows[0])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def text_page(bar_lengths: list[int], width: int = 400, height: int = 400) -> list[list[int]]:
    """A white page with black bars standing in for lines of text."""
    rows = [[255] * width for _ in range(height)]
    for n, length in enumerate(bar_lengths):
        top = 20 + n * 24
        for y in range(top, min(top + 12, height)):
            for x in range(20, min(20 + length, width)):
                rows[y][x] = 0
    return rows


def box(text, x, y, w, h, conf=1.0):
    return {"text": text, "conf": conf, "x": x, "y": y, "w": w, "h": h}


class TestNormalize(unittest.TestCase):
    def test_fullwidth_alnum_becomes_halfwidth(self):
        self.assertEqual(textclean.normalize("ＡＢＣ１２３"), "ABC123")

    def test_halfwidth_katakana_becomes_fullwidth(self):
        self.assertEqual(textclean.normalize("ﾃｽﾄ"), "テスト")

    def test_japanese_punctuation_is_preserved(self):
        self.assertEqual(textclean.normalize("これは、本です。"), "これは、本です。")

    def test_fullwidth_comma_inside_latin_is_converted(self):
        self.assertEqual(textclean.normalize("English，text"), "English,text")

    def test_invisible_characters_are_dropped(self):
        self.assertEqual(textclean.normalize("a​b"), "ab")


class TestTesseractArtifacts(unittest.TestCase):
    fix = staticmethod(textclean.fix_tesseract_artifacts)

    def test_colon_runs_become_ellipsis(self):
        for seen in (":::", ":……", "::…:", ":……・"):
            self.assertEqual(self.fix(f"ところで、{seen}」"), "ところで、……」", seen)

    def test_lone_colon_and_middle_dot_are_kept(self):
        self.assertEqual(self.fix("入力:細渕"), "入力:細渕")
        self.assertEqual(self.fix("アート・ギャラリー"), "アート・ギャラリー")

    def test_doubled_small_kana_is_dropped(self):
        self.assertEqual(self.fix("どうでもいぃいい、どうせ"), "どうでもいい、どうせ")
        self.assertEqual(self.fix("市にゃやって来た"), "市にやって来た")
        self.assertEqual(self.fix("おそろしいぃ悪魔"), "おそろしい悪魔")
        self.assertEqual(self.fix("ぼんやゃやりした"), "ぼんやりした")

    def test_real_small_tsu_sequences_survive(self):
        self.assertEqual(self.fix("ガッツがある"), "ガッツがある")
        self.assertEqual(self.fix("きゃあ"), "きゃあ")


class TestLiveText(unittest.TestCase):
    def test_dot_runs_become_ellipsis(self):
        fix = textclean.fix_livetext_artifacts
        self.assertEqual(fix("飲んでも、・・・・・・神様"), "飲んでも、……神様")
        self.assertEqual(fix("アート・ギャラリー"), "アート・ギャラリー")

    def test_transcript_breaks_only_at_sentence_ends(self):
        lines = textclean.livetext_lines("メロスは両手で老爺のからだをゆす\nぶって質問を重ねた。\n「王様は、人を殺します。」")
        paragraphs = textclean.build_paragraphs([(l["text"], l["fill"]) for l in lines])
        self.assertEqual(paragraphs, ["メロスは両手で老爺のからだをゆすぶって質問を重ねた。",
                                      "「王様は、人を殺します。」"])


class TestNDLOCR(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "src" / "kindle2ai"))
        import ndlocr_runner
        self.runner = ndlocr_runner

    def test_halves_join_on_their_overlap(self):
        self.assertEqual(self.runner.join_overlapping("買いに、", "いに、はるばる"),
                         "買いに、はるばる")

    def test_join_tolerates_junk_at_the_seam(self):
        self.assertEqual(self.runner.join_overlapping("この子は、少し・", "一少しも笑って"),
                         "この子は、少しも笑って")

    def test_lone_punctuation_is_not_an_overlap(self):
        # "、" alone matching would duplicate 少し.
        self.assertEqual(
            self.runner.join_overlapping("この子は、少し、", "、少しも笑って"),
            "この子は、少しも笑って")
        self.assertIsNone(self.runner.join_overlapping("あいう", "えおか"))

    def test_decoder_loops_are_detected(self):
        looped = "この子は、 この子は、 その証拠には、 この子は、 その証拠には、"
        self.assertGreater(self.runner.repeated_span(looped), 0)
        self.assertEqual(self.runner.repeated_span(
            "メロスは激怒した。必ず、かの邪智暴虐の王を除かなければならぬと決意した。"), 0)
        self.assertEqual(self.runner.repeated_span("ぽつりぽつり雨が降り出した"), 0)

    def _item(self, text, x, y, w, h, vertical=True):
        return {"text": text, "confidence": 0.9,
                "isVertical": "true" if vertical else "false",
                "boundingBox": [[x, y], [x, y + h], [x + w, y], [x + w, y + h]]}

    def test_page_keeps_reading_order_and_marks_indented_columns(self):
        page = ndlocr.parse_page({
            "imginfo": {"img_width": 1000, "img_height": 1600},
            "contents": [[
                self._item("前の段落の続き。", 900, 100, 32, 1500),
                self._item("新しい段落が始まる", 840, 132, 32, 1468),
                self._item("走れメロス", 400, 40, 200, 30, vertical=False),
            ]]})
        texts = [l["text"] for l in page["lines"]]
        self.assertEqual(texts, ["前の段落の続き。", "　新しい段落が始まる", "走れメロス"])

    def test_spaces_beside_japanese_are_dropped(self):
        fix = textclean.fix_ndlocr_artifacts
        self.assertEqual(fix("激怒した。 必ず、 かの"), "激怒した。必ず、かの")
        self.assertEqual(fix("　段落 Kindle Store で"), "　段落Kindle Storeで")

    def test_vertical_prefers_ndlocr_when_installed(self):
        from unittest import mock
        with mock.patch.object(ndlocr, "is_ready", return_value=True):
            self.assertEqual(pipeline._vertical_engine("jpn_vert")[0], "ndlocr")
        with mock.patch.object(ndlocr, "is_ready", return_value=False), \
                mock.patch.object(native, "livetext_available", return_value=True):
            self.assertEqual(pipeline._vertical_engine("jpn_vert")[0], "livetext")


def fake_corner_ink(image):
    text = Path(image).read_text()
    return 1.0 if "+sheet" in text else 0.014 if "+toolbar" in text else 0.0


class FakeKindle:
    """Just enough of Kindle for Mac to exercise page-turn detection.

    Reproduces what broke a real capture: the first key press after focusing
    is swallowed, and the backward key on the cover jumps to the last reading
    position ("戻る") instead of doing nothing. Clicking the page toggles the
    toolbar overlay.
    """

    def __init__(self, pages=12, vertical=False, jump_to=5, swallow_first=True,
                 toolbar=False, focus_lost_at=None, dead_at=None):
        self.pages, self.pos, self.jump_to = pages, 0, jump_to
        self.forward = "left" if vertical else "right"
        self.backward = "right" if vertical else "left"
        self.swallow = swallow_first
        self.overlay = toolbar
        # Page where keys stop arriving until the reader is clicked again.
        self.focus_lost_at, self.focused = focus_lost_at, True
        # Page past which nothing turns forward at all (a stuck reader).
        self.dead_at = dead_at
        # After the last page Kindle dims the book behind its end-of-book
        # sheet; clicking in it opens the notebook, and clicking a highlight
        # there jumps back into the book.
        self.view = "book"

    def press(self, key):
        if self.swallow:
            self.swallow = False
            return
        if self.view != "book":
            if key == "escape" and self.view == "sheet":
                self.view = "book"
            return
        if key == self.forward and self.pos == self.pages - 1:
            self.view = "sheet"
            return
        if self.pos == self.focus_lost_at and self.focused:
            self.focused = False
            self.focus_lost_at = None
        if not self.focused:
            return
        if key == self.forward and self.pos == self.dead_at:
            return
        if key == self.forward:
            self.pos = min(self.pos + 1, self.pages - 1)
        elif key == self.backward:
            self.pos = self.jump_to if self.pos == 0 else self.pos - 1

    def click(self, window=None):
        if self.view == "sheet":
            self.view = "notebook"  # the click hit "ノートブックに移動"
            return
        if self.view == "notebook":
            self.view, self.pos = "book", 5  # ... and then a highlight
            return
        self.overlay = not self.overlay
        self.focused = True

    def footer(self):
        if self.view != "book":
            return ""
        return f"位置No. {self.pos * 3 + 1}/100・{self.pos * 100 // (self.pages - 1)}%"

    def screen(self):
        if self.view == "sheet":
            return f"page{self.pos}+sheet"
        if self.view == "notebook":
            return "notebook"
        return f"page{self.pos}" + ("+toolbar" if self.overlay else "")


class TestPageTurnDirection(unittest.TestCase):
    def _capture(self, kindle, max_pages=50):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            session = capture.CaptureSession(outdir=Path(td), next_key="auto")
            same = lambda a, b: Path(a).read_text() == Path(b).read_text()
            with mock.patch.object(capture, "preflight", return_value=[]), \
                    mock.patch.object(capture, "activate"), \
                    mock.patch.object(capture, "frontmost_bundle",
                                      return_value=capture.KINDLE_BUNDLE), \
                    mock.patch.object(capture, "focus_reader", side_effect=kindle.click), \
                    mock.patch.object(capture, "send_key", side_effect=kindle.press), \
                    mock.patch.object(capture.time, "sleep"), \
                    mock.patch.object(capture.native, "compare", side_effect=lambda a, b, size=256: {
                        "changed_frac": 0.0 if same(a, b) else 0.5}), \
                    mock.patch.object(capture.CaptureSession, "_shot",
                                      lambda self, wid, path: Path(path).write_text(
                                          kindle.screen() + "|" + kindle.footer())), \
                    mock.patch.object(capture, "corner_ink", fake_corner_ink), \
                    mock.patch.object(capture.CaptureSession, "_footer",
                                      lambda self, img: Path(img).read_text().split("|")[1]):
                window = capture.KindleWindow(1, "Kindle", 0, 0, 100, 100)
                session.run(max_pages, window=window)
                return session.next_key, [
                    (session.pages_dir / p["file"]).read_text().split("|")[0]
                    for p in session.pages]

    def test_left_bound_book_turns_right_from_the_cover(self):
        key, pages = self._capture(FakeKindle(vertical=False))
        self.assertEqual(key, "right")
        self.assertEqual(pages, [f"page{i}" for i in range(12)])

    def test_right_bound_book_turns_left_from_the_cover(self):
        key, pages = self._capture(FakeKindle(vertical=True))
        self.assertEqual(key, "left")
        self.assertEqual(pages, [f"page{i}" for i in range(12)])

    def test_toolbar_left_open_is_closed_before_capturing(self):
        # Kindle keeps the toolbar up after a jump; one click must not reopen it.
        key, pages = self._capture(FakeKindle(toolbar=True))
        self.assertEqual(pages, [f"page{i}" for i in range(12)])

    def test_end_of_book_sheet_ends_the_capture(self):
        # The last page is followed by Kindle's sheet; nothing after it is a page.
        kindle = FakeKindle()
        key, pages = self._capture(kindle)
        self.assertEqual(pages, [f"page{i}" for i in range(12)])
        self.assertEqual(kindle.view, "book")  # the sheet was closed, not clicked

    def test_a_stall_mid_book_is_recovered_not_taken_as_the_end(self):
        key, pages = self._capture(FakeKindle(focus_lost_at=6))
        self.assertEqual(pages, [f"page{i}" for i in range(12)])

    def test_stuck_mid_book_stops_with_an_error_instead_of_finishing(self):
        with self.assertRaises(capture.CaptureError) as ctx:
            self._capture(FakeKindle(dead_at=4))
        self.assertIn("not the end", str(ctx.exception))

    def test_resume_keeps_the_page_left_on_screen(self):
        # An error after a page turn leaves Kindle one page past the last save.
        from unittest import mock
        kindle = FakeKindle(swallow_first=False)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            session = capture.CaptureSession(outdir=out, next_key="right")
            session.pages_dir.mkdir(parents=True)
            for i in range(3):
                (session.pages_dir / f"page_{i + 1:05d}.png").write_text(
                    f"page{i}|位置No. {i * 3 + 1}/100・{i * 9}%")
                session.pages.append({"index": i + 1, "file": f"page_{i + 1:05d}.png"})
            kindle.pos = 3  # page3 is on screen but was never saved
            same = lambda a, b: Path(a).read_text() == Path(b).read_text()
            with mock.patch.object(capture, "preflight", return_value=[]), \
                    mock.patch.object(capture, "activate"), \
                    mock.patch.object(capture, "frontmost_bundle",
                                      return_value=capture.KINDLE_BUNDLE), \
                    mock.patch.object(capture, "focus_reader", side_effect=kindle.click), \
                    mock.patch.object(capture, "send_key", side_effect=kindle.press), \
                    mock.patch.object(capture.time, "sleep"), \
                    mock.patch.object(capture.native, "compare", side_effect=lambda a, b, size=256: {
                        "changed_frac": 0.0 if same(a, b) else 0.5}), \
                    mock.patch.object(capture.CaptureSession, "_shot",
                                      lambda self, wid, path: Path(path).write_text(
                                          kindle.screen() + "|" + kindle.footer())), \
                    mock.patch.object(capture, "corner_ink", fake_corner_ink), \
                    mock.patch.object(capture.CaptureSession, "_footer",
                                      lambda self, img: Path(img).read_text().split("|")[1]):
                session.run(50, window=capture.KindleWindow(1, "Kindle", 0, 0, 9, 9))
            pages = [(session.pages_dir / p["file"]).read_text().split("|")[0]
                     for p in session.pages]
            self.assertEqual(pages, [f"page{i}" for i in range(12)])

    def test_resume_keeps_the_verified_key(self):
        from unittest import mock
        from kindle2ai import cli
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "pages").mkdir()
            (out / "pages" / "page_00001.png").write_text("page0")
            (out / "state.json").write_text(json.dumps({
                "next_key": "right", "pages": [{"index": 1, "file": "page_00001.png"}],
                "book": {"title": "本", "asin": "X"}}))
            seen = {}

            def fake_run(session, *a, **k):
                seen["key"] = session.next_key
                return session.pages

            args = cli.build_parser().parse_args(
                ["capture", "--out", str(out), "--resume"])
            with mock.patch.object(capture, "find_window",
                                   return_value=capture.KindleWindow(1, "Kindle", 0, 0, 9, 9)), \
                    mock.patch.object(capture.CaptureSession, "run", fake_run):
                cli.cmd_capture(args)
            self.assertEqual(seen["key"], "right")

    def test_footer_ignores_body_text_above_it(self):
        lines = [
            {"text": "図表は131ページを参照", "x": 100, "y": 10, "w": 400, "h": 20},
            {"text": "位置No. 78/2744", "x": 300, "y": 60, "w": 150, "h": 18},
            {"text": "3%・第2章", "x": 460, "y": 61, "w": 80, "h": 18},
        ]
        footer = capture.footer_line(lines)
        self.assertEqual(footer, "位置No. 78/2744 3%・第2章")
        self.assertEqual(capture.parse_location(footer), 78)

    def test_one_low_position_reading_does_not_stop_the_capture(self):
        from unittest import mock
        session = capture.CaptureSession(outdir=Path("."))
        session.pages = [{"index": 1, "file": "first.png"}]
        readings = iter([100, 10, 101, 102])
        with mock.patch.object(session, "_same", return_value=False), \
                mock.patch.object(session, "_location", side_effect=lambda img: next(readings)):
            for _ in range(4):
                session._guard_against_looping(Path("page.png"))
        self.assertEqual(session._last_location, 102)

    def test_footer_percent_is_parsed(self):
        self.assertEqual(capture.parse_percent("10ページ中の10ページ目・21%・第2章"), 21)
        self.assertIsNone(capture.parse_location("10ページ中の10ページ目・21%・第2章"))

    def test_footer_position_is_parsed(self):
        self.assertEqual(capture.parse_location("位置No. 26/2744・1%・はじめに"), 26)
        self.assertEqual(capture.parse_location("Location 1,234 of 5,000"), 1234)
        self.assertIsNone(capture.parse_location("ページ 131"))
        self.assertIsNone(capture.parse_location("章を読み終えるまで1分・13%"))


class TestUINoise(unittest.TestCase):
    def test_kindle_chrome_is_noise(self):
        for s in ["位置No. 1,234 of 6,420", "45%", "残り 3 時間",
                  "Loc. 882", "Page 14 of 300", "  42  "]:
            self.assertTrue(textclean.is_ui_noise(s), s)

    def test_body_text_is_not_noise(self):
        for s in ["吾輩は猫である。", "The quick brown fox.", "第1章 概要"]:
            self.assertFalse(textclean.is_ui_noise(s), s)

    def test_running_header_detected_across_pages(self):
        pages = [["分散システム入門", f"本文{i}", f"{i}"] for i in range(1, 9)]
        running = textclean.find_running_elements(pages)
        self.assertIn("分散システム入門", running)
        self.assertNotIn("本文1", running)

    def test_keep_indices_strips_edges_only(self):
        lines = ["ヘッダ", "本文です", "位置No. 5"]
        kept = textclean.keep_indices(lines, {"ヘッダ"})
        self.assertEqual(kept, [1])


class TestSymbolNoise(unittest.TestCase):
    def test_border_of_symbols_is_noise(self):
        self.assertTrue(textclean.is_symbol_noise("|||==|"))
        self.assertTrue(textclean.is_symbol_noise("<<>>"))

    def test_body_text_survives(self):
        self.assertFalse(textclean.is_symbol_noise("これは本文の行です。"))
        self.assertFalse(textclean.is_symbol_noise("The quick brown fox."))

    def test_latin_only_line_kept_unless_japanese_document(self):
        self.assertFalse(textclean.is_symbol_noise("abc", 0.3, False))
        self.assertTrue(textclean.is_symbol_noise("abc", 0.3, True))

    def test_confident_latin_line_kept_even_in_japanese_document(self):
        self.assertFalse(textclean.is_symbol_noise("BCG", 0.95, True))

    def test_long_latin_line_kept_in_japanese_document(self):
        long_latin = "a mixed quotation that runs on for a while here"
        self.assertFalse(textclean.is_symbol_noise(long_latin, 0.3, True))

    def test_document_language_detection(self):
        self.assertTrue(textclean.is_japanese_document(["これは日本語の本です"]))
        self.assertFalse(textclean.is_japanese_document(["This is an English book"]))
        self.assertFalse(textclean.is_japanese_document([]))


class TestParagraphs(unittest.TestCase):
    def test_japanese_lines_join_without_spaces(self):
        got = textclean.build_paragraphs([("これは長い文章の", 0.98),
                                          ("続きである。", 0.4)])
        self.assertEqual(got, ["これは長い文章の続きである。"])

    def test_ideographic_space_starts_new_paragraph(self):
        got = textclean.build_paragraphs([("一つ目の段落。", 0.99),
                                          ("　二つ目の段落。", 0.5)])
        self.assertEqual(got, ["一つ目の段落。", "二つ目の段落。"])

    def test_short_line_ending_in_period_breaks_paragraph(self):
        got = textclean.build_paragraphs([("文の終わり。", 0.3), ("次の段落。", 0.4)])
        self.assertEqual(len(got), 2)

    def test_english_hyphen_wrap_is_rejoined(self):
        got = textclean.build_paragraphs([("A wrapped Eng-", 0.99),
                                          ("lish word.", 0.4)])
        self.assertEqual(got, ["A wrapped English word."])

    def test_english_lines_join_with_space(self):
        got = textclean.build_paragraphs([("the quick brown", 0.99), ("fox.", 0.3)])
        self.assertEqual(got, ["the quick brown fox."])

    def test_headings_promoted_to_markdown(self):
        self.assertEqual(textclean.as_markdown(["第3章 概要"]), ["## 第3章 概要"])
        self.assertEqual(textclean.as_markdown(["ふつうの本文です。"]),
                         ["ふつうの本文です。"])


class TestLayout(unittest.TestCase):
    def test_horizontal_reading_order(self):
        page = {"width": 1000, "height": 800, "lines": [
            box("second", 100, 200, 400, 40),
            box("first", 100, 100, 400, 40),
        ]}
        got = layout.analyze(page)
        self.assertEqual([l["text"] for l in got["lines"]], ["first", "second"])
        self.assertEqual(got["orientation"], "horizontal")

    def test_vertical_detected_and_read_right_to_left(self):
        page = {"width": 1000, "height": 800, "lines": [
            box("左の列です", 200, 100, 40, 500),
            box("右の列です", 700, 100, 40, 500),
        ]}
        got = layout.analyze(page)
        self.assertEqual(got["orientation"], "vertical")
        self.assertEqual([l["text"] for l in got["lines"]],
                         ["右の列です", "左の列です"])

    def test_two_page_spread_split_left_to_right(self):
        lines = []
        for i in range(4):
            lines.append(box(f"L{i}", 50, 100 + i * 50, 300, 40))
            lines.append(box(f"R{i}", 600, 100 + i * 50, 300, 40))
        got = layout.analyze({"width": 1000, "height": 800, "lines": lines})
        self.assertTrue(got["spread"])
        self.assertEqual([l["text"] for l in got["lines"]][:5],
                         ["L0", "L1", "L2", "L3", "R0"])

    def test_columns_one_disables_spread_split(self):
        lines = [box(f"L{i}", 50, 100 + i * 50, 300, 40) for i in range(4)]
        lines += [box(f"R{i}", 600, 100 + i * 50, 300, 40) for i in range(4)]
        got = layout.analyze({"width": 1000, "height": 800, "lines": lines},
                             columns="1")
        self.assertFalse(got["spread"])

    def test_fill_marks_short_lines(self):
        page = {"width": 1000, "height": 800, "lines": [
            box("full width line", 100, 100, 800, 40),
            box("short", 100, 160, 200, 40),
        ]}
        fills = [l["fill"] for l in layout.analyze(page)["lines"]]
        self.assertGreater(fills[0], 0.9)
        self.assertLess(fills[1], 0.5)


class TestPipeline(unittest.TestCase):
    def _page(self, index, lines):
        return {"index": index, "file": f"page_{index:05d}.png",
                "orientation": "horizontal", "spread": False,
                "lines": [{"text": t, "fill": f} for t, f in lines]}

    def test_running_header_removed_from_every_page(self):
        pages = [self._page(i, [("書名ヘッダ", 0.3),
                                (f"本文の{i}ページ目です。", 0.4),
                                (str(i), 0.1)]) for i in range(1, 7)]
        out = pipeline.assemble(pages)
        for page in out:
            self.assertEqual(page["paragraphs"], [f"本文の{page['index']}ページ目です。"])

    def test_paragraph_stitched_across_page_boundary(self):
        pages = [self._page(1, [("前のページで途中まで書かれた", 0.99)]),
                 self._page(2, [("文章の続きである。", 0.4)])]
        blocks = pipeline.stitch(pipeline.assemble(pages))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["text"], "前のページで途中まで書かれた文章の続きである。")
        self.assertEqual((blocks[0]["page_start"], blocks[0]["page_end"]), (1, 2))

    def test_completed_sentence_does_not_stitch(self):
        pages = [self._page(1, [("ここで文が終わる。", 0.4)]),
                 self._page(2, [("新しい段落が始まる。", 0.4)])]
        blocks = pipeline.stitch(pipeline.assemble(pages))
        self.assertEqual(len(blocks), 2)


class TestPageChangeDetection(unittest.TestCase):
    """Guards the bug that made capture stop after one page.

    A 64-bit perceptual hash cannot tell two pages of body text apart: shrunk to
    8x8 every page is the same grey block, so the end-of-book check fired on
    page two. Detection must compare enough pixels to see the text itself.
    """

    LINES_A = [340, 350, 330, 345, 120, 340, 335, 200]
    LINES_B = [200, 330, 345, 340, 350, 90, 340, 310]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.a, self.b = base / "a.png", base / "b.png"
        write_gray_png(self.a, text_page(self.LINES_A))
        write_gray_png(self.b, text_page(self.LINES_B))

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_pages_compare_as_unchanged(self):
        result = native.compare(self.a, self.a)
        self.assertEqual(result["changed_frac"], 0.0)
        self.assertLess(result["changed_frac"], capture.SAME_PAGE)

    def test_different_pages_exceed_the_same_page_threshold(self):
        changed = native.compare(self.a, self.b)["changed_frac"]
        self.assertGreater(changed, capture.SAME_PAGE)
        # Comfortably clear of the threshold, not scraping past it.
        self.assertGreater(changed, capture.SAME_PAGE * 3)

    def test_settle_threshold_is_below_the_same_page_threshold(self):
        self.assertLess(capture.SETTLED, capture.SAME_PAGE)


class TestTesseractBackend(unittest.TestCase):
    def test_japanese_words_join_without_spaces(self):
        self.assertEqual(tess._join_words(["論点", "思考", "と", "は"]), "論点思考とは")

    def test_latin_words_keep_their_spaces(self):
        self.assertEqual(tess._join_words(["the", "quick", "fox"]), "the quick fox")

    def test_mixed_script_joins_on_the_japanese_side(self):
        self.assertEqual(tess._join_words(["日本語", "and", "English"]),
                         "日本語and English")

    def test_vertical_japanese_selects_the_vert_model(self):
        self.assertEqual(pipeline._tess_lang("ja-JP,en-US", vertical=True), "jpn_vert")

    def test_horizontal_japanese_selects_the_plain_model(self):
        self.assertEqual(pipeline._tess_lang("ja-JP,en-US", vertical=False), "jpn")

    def test_non_japanese_languages_map_through(self):
        self.assertEqual(pipeline._tess_lang("ko-KR", vertical=True), "kor")
        self.assertEqual(pipeline._tess_lang("en-US", vertical=True), "eng")

    def test_engine_override_skips_probing(self):
        # Must not touch the filesystem or run OCR when the engine is forced.
        self.assertEqual(pipeline.choose_engine([], "ja-JP", "vision", "auto")[0],
                         "vision")
        self.assertEqual(pipeline.choose_engine([], "ja-JP", "tesseract", "auto")[0],
                         "tesseract")
        self.assertEqual(pipeline.choose_engine([], "ja-JP", "livetext", "auto")[0],
                         "livetext")

    def test_vertical_prefers_live_text_and_falls_back_to_tesseract(self):
        from unittest import mock
        with mock.patch.object(ndlocr, "is_ready", return_value=False):
            with mock.patch.object(native, "livetext_available", return_value=True):
                self.assertEqual(pipeline._vertical_engine("jpn_vert")[0], "livetext")
            with mock.patch.object(native, "livetext_available", return_value=False):
                self.assertEqual(pipeline._vertical_engine("jpn_vert")[0], "tesseract")


class TestExport(unittest.TestCase):
    def _blocks(self, n, size=100):
        return [{"text": "あ" * size, "page_start": i, "page_end": i}
                for i in range(1, n + 1)]

    def test_chunks_respect_target_size(self):
        chunks = export.chunk(self._blocks(10), target_chars=250, overlap_chars=0)
        self.assertTrue(all(c["chars"] <= 350 for c in chunks))
        self.assertGreater(len(chunks), 1)

    def test_chunking_covers_every_block(self):
        blocks = [{"text": f"段落{i}", "page_start": i, "page_end": i}
                  for i in range(1, 21)]
        chunks = export.chunk(blocks, target_chars=20, overlap_chars=0)
        joined = "".join(c["text"] for c in chunks)
        for i in range(1, 21):
            self.assertIn(f"段落{i}", joined)

    def test_chunking_terminates_with_overlap(self):
        chunks = export.chunk(self._blocks(30), target_chars=150, overlap_chars=100)
        self.assertLess(len(chunks), 100)
        self.assertGreater(len(chunks), 1)

    def test_write_all_produces_requested_formats(self):
        with tempfile.TemporaryDirectory() as td:
            meta = export.build_meta("テスト本", "unit-test", 3, "B0TEST")
            written = export.write_all(Path(td), meta, self._blocks(3),
                                       ["md", "jsonl", "txt"])
            names = sorted(p.name for p in written)
            self.assertEqual(names, ["テスト本.jsonl", "テスト本.md", "テスト本.txt"])
            md = (Path(td) / "テスト本.md").read_text(encoding="utf-8")
            self.assertIn('title: "テスト本"', md)
            self.assertIn("<!-- page 1 -->", md)

    def test_slug_strips_path_separators(self):
        self.assertNotIn("/", export._slug("a/b:c*d"))


class TestEpub(unittest.TestCase):
    def _build(self, path):
        container = ('<?xml version="1.0"?><container version="1.0" '
                     'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                     '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                     'media-type="application/oebps-package+xml"/></rootfiles></container>')
        opf = ('<?xml version="1.0" encoding="utf-8"?>'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
               'unique-identifier="id"><metadata '
               'xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<dc:title>テスト書籍</dc:title><dc:creator>著者名</dc:creator>'
               '<dc:language>ja</dc:language></metadata><manifest>'
               '<item id="b" href="b.xhtml" media-type="application/xhtml+xml"/>'
               '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
               '</manifest><spine><itemref idref="a"/><itemref idref="b"/>'
               '</spine></package>')
        a = ('<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>第1章 導入</h1>'
             '<p>本文A。<ruby>漢字<rt>かんじ</rt></ruby>。</p>'
             '<style>p{color:red}</style></body></html>')
        b = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>本文B。</p></body></html>'
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", container)
            z.writestr("OEBPS/content.opf", opf)
            z.writestr("OEBPS/a.xhtml", a)
            z.writestr("OEBPS/b.xhtml", b)

    def test_metadata_spine_order_and_ruby_stripping(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "t.epub"
            self._build(path)
            meta, blocks = local.read_epub(path)
            self.assertEqual(meta["title"], "テスト書籍")
            self.assertEqual(meta["author"], "著者名")
            texts = [b["text"] for b in blocks]
            self.assertEqual(texts[0], "## 第1章 導入")
            self.assertIn("本文A。漢字。", texts)       # ruby reading dropped
            self.assertNotIn("かんじ", "".join(texts))
            self.assertNotIn("color:red", "".join(texts))  # <style> dropped
            self.assertLess(texts.index("本文A。漢字。"), texts.index("本文B。"))

    def test_unsupported_extension_explains_drm(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "book.azw3"
            path.write_bytes(b"x")
            with self.assertRaises(ValueError) as ctx:
                local.read_any(path)
            self.assertIn("capture", str(ctx.exception))


class TestAozoraGroundTruth(unittest.TestCase):
    """The evaluation harness must not invent or hide errors of its own."""

    def setUp(self):
        import aozora
        self.aozora = aozora

    def _write(self, body: str) -> Path:
        header = ("タイトル\n著者\n\n" + "-" * 20 + "\n"
                  "【テキスト中に現れる記号について】\n" + "-" * 20 + "\n")
        footer = "\n底本：「どこかの本」出版社\n"
        path = Path(self.tmp.name) / "work.txt"
        path.write_bytes((header + body + footer).encode("cp932"))
        return path

    def test_header_and_colophon_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            self.tmp = type("T", (), {"name": td})
            parsed = self.aozora.parse(self._write("本文だけが残る。"))
            self.assertEqual(parsed["without_ruby"], "本文だけが残る。")

    def test_ruby_is_split_into_two_readings(self):
        with tempfile.TemporaryDirectory() as td:
            self.tmp = type("T", (), {"name": td})
            parsed = self.aozora.parse(self._write("邪智暴虐《じゃちぼうぎゃく》の王。"))
            self.assertEqual(parsed["without_ruby"], "邪智暴虐の王。")
            self.assertEqual(parsed["with_ruby"], "邪智暴虐じゃちぼうぎゃくの王。")

    def test_editorial_annotations_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            self.tmp = type("T", (), {"name": td})
            parsed = self.aozora.parse(self._write("見出し［＃「見出し」は大見出し］本文。"))
            self.assertEqual(parsed["without_ruby"], "見出し本文。")


class TestAlignment(unittest.TestCase):
    def setUp(self):
        from align import align
        self.align = align
        # Varied text: a repeated sentence shares no unique n-grams, so there
        # would be nothing to anchor on.
        import random
        rng = random.Random(42)
        words = ["制約", "設計", "観測", "収束", "分解", "記述", "判断", "余白",
                 "手順", "検証", "輪郭", "頻度", "誤差", "単位", "境界"]
        self.truth = "".join(
            rng.choice(words) + rng.choice("はをがにのへと") + rng.choice(words) + "。"
            for _ in range(120))

    def _kinds(self, ops):
        from collections import Counter
        return Counter(o.kind for o in ops)

    def test_identical_text_has_no_errors(self):
        ops, _ = self.align(self.truth, self.truth, anchor_n=8)
        self.assertEqual(self._kinds(ops)["sub"], 0)
        self.assertEqual(self._kinds(ops)["ins"], 0)
        self.assertEqual(self._kinds(ops)["del"], 0)

    def test_substitutions_are_counted_and_identified(self):
        seen = self.truth.replace("猫", "描")
        ops, _ = self.align(self.truth, seen, anchor_n=8)
        subs = [(o.truth, o.seen) for o in ops if o.kind == "sub"]
        self.assertEqual(len(subs), self.truth.count("猫"))
        self.assertTrue(all(s == ("猫", "描") for s in subs))

    def test_partial_capture_aligns_to_its_own_region(self):
        seen = self.truth[200:600]
        ops, (start, end) = self.align(self.truth, seen, anchor_n=8)
        self.assertGreaterEqual(start, 150)
        self.assertLessEqual(end, 650)
        self.assertEqual(self._kinds(ops)["sub"], 0)

    def test_trailing_matter_after_the_text_is_not_scored(self):
        # A colophon or end-of-book page follows the last line of the work.
        seen = self.truth + "底本:新潮文庫入力:校正:青空文庫作成ファイル" * 3
        ops, _ = self.align(self.truth, seen, anchor_n=8)
        self.assertEqual(self._kinds(ops)["ins"], 0)

    def test_unrelated_text_is_rejected_rather_than_scored(self):
        from align import AlignmentFailed
        unrelated = "まったく関係のない文章がここに並んでいるだけである。" * 20
        with self.assertRaises(AlignmentFailed):
            self.align(self.truth, unrelated, anchor_n=8)

    def test_inserted_characters_are_reported_as_extra(self):
        # Sparse enough that clean n-grams still exist to anchor on; dense
        # errors legitimately defeat anchoring and are rejected instead.
        seen = "".join(ch + ("〓" if i % 40 == 39 else "")
                       for i, ch in enumerate(self.truth))
        ops, _ = self.align(self.truth, seen, anchor_n=8)
        self.assertGreaterEqual(self._kinds(ops)["ins"], 10)
        self.assertEqual(self._kinds(ops)["sub"], 0)


if __name__ == "__main__":
    unittest.main()

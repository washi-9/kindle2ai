"""Drive the Kindle for Mac app: focus it, screenshot the window, turn the page.

Nothing here touches the book files on disk. It reads the same pixels a person
reading the book would see, which is why it works regardless of book format.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import native

KINDLE_BUNDLE = "com.amazon.Lassen"

# macOS virtual key codes.
KEY_CODES = {
    "right": 124, "left": 123, "down": 125, "up": 126,
    "space": 49, "pagedown": 121, "pageup": 116, "escape": 53,
}

# Fraction of pixels that must change for two captures to count as different
# pages. Distinct pages of body text measure ~0.06; a settled, identical page
# measures 0.0. The gap is wide, so the exact value is not delicate.
SAME_PAGE = 0.010
SETTLED = 0.004

# Adopting a page-turn key needs a higher bar than merely detecting change, so
# a toolbar fading in is never mistaken for a working key. Measured on a real
# book: the correct key moves 12-73% of pixels, a wrong key moves 0%.
TURN_CONFIRM = 0.030

# Right-bound (vertical Japanese) books turn with the left arrow, left-bound
# books with the right. Trying both is more reliable than guessing from
# metadata, which is often absent or wrong. Each candidate is paired with the
# key that should undo it, which the direction check needs.
TURN_KEYS = ["right", "left", "pagedown"]
BACK_KEY = {"right": "left", "left": "right", "pagedown": "pageup",
            "down": "up", "up": "down", "pageup": "pagedown"}

# A wrong-direction key is not always inert: on the cover, the backward key can
# trigger Kindle's "jump back" to the last reading position. That changes the
# screen just like a page turn, which is how a backward key was once adopted
# and the capture cycled cover -> 位置26 -> 8 -> 1 -> cover forever.
# Kindle's footer can show the position ("位置No. 26/2744", "Location 26 of
# 2744"); when it does, it settles the direction outright. Page counts are not
# used: "10ページ中の10ページ目" stays at 10 for a hundred pages, and a bare
# "ページ" or "Page" also matches body text.
_LOCATION = re.compile(r"(?:位置\s*No\.?|Location|Loc\.)\s*([\d,]+)", re.I)

_PERCENT = re.compile(r"(\d{1,3})\s*%")

# How many presses `_return_to` may spend walking back to the start page.
RETURN_LIMIT = 30

# Kindle's toolbar puts a back arrow and a contents icon in the window's top-
# left corner; with the toolbar hidden that corner is blank margin. Measured:
# 0.014 of the corner is dark with the toolbar up, exactly 0 without.
TOOLBAR_CORNER = (0.0, 0.035, 0.06, 0.035)
TOOLBAR_INK = 0.003

# When Kindle puts a sheet over the book -- the end-of-book screen with review,
# "メモとハイライト" and "ノートブックに移動", or any other dialog -- it dims
# everything behind it, and the same corner turns almost solid (measured 1.0).
# A capture must stop there: it once pressed on, and its recovery click landed
# on "ノートブックに移動", then on a highlight, and resumed capturing from 20%.
MODAL_INK = 0.5

# The percentage at or above which Kindle's end-of-book sheet is the expected
# thing to appear. OCR of the footer's percentage is unreliable ("100%" reads
# as "10%", "90%" as "00%"), so the highest plausible reading is used.
FINISHED_PERCENT = 90

# A capture that stops changing below this share of the book is stuck, not
# finished. Kindle often reports 99% on the last page, never less than 97.
END_PERCENT = 97

# Keys tried when the usual one stops turning the page mid-book.
FALLBACK_KEYS = ["pagedown", "space"]


class CaptureError(RuntimeError):
    pass


def footer_line(lines: list[dict]) -> str:
    """Join the OCR boxes that sit on the same row as the footer's "N%"."""
    anchors = [l for l in lines if _PERCENT.search(l["text"])]
    if not anchors:
        return ""
    anchor = max(anchors, key=lambda l: l["y"])  # the footer is the lowest
    mid = anchor["y"] + anchor["h"] / 2
    row = [l for l in lines if abs(l["y"] + l["h"] / 2 - mid) <= anchor["h"] * 0.6]
    return " ".join(l["text"] for l in sorted(row, key=lambda l: l["x"]))


def parse_percent(footer: str) -> int | None:
    """'10ページ中の10ページ目・21%・第2章' -> 21."""
    m = _PERCENT.search(footer)
    return int(m.group(1)) if m else None


def corner_ink(image: Path) -> float:
    return native.ink(image, TOOLBAR_CORNER)


def toolbar_visible(image: Path) -> bool:
    return TOOLBAR_INK < corner_ink(image) < MODAL_INK


def parse_location(footer: str) -> int | None:
    """'位置No. 26/2744・1%' -> 26. None when the footer shows no position
    (e.g. only '章を読み終えるまで1分・13%')."""
    m = _LOCATION.search(footer)
    return int(m.group(1).replace(",", "")) if m else None


@dataclass
class KindleWindow:
    id: int
    title: str
    x: float
    y: float
    width: float
    height: float


def find_window(bundle: str = KINDLE_BUNDLE) -> KindleWindow:
    wins = native.windows(bundle)
    if not wins:
        raise CaptureError(
            "No Kindle window found. Open the Kindle app and open a book first.\n"
            "(If the app is open, grant Screen Recording to your terminal "
            "or Kindle2AI.app.)"
        )
    w = wins[0]  # already sorted largest-first
    return KindleWindow(id=w["id"], title=w.get("title", ""),
                        x=w["x"], y=w["y"],
                        width=w["width"], height=w["height"])


def activate(bundle: str = KINDLE_BUNDLE) -> None:
    native.osascript(f'tell application id "{bundle}" to activate')
    time.sleep(0.4)


def focus_reader(window: KindleWindow) -> None:
    """Click inside the page area so arrow keys reach the reader.

    Activating the app is not enough: if keyboard focus sits on a toolbar
    control, every arrow key is swallowed and the book never advances. A click
    in the lower page margin -- below the text, above the progress bar -- puts
    focus back on the reader without selecting a word.
    """
    x = int(window.x + window.width / 2)
    y = int(window.y + window.height * 0.88)
    native.osascript(f'tell application "System Events" to click at {{{x}, {y}}}')
    time.sleep(0.8)


def frontmost_bundle() -> str:
    return native.osascript(
        'tell application "System Events" to get bundle identifier of '
        "(first application process whose frontmost is true)"
    )


def send_key(key: str) -> None:
    code = KEY_CODES.get(key)
    if code is None:
        raise CaptureError(f"unknown key: {key} (choose from {', '.join(KEY_CODES)})")
    native.osascript(f'tell application "System Events" to key code {code}')


def preflight() -> list[str]:
    """Return human-readable problems that would make a capture silently fail."""
    problems: list[str] = []
    perms = native.permissions()
    if not perms.get("screen_recording"):
        problems.append(
            "Screen Recording not granted -> captures would be blank.\n"
            "  System Settings > Privacy & Security > Screen & System Audio Recording\n"
            "  Enable the app you run this from (your terminal, or Kindle2AI),\n"
            "  then restart it. If it is already on but still not recognized,\n"
            "  remove it with '-' and add it again with '+': a rebuilt app no\n"
            "  longer matches the old entry."
        )
    if not perms.get("accessibility"):
        problems.append(
            "Accessibility not granted -> page turns would do nothing.\n"
            "  System Settings > Privacy & Security > Accessibility\n"
            "  Enable the app you run this from (your terminal, or Kindle2AI),\n"
            "  then restart it. If it is already on but still not recognized,\n"
            "  remove it with '-' and add it again with '+': a rebuilt app no\n"
            "  longer matches the old entry."
        )
    return problems


@dataclass
class CaptureSession:
    outdir: Path
    next_key: str = "right"
    delay: float = 1.0
    settle_timeout: float = 4.0
    end_repeats: int = 3
    book: dict = field(default_factory=dict)
    pages: list[dict] = field(default_factory=list)
    _last_location: int | None = field(default=None, repr=False)
    _backwards: bool = field(default=False, repr=False)
    _last_percent: int | None = field(default=None, repr=False)
    # Corner ink of the book's own pages; a dark reading theme is already
    # near 1.0 there, which disables the dimmed-sheet check rather than
    # mistaking every page for one.
    _baseline_corner: float = field(default=0.0, repr=False)

    @property
    def pages_dir(self) -> Path:
        return self.outdir / "pages"

    @property
    def state_path(self) -> Path:
        return self.outdir / "state.json"

    @classmethod
    def load(cls, outdir: Path) -> "CaptureSession":
        path = Path(outdir) / "state.json"
        if not path.exists():
            return cls(outdir=Path(outdir))
        data = json.loads(path.read_text())
        s = cls(outdir=Path(outdir))
        s.next_key = data.get("next_key", "right")
        s.delay = data.get("delay", 1.0)
        s.book = data.get("book", {})
        s.pages = data.get("pages", [])
        return s

    def save(self, extra: dict | None = None) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        data = {
            "next_key": self.next_key,
            "delay": self.delay,
            "book": self.book,
            "pages": self.pages,
            **(extra or {}),
        }
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    # -- capture primitives -------------------------------------------------

    def _shot(self, window_id: int, path: Path) -> None:
        native.screencapture_window(window_id, path)

    def _capture_settled(self, window_id: int, path: Path, scratch: Path) -> None:
        """Re-shoot until two consecutive frames match, so page-turn animations
        and lazily rendered images are never captured mid-flight."""
        deadline = time.time() + self.settle_timeout
        self._shot(window_id, path)
        while time.time() < deadline:
            time.sleep(0.3)
            self._shot(window_id, scratch)
            if native.compare(path, scratch)["changed_frac"] < SETTLED:
                path.write_bytes(scratch.read_bytes())
                return
            path.write_bytes(scratch.read_bytes())

    def _last_page_path(self) -> Path | None:
        if not self.pages:
            return None
        return self.pages_dir / self.pages[-1]["file"]

    def _footer(self, image: Path) -> str:
        """The text of Kindle's footer line (position, percent, chapter).

        The bottom strip also holds the last lines of the page, whose numbers
        once passed for a position (131 -> 78) and stopped a good capture. The
        footer always carries a percentage, so only text on that line is kept.
        """
        strip = self.outdir / "_footer.png"
        try:
            native.prep(image, strip, crop=(0.88, 0.0, 0.0, 0.0))
            lines = native.ocr(strip, langs="ja-JP,en-US")["lines"]
        except native.NativeError:
            return ""
        finally:
            strip.unlink(missing_ok=True)
        return footer_line(lines)

    def _location(self, image: Path) -> int | None:
        """Kindle's position number from the footer, if the footer shows one."""
        return parse_location(self._footer(image))

    def _percent(self, image: Path) -> int | None:
        return parse_percent(self._footer(image))

    def _turn(self, key: str, window: KindleWindow, ref: Path, dest: Path,
              scratch: Path, tries: int = 2) -> bool:
        """Press `key` until the screen differs from `ref` (at most `tries`
        times: the first press after focusing is sometimes swallowed)."""
        for _ in range(tries):
            send_key(key)
            time.sleep(self.delay)
            self._capture_settled(window.id, dest, scratch)
            if native.compare(dest, ref)["changed_frac"] >= TURN_CONFIRM:
                return True
        return False

    def _same(self, a: Path, b: Path) -> bool:
        return native.compare(a, b)["changed_frac"] < SAME_PAGE

    def _return_to(self, start: Path, keys: list[str], window: KindleWindow,
                   probe: Path, scratch: Path) -> bool:
        """Walk back to the start page, trying each key in turn until the
        screen matches it or the key stops moving anything."""
        self._capture_settled(window.id, probe, scratch)
        if self._same(probe, start):
            return True
        before = self.outdir / "_before.png"
        try:
            for key in keys:
                for _ in range(RETURN_LIMIT):
                    before.write_bytes(probe.read_bytes())
                    send_key(key)
                    time.sleep(self.delay)
                    self._capture_settled(window.id, probe, scratch)
                    if self._same(probe, start):
                        return True
                    if self._covered_by_sheet(probe):
                        send_key("escape")  # walked off the end of the book
                        self._capture_settled(window.id, probe, scratch)
                        break
                    if self._same(probe, before):
                        break  # this key goes no further
        finally:
            before.unlink(missing_ok=True)
        return False

    def _detect_turn_key(self, window: KindleWindow, probe: Path,
                         scratch: Path, progress=None) -> str:
        """Work out which key turns forward, then come back to the start page.

        A key counts as forward only if pressing it twice and its opposite
        twice retraces the same pages back to the start: a backward key that
        jumps from the cover to the last reading position fails that, because
        its opposite does not jump back. When the footer shows a position
        number, it must also increase.
        """
        start = self._last_page_path()
        first = self.outdir / "_turn1.png"
        second = self.outdir / "_turn2.png"
        start_loc = self._location(start)
        self._last_location = start_loc
        try:
            for key in TURN_KEYS:
                back = BACK_KEY[key]
                if not self._turn(key, window, start, first, scratch):
                    continue  # does nothing here (e.g. backward on the cover)
                ok = self._turn(key, window, first, second, scratch, tries=1)
                if ok:
                    ok = (self._turn(back, window, second, probe, scratch, tries=1)
                          and self._same(probe, first)
                          and self._turn(back, window, first, probe, scratch, tries=1)
                          and self._same(probe, start))
                if ok and start_loc is not None:
                    loc = self._location(second)
                    ok = loc is None or loc > start_loc
                if ok:
                    self.next_key = key
                    if progress:
                        progress(len(self.pages), f"page-turn key: '{key}' "
                                 "(verified forward; starting from the first page)")
                    return key
                if progress:
                    progress(len(self.pages), f"'{key}' does not turn forward; "
                             "returning to the first page")
                # A key that failed the check usually went backwards (e.g. a
                # jump from the cover), so walking on with it leads home.
                if not self._return_to(start, [key, back], window, probe, scratch):
                    raise CaptureError(
                        "Lost the starting page while working out the page-turn "
                        "direction.\n"
                        "  Show the page you want to start from in Kindle again, "
                        "then retry.")
        finally:
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
        raise CaptureError(
            "No key turned the page forward (tried: " + ", ".join(TURN_KEYS) + ").\n"
            "  - If the book is on its last page, go back to the beginning\n"
            "  - Click once inside the Kindle page area yourself, then retry\n"
            "  - Check Accessibility permission: kindle2ai doctor"
        )

    def _accept(self, probe: Path) -> dict:
        index = len(self.pages) + 1
        dest = self.pages_dir / f"page_{index:05d}.png"
        dest.write_bytes(probe.read_bytes())
        entry = {"index": index, "file": dest.name}
        self.pages.append(entry)
        return entry

    def _focus(self, window: KindleWindow, probe: Path, scratch: Path) -> None:
        """Click the reader so keys reach it, without leaving its toolbar open.

        The click lands in the page view, which toggles Kindle's toolbar and
        progress-bar overlay. Left open, the overlay is captured on every page
        and its controls can take the arrow keys -- a capture once stalled at
        a chapter start with the toolbar up and was reported as the end.
        """
        self._capture_settled(window.id, probe, scratch)
        if self._covered_by_sheet(probe):
            return  # a sheet is open: clicking would press one of its buttons
        focus_reader(window)
        self._capture_settled(window.id, probe, scratch)
        # Each click toggles the toolbar, and it may have been up already
        # (Kindle leaves it open after a jump, with a 「戻る」 thumbnail), so
        # click until it is actually gone rather than assuming one state.
        for _ in range(2):
            if not toolbar_visible(probe):
                break
            focus_reader(window)
            self._capture_settled(window.id, probe, scratch)

    def _rejoin(self, window: KindleWindow, probe: Path, scratch: Path,
                progress=None) -> None:
        """Line a resumed capture up with the pages already saved.

        A run that stopped on an error has usually turned to the next page
        without saving it; pressing on from there would skip that page. So:
        on the last saved page -> carry on; one page past it -> keep the page
        on screen first; anywhere else -> ask for the right page.
        """
        last = self._last_page_path()
        self._capture_settled(window.id, probe, scratch)
        if self._same(probe, last):
            return
        ahead = self.outdir / "_ahead.png"
        ahead.write_bytes(probe.read_bytes())
        try:
            back = BACK_KEY.get(self.next_key)
            if back and self._turn(back, window, ahead, probe, scratch, tries=1) \
                    and self._same(probe, last):
                self._turn(self.next_key, window, last, probe, scratch)
                if self._same(probe, ahead):
                    self._guard_against_looping(probe)
                    entry = self._accept(probe)
                    self.save()
                    if progress:
                        progress(entry["index"], entry["file"] + " (page left on screen)")
                    return
            raise CaptureError(
                f"Kindle is not showing the last captured page (page "
                f"{len(self.pages)}).\n"
                f"  Show that page in Kindle (see {last}) and resume again.")
        finally:
            ahead.unlink(missing_ok=True)

    def _try_fallback_keys(self, window: KindleWindow, probe: Path,
                           scratch: Path, progress=None) -> bool:
        """When the usual key stops working mid-book, try the other forward
        keys. A page they reach is kept only if it is new and Kindle's
        position did not go backwards."""
        last = self._last_page_path()
        before = self._location(last)
        for key in FALLBACK_KEYS:
            if key == self.next_key:
                continue
            if not self._turn(key, window, last, probe, scratch, tries=1):
                continue
            loc = self._location(probe)
            if before is not None and loc is not None and loc < before:
                self._return_to(last, [self.next_key, BACK_KEY.get(key, key)],
                                window, probe, scratch)
                continue
            if progress:
                progress(len(self.pages), f"'{self.next_key}' stopped turning the "
                         f"page; continuing with '{key}'")
            self.next_key = key
            self._guard_against_looping(probe)
            entry = self._accept(probe)
            if progress:
                progress(entry["index"], entry["file"])
            return True
        return False

    def _covered_by_sheet(self, image: Path) -> bool:
        """True when Kindle has dimmed the book behind a sheet or dialog."""
        return self._baseline_corner < MODAL_INK <= corner_ink(image)

    def _leave_sheet(self, progress=None) -> None:
        """Kindle covered the book with a sheet: finish if the book was ending,
        otherwise stop and say so. Never click into it."""
        send_key("escape")  # close the sheet; Escape does nothing else in the reader
        if self._last_percent is None or self._last_percent >= FINISHED_PERCENT:
            if progress:
                progress(len(self.pages), "end of book (Kindle showed its "
                         "end-of-book screen)")
            return
        raise CaptureError(
            f"Kindle opened a dialog at {self._last_percent}% of the book; stopped.\n"
            "  Close it in Kindle, show the last captured page, and resume.")

    def _note_percent(self, page: Path) -> None:
        """Remember the highest plausible percentage read from the footer."""
        percent = self._percent(page)
        if percent is None:
            return
        # A reading far below the last one is a misread ("100%" -> "10%").
        if self._last_percent is None or percent >= self._last_percent - 3:
            self._last_percent = max(percent, self._last_percent or 0)

    def _guard_against_looping(self, page: Path) -> None:
        """Stop rather than capture the same pages round and round.

        A forward capture never shows the first page again, and Kindle's
        position number never goes down.
        """
        first = self.pages_dir / self.pages[0]["file"]
        if len(self.pages) > 1 and self._same(page, first):
            raise CaptureError(
                "The first page came round again, so the capture is going in a "
                "circle; stopped.\n"
                "  Show the first page in Kindle and retry (the page-turn "
                "direction is detected again).")
        self._note_percent(page)
        loc = self._location(page)
        if loc is None:
            return
        if self._last_location is not None and loc < self._last_location:
            # One low reading can be a misread digit; two in a row is not.
            if self._backwards:
                raise CaptureError(
                    f"Kindle's position went backwards ({self._last_location} -> "
                    f"{loc}), so pages are being turned the wrong way; stopped.\n"
                    "  Show the first page in Kindle and retry.")
            self._backwards = True
            return
        self._backwards = False
        self._last_location = loc

    # -- main loop ----------------------------------------------------------

    def run(self, max_pages: int, window: KindleWindow | None = None,
            progress=None) -> list[dict]:
        problems = preflight()
        if problems:
            raise CaptureError("\n\n".join(problems))

        window = window or find_window()
        activate()
        if frontmost_bundle() != KINDLE_BUNDLE:
            raise CaptureError("Could not bring the Kindle app to the front.")

        self.pages_dir.mkdir(parents=True, exist_ok=True)
        probe = self.outdir / "_probe.png"
        scratch = self.outdir / "_scratch.png"
        resuming = bool(self.pages)
        detected = self.next_key == "auto"

        try:
            self._focus(window, probe, scratch)
            # The first page of a fresh run is captured without turning.
            if not resuming:
                self._capture_settled(window.id, probe, scratch)
                entry = self._accept(probe)
                self.save({"window_title": window.title})
                if progress:
                    progress(1, entry["file"])

            self._baseline_corner = corner_ink(self._last_page_path())
            if resuming and self.next_key != "auto":
                self._rejoin(window, probe, scratch, progress)

            if self.next_key == "auto":
                if len(self.pages) >= max_pages:
                    return self.pages
                self._detect_turn_key(window, probe, scratch, progress)
                self.save({"window_title": window.title})

            unchanged = 0
            while len(self.pages) < max_pages:
                send_key(self.next_key)
                time.sleep(self.delay)
                self._capture_settled(window.id, probe, scratch)

                last = self._last_page_path()
                changed = native.compare(probe, last)["changed_frac"]

                if self._covered_by_sheet(probe):
                    self._leave_sheet(progress)
                    break

                if changed < SAME_PAGE:
                    unchanged += 1
                    # A first turn that moves nothing is a stuck key, not an
                    # ending; saying "end of book" there sends people hunting
                    # for the wrong problem.
                    if (unchanged == 1 and len(self.pages) == 1 and not resuming
                            and not detected):
                        raise CaptureError(
                            f"The page did not change after pressing "
                            f"'{self.next_key}'.\n"
                            "  - Drop --next-key to let kindle2ai find the right "
                            "key automatically\n"
                            "  - Vertical (right-bound) Japanese books turn the "
                            "other way: --next-key left\n"
                            "  - Click once inside the Kindle page area "
                            "yourself, then retry\n"
                            "  - Check Accessibility permission: kindle2ai doctor"
                        )
                    if unchanged == 1 and (self._last_percent or 0) < END_PERCENT:
                        # Mid-book, focus has most often slipped (or the
                        # toolbar came up): click the reader and press again.
                        # Not near the end, where the click could land on
                        # Kindle's end-of-book controls.
                        self._focus(window, probe, scratch)
                        if not self._same(probe, last):  # the click itself moved on
                            unchanged = 0
                        continue
                    if unchanged == 2 and (self._last_percent or 0) < END_PERCENT \
                            and self._try_fallback_keys(window, probe, scratch, progress):
                        unchanged = 0
                        continue
                    if unchanged >= self.end_repeats:
                        percent = self._last_percent
                        if percent is not None and percent < END_PERCENT:
                            stuck = self.outdir / "stuck.png"
                            stuck.write_bytes(probe.read_bytes())
                            raise CaptureError(
                                f"The page stopped turning at {percent}% of the book, "
                                "which is not the end; stopped.\n"
                                f"  The screen at that moment is saved as {stuck}.\n"
                                "  Check for a dialog or popup in Kindle, close it, "
                                "and resume: the pages so far are kept.")
                        if progress:
                            progress(len(self.pages),
                                     "end of book (page stopped changing)")
                        break
                    continue

                unchanged = 0
                self._guard_against_looping(probe)
                entry = self._accept(probe)
                self.save({"window_title": window.title})
                if progress:
                    progress(entry["index"], entry["file"])
        finally:
            probe.unlink(missing_ok=True)
            scratch.unlink(missing_ok=True)
            (self.outdir / "_before.png").unlink(missing_ok=True)
            self.save({"window_title": window.title})

        return self.pages

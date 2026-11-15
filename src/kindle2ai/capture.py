"""Drive the Kindle for Mac app: focus it, screenshot the window, turn the page.

Nothing here touches the book files on disk. It reads the same pixels a person
reading the book would see, which is why it works regardless of book format.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import native

KINDLE_BUNDLE = "com.amazon.Lassen"

# macOS virtual key codes.
KEY_CODES = {
    "right": 124, "left": 123, "down": 125, "up": 126,
    "space": 49, "pagedown": 121, "pageup": 116,
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
# metadata, which is often absent or wrong.
TURN_KEYS = ["right", "left", "space", "pagedown"]


class CaptureError(RuntimeError):
    pass


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
            "(If the app is open, grant Screen Recording to your terminal.)"
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
            "  Enable your terminal app, then restart it."
        )
    if not perms.get("accessibility"):
        problems.append(
            "Accessibility not granted -> page turns would do nothing.\n"
            "  System Settings > Privacy & Security > Accessibility\n"
            "  Enable your terminal app, then restart it."
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

    def _detect_turn_key(self, window: KindleWindow, probe: Path,
                         scratch: Path, progress=None) -> str:
        """Find a key that actually turns the page, and keep the page it turned to."""
        last = self._last_page_path()
        for key in TURN_KEYS:
            send_key(key)
            time.sleep(self.delay)
            self._capture_settled(window.id, probe, scratch)
            changed = native.compare(probe, last)["changed_frac"]
            if changed >= TURN_CONFIRM:
                self.next_key = key
                if progress:
                    progress(len(self.pages),
                             f"page-turn key: '{key}' ({changed:.0%} of the page changed)")
                entry = self._accept(probe)
                if progress:
                    progress(entry["index"], entry["file"])
                return key
        raise CaptureError(
            "No key turned the page (tried: " + ", ".join(TURN_KEYS) + ").\n"
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
        focus_reader(window)

        self.pages_dir.mkdir(parents=True, exist_ok=True)
        probe = self.outdir / "_probe.png"
        scratch = self.outdir / "_scratch.png"
        resuming = bool(self.pages)

        try:
            # The first page of a fresh run is captured without turning.
            if not resuming:
                self._capture_settled(window.id, probe, scratch)
                entry = self._accept(probe)
                self.save({"window_title": window.title})
                if progress:
                    progress(1, entry["file"])

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

                if changed < SAME_PAGE:
                    unchanged += 1
                    # A first turn that moves nothing is a stuck key, not an
                    # ending; saying "end of book" there sends people hunting
                    # for the wrong problem.
                    if unchanged == 1 and len(self.pages) == 1 and not resuming:
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
                    if unchanged >= self.end_repeats:
                        if progress:
                            progress(len(self.pages),
                                     "end of book (page stopped changing)")
                        break
                    continue

                unchanged = 0
                entry = self._accept(probe)
                self.save({"window_title": window.title})
                if progress:
                    progress(entry["index"], entry["file"])
        finally:
            probe.unlink(missing_ok=True)
            scratch.unlink(missing_ok=True)
            self.save({"window_title": window.title})

        return self.pages

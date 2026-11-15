"""Read the local Kindle for Mac library index (titles only, read-only)."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

CONTAINER = Path.home() / "Library/Containers/com.amazon.Lassen/Data"
BOOK_DB = CONTAINER / "Library/Protected/BookData.sqlite"
EBOOKS_DIR = CONTAINER / "Library/eBooks"

# ZRAWBOOKSTATE observed values; 3 = local/downloaded, 0 = cloud only.
STATE_LOCAL = 3


@dataclass
class Book:
    asin: str
    title: str
    state: int
    max_location: int
    mimetype: str
    language: str
    is_dictionary: bool
    downloaded: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _snapshot(db: Path, tmp: Path) -> Path:
    """Copy db + WAL sidecars so we never touch the live file."""
    dest = tmp / db.name
    shutil.copy2(db, dest)
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            shutil.copy2(side, tmp / side.name)
    return dest


def list_books(include_dictionaries: bool = False) -> list[Book]:
    if not BOOK_DB.exists():
        raise FileNotFoundError(
            f"Kindle library database not found at {BOOK_DB}\n"
            "Open the Kindle app at least once, or pass --title manually."
        )
    with tempfile.TemporaryDirectory() as td:
        snap = _snapshot(BOOK_DB, Path(td))
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """
                SELECT ZBOOKID, ZDISPLAYTITLE, ZRAWBOOKSTATE, ZRAWMAXLOCATION,
                       ZMIMETYPE, ZLANGUAGE, ZRAWISDICTIONARY, ZPATH
                  FROM ZBOOK
                 ORDER BY ZRAWLASTACCESSTIME DESC, ZDISPLAYTITLE
                """
            ).fetchall()
        finally:
            con.close()

    books: list[Book] = []
    for asin, title, state, maxloc, mime, lang, is_dict, path in rows:
        dictionary = bool(is_dict)
        if dictionary and not include_dictionaries:
            continue
        books.append(Book(
            asin=_clean_asin(asin or ""),
            title=(title or "").strip(),
            state=state or 0,
            max_location=maxloc or 0,
            mimetype=mime or "",
            language=(lang or "").strip(),
            is_dictionary=dictionary,
            downloaded=bool(path) and (state or 0) == STATE_LOCAL,
        ))
    return books


def _clean_asin(raw: str) -> str:
    """'A:B077XSB8BS-0' -> 'B077XSB8BS'."""
    s = raw.split(":", 1)[-1]
    if "-" in s:
        s = s.rsplit("-", 1)[0]
    return s


def find(query: str, include_dictionaries: bool = False) -> list[Book]:
    q = query.strip().casefold()
    return [b for b in list_books(include_dictionaries)
            if q in b.title.casefold() or q == b.asin.casefold()]

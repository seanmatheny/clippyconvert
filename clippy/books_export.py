"""Read Apple Books annotations as a sync source (Books -> Kindle).

Books stores the highlighted text verbatim, so no CFI parsing is needed:
the text itself is the position key that gets re-located in the Kindle book.
"""

import datetime
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

HOME = os.path.expanduser("~")
LIB_DB = f"{HOME}/Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary/BKLibrary-1-091020131601.sqlite"
ANN_DB = f"{HOME}/Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation/AEAnnotation_v10312011_1727_local.sqlite"
CORE_DATA_EPOCH = 978307200

# Books color indices (ZANNOTATIONSTYLE) -> Kindle mchl_color
KINDLE_COLOR = {0: "yellow", 1: "orange", 2: "blue", 3: "yellow", 4: "pink", 5: "blue"}


@dataclass
class BooksHighlight:
    uuid: str
    assetid: str
    text: str
    note: Optional[str]
    style: int
    is_underline: bool
    created: Optional[datetime.datetime]
    chapter: Optional[str]  # ZFUTUREPROOFING5; NULL on kindle2books imports

    @property
    def native(self) -> bool:
        """True for highlights made in Books itself; kindle2books-imported
        rows leave the chapter-title field NULL."""
        return self.chapter is not None

    @property
    def color(self) -> str:
        return KINDLE_COLOR.get(self.style, "yellow")


def load_books_highlights(assetid: Optional[str] = None) -> list:
    con = sqlite3.connect(f"file:{ANN_DB}?mode=ro", uri=True)
    q = (
        "SELECT ZANNOTATIONUUID, ZANNOTATIONASSETID, ZANNOTATIONSELECTEDTEXT, "
        "ZANNOTATIONNOTE, ZANNOTATIONSTYLE, ZANNOTATIONISUNDERLINE, "
        "ZANNOTATIONCREATIONDATE, ZFUTUREPROOFING5 FROM ZAEANNOTATION "
        "WHERE ZANNOTATIONDELETED=0 AND ZANNOTATIONTYPE=2 "
        "AND ZANNOTATIONSELECTEDTEXT IS NOT NULL"
    )
    args = ()
    if assetid:
        q += " AND ZANNOTATIONASSETID=?"
        args = (assetid,)
    rows = con.execute(q, args).fetchall()
    con.close()
    out = []
    for u, aid, text, note, style, is_ul, created, chapter in rows:
        dt = None
        if created is not None:
            dt = datetime.datetime.fromtimestamp(created + CORE_DATA_EPOCH)
        out.append(
            BooksHighlight(
                uuid=u,
                assetid=aid,
                text=text,
                note=note or None,
                style=style or 0,
                is_underline=bool(is_ul),
                created=dt,
                chapter=chapter,
            )
        )
    return out


def load_books_library() -> list:
    """[(assetid, title, author)] for every asset with a title. No local-file
    filter — only the stored text is needed, not the EPUB."""
    con = sqlite3.connect(f"file:{LIB_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ZASSETID, ZTITLE, ZAUTHOR FROM ZBKLIBRARYASSET WHERE ZTITLE IS NOT NULL"
    ).fetchall()
    con.close()
    return rows

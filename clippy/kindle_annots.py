"""Read highlights straight from the Mac Kindle app's synced database.

The Kindle app keeps every highlight the account has made — including ones made
on a Kindle device, synced wirelessly through Amazon — in `server_view` of
ksdk_annotation_v1.db. Those rows store byte/pid *positions* but no text, so we
reconstruct each passage from the book's own file (MOBI or KFX) via the same
readers books2kindle uses, and hand back `Clipping` records shaped exactly like
the My Clippings.txt parser's — letting kindle2books import from the app with no
USB connection.

This is the mirror of books2kindle: there, Books text is located in the Kindle
file to get a position; here, a Kindle position is turned back into text so it
can be located in the Books EPUB.
"""

import datetime
import sqlite3

from clippy import ksdk
from clippy.kfx import KfxBook
from clippy.mobi import MobiBook, UnsupportedBook
from clippy.parse_clippings import Clipping
from clippy import kfx


def open_book(book):
    """Open a Kindle book as its format-appropriate reader. Both readers share
    the same interface (norm_text / byte_range_for_norm / extract_text /
    text_length), so callers stay format-agnostic. Raises UnsupportedBook or
    OSError on failure."""
    if book.is_kfx:
        return KfxBook.from_dir(book.path)
    return MobiBook.from_file(book.path)


def load_kindle_highlights(title_filter=None):
    """Reconstruct the app's synced highlights/underlines as Clipping records.

    Returns (clippings, skipped) where skipped is [(title, reason)] for books
    whose file is missing, DRM'd, an unsupported format, or whose position
    space doesn't line up with the app's (so extracted text can't be trusted).
    """
    db = ksdk.find_ksdk_db()
    books_by_id = {b.bookid: b for b in ksdk.load_kindle_books()}

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ids = [r[0] for r in con.execute(
        "SELECT DISTINCT dataset_id FROM server_view WHERE dataset IN (?,?)",
        (ksdk.DATASET_HIGHLIGHT, ksdk.DATASET_UNDERLINE))]
    con.close()

    targets = []
    for did in ids:
        b = books_by_id.get(did.split("-")[0])
        if not b:
            continue
        if title_filter and title_filter.lower() not in b.title.lower():
            continue
        targets.append((did, b))

    # Extract KFX content once for the whole batch (Calibre starts a single time).
    kfx.prewarm([b.path for _, b in targets if b.is_kfx])

    clippings, skipped = [], []
    for did, b in sorted(targets, key=lambda t: t[1].title):
        anns = [a for a in ksdk.load_existing_annotations(db, did)
                if a.source == "server_view"
                and a.dataset in (ksdk.DATASET_HIGHLIGHT, ksdk.DATASET_UNDERLINE)]
        if not anns:
            continue
        if not (b.is_mobi or b.is_kfx):
            skipped.append((b.title, f"unsupported format {b.mime}"))
            continue
        try:
            mb = open_book(b)
        except (UnsupportedBook, OSError) as e:
            skipped.append((b.title, f"{'KFX' if b.is_kfx else 'MOBI'} read failed: {e}"))
            continue
        if b.maxpos is not None and mb.text_length != b.maxpos:
            skipped.append((b.title,
                            f"position space mismatch ({mb.text_length} != app {b.maxpos})"))
            continue
        for a in anns:
            text = mb.extract_text(a.start, a.end).strip()
            if not text:
                continue
            kind = "Underline" if a.dataset == ksdk.DATASET_UNDERLINE else "Highlight"
            added = None
            ct = a.payload.get("created_time") or a.payload.get("last_modified")
            if isinstance(ct, (int, float)) and ct > 0:
                added = datetime.datetime.fromtimestamp(ct / 1000)
            clippings.append(Clipping(
                title=b.title, author=b.author or None, kind=kind, page=None,
                loc_start=a.start, loc_end=a.end, added=added, text=text))
    return clippings, skipped

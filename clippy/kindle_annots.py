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
from dataclasses import dataclass

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


@dataclass
class SkippedBook:
    """A book whose synced highlights could not be read back — wholly or in
    part. `book` is None when the app has annotations for an id its library no
    longer lists."""
    title: str
    reason: str
    count: int  # highlights affected
    book: object = None
    not_downloaded: bool = False


def load_kindle_highlights(title_filter=None):
    """Reconstruct the app's synced highlights/underlines as Clipping records.

    Returns (clippings, skipped) where skipped is a list of SkippedBook for
    books whose file is missing or not downloaded, DRM'd, an unsupported
    format, whose position space doesn't line up with the app's (so extracted
    text can't be trusted), or that yielded empty text for some highlights.

    Note this can only ever see books the app has downloaded: the app syncs
    annotations per local book, so a cloud-only book has no rows here at all.
    ksdk.undownloaded_books() covers that blind spot.
    """
    db = ksdk.find_ksdk_db()
    books_by_id = {b.bookid: b for b in ksdk.load_kindle_books()}

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    counts = con.execute(
        "SELECT dataset_id, COUNT(*) FROM server_view WHERE dataset IN (?,?) "
        "GROUP BY dataset_id",
        (ksdk.DATASET_HIGHLIGHT, ksdk.DATASET_UNDERLINE)).fetchall()
    con.close()

    targets, skipped = [], []
    for did, n in counts:
        b = books_by_id.get(did.split("-")[0])
        if not b:
            if not title_filter:
                skipped.append(SkippedBook(
                    f"<{did.split('-')[0]}>",
                    "the Kindle app no longer lists this book (removed, or never "
                    "downloaded on this Mac)", n, None, True))
            continue
        if title_filter and title_filter.lower() not in b.title.lower():
            continue
        targets.append((did, b))

    # Extract KFX content once for the whole batch (Calibre starts a single time).
    kfx.prewarm([b.path for _, b in targets if b.is_kfx])

    clippings = []
    for did, b in sorted(targets, key=lambda t: t[1].title):
        anns = [a for a in ksdk.load_existing_annotations(db, did)
                if a.source == "server_view"
                and a.dataset in (ksdk.DATASET_HIGHLIGHT, ksdk.DATASET_UNDERLINE)]
        if not anns:
            continue
        if not b.downloaded:
            skipped.append(SkippedBook(
                b.title, "not downloaded in the Kindle app", len(anns), b, True))
            continue
        if not (b.is_mobi or b.is_kfx):
            skipped.append(SkippedBook(
                b.title, f"unsupported format {b.mime}", len(anns), b))
            continue
        try:
            mb = open_book(b)
        except (UnsupportedBook, OSError) as e:
            skipped.append(SkippedBook(
                b.title, f"{'KFX' if b.is_kfx else 'MOBI'} read failed: {e}", len(anns), b))
            continue
        if b.maxpos is not None and mb.text_length != b.maxpos:
            skipped.append(SkippedBook(
                b.title, f"position space mismatch ({mb.text_length} != app {b.maxpos})",
                len(anns), b))
            continue
        empty = 0
        for a in anns:
            text = mb.extract_text(a.start, a.end).strip()
            if not text:
                empty += 1
                continue
            kind = "Underline" if a.dataset == ksdk.DATASET_UNDERLINE else "Highlight"
            added = None
            ct = a.payload.get("created_time") or a.payload.get("last_modified")
            if isinstance(ct, (int, float)) and ct > 0:
                added = datetime.datetime.fromtimestamp(ct / 1000)
            clippings.append(Clipping(
                title=b.title, author=b.author or None, kind=kind, page=None,
                loc_start=a.start, loc_end=a.end, added=added, text=text))
        if empty:
            skipped.append(SkippedBook(
                b.title, f"{empty} of {len(anns)} highlights extracted no text "
                         "(the rest were read normally)", empty, b))
    return clippings, skipped

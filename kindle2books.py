#!/usr/bin/env python3
"""Convert Kindle highlights into Apple Books annotations.

By default the highlights come straight from the Mac Kindle app's synced
database — no cable needed, since device highlights sync there wirelessly
through Amazon. The app stores positions without text, so each passage is
reconstructed from the book's own MOBI/KFX file (the mirror of books2kindle).
A "My Clippings.txt" file can be used instead, including one pulled fresh off a
USB-connected Kindle.

Usage:
  python3 kindle2books.py sync                        # dry run from the Kindle app DB
  python3 kindle2books.py sync --apply                # insert into Apple Books
  python3 kindle2books.py sync --books shogun         # limit to matching titles
  python3 kindle2books.py sync --from-clippings "My Clippings.txt"  # use a clippings file
  python3 kindle2books.py sync "My Clippings.txt"     # positional path, same as above
  python3 kindle2books.py sync --from-kindle --apply  # pull clippings from USB Kindle, then import
  python3 kindle2books.py sync --download             # fetch cloud-only Kindle books first

The Kindle app only holds annotations for books it has downloaded, so a
cloud-only book's highlights are invisible here. Those books are listed at the
start of every run, and --download asks the app to fetch them first.

Notes and Bookmarks are ignored. When reading a clippings file, Kindle's
progressive re-highlight entries are deduplicated (the last adjusted version
wins); the app DB is already the canonical synced state, so no dedupe is
needed there. Passages are located by text search in the sideloaded EPUB and
stored as EPUB CFI ranges — the same representation Apple Books itself uses —
so Kindle "locations" never need to map to page numbers.

Apple Books must be closed during --apply; the script quits it and backs up
the annotation database first.
"""

import argparse
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clippy.parse_clippings import parse_clippings, dedupe_highlights
from clippy.epub_cfi import Epub, normalize_with_map
from clippy.match import match_book
from clippy.kindle_annots import load_kindle_highlights
from clippy.term import maybe_green
from clippy import ksdk
from clippy.access import require_container_access, BOOKS_CONTAINER, KINDLE_CONTAINER

HOME = os.path.expanduser("~")
LIB_DB = f"{HOME}/Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary/BKLibrary-1-091020131601.sqlite"
ANN_DB = f"{HOME}/Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation/AEAnnotation_v10312011_1727_local.sqlite"
CORE_DATA_EPOCH = 978307200

STYLE_HIGHLIGHT = 3  # yellow
STYLE_UNDERLINE = 0

CLIPPY = r"""
   __
  /  \      ___________________________________________
  |  |     / It looks like you're importing Kindle     \
  @  @    |  highlights into Apple Books.               |
  |  |    |  Would you like some help with that?        |
  || |/    \___________________________________________/
  || ||
  |\_/|
  \___/
"""


def load_library():
    """(searchable books, titles of cloud-only ones).

    Apple Books keeps a path for purchases it has not downloaded, with no file
    behind it. Those can't be searched for passages, so they are reported
    rather than quietly dropped — otherwise their Kindle highlights show up
    under "not found in Apple Books library", which is misleading."""
    con = sqlite3.connect(f"file:{LIB_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ZASSETID, ZTITLE, ZAUTHOR, ZPATH FROM ZBKLIBRARYASSET "
        "WHERE ZTITLE IS NOT NULL AND ZPATH IS NOT NULL"
    ).fetchall()
    con.close()
    usable = [r for r in rows if os.path.exists(r[3]) and not r[3].endswith(".pdf")]
    cloud_only = sorted(r[1] for r in rows if not os.path.exists(r[3]))
    return usable, cloud_only


def print_titles(titles, cap=20):
    """List titles under a report line, trimmed so a big cloud library can't
    bury the rest of the run."""
    for t in titles[:cap]:
        print(f"  {t[:70]}")
    if len(titles) > cap:
        print(f"  ... and {len(titles) - cap} more")


def fetch_cloud_books(pending):
    """Ask the Kindle app to download the books it is holding only in the
    cloud, and wait for the files to land."""
    from clippy import kindle_download

    requested = [b for b in pending if kindle_download.request_download(b)]
    manual = [b for b in pending if b not in requested]
    for b in requested:
        print(f"  requested download: {b.title[:60]}")
    for b in manual:
        print(f"  cannot request (personal document — download it by hand): {b.title[:60]}")
    if not requested:
        return
    print(f"waiting for the Kindle app to fetch {len(requested)} book(s)...", flush=True)

    def progress(done, total, left):
        print(f"\r  {done}/{total} downloaded, {left}s left\x1b[K", end="", flush=True)

    arrived, still = kindle_download.wait_for_downloads(
        [b.bookid for b in requested], progress=progress if sys.stdout.isatty() else None)
    if sys.stdout.isatty():
        print("\r\x1b[K", end="")
    print(f"downloaded {len(arrived)} book(s)"
          + (f"; {len(still)} did not arrive in time" if still else ""))
    for b in still:
        print(f"  still missing: {b.title[:60]}")
    if arrived:
        print("note: the app syncs a book's highlights shortly after downloading it — "
              "if a freshly fetched book reports no highlights, re-run this command.")


def existing_annotation_keys(con, assetid):
    rows = con.execute(
        "SELECT ZANNOTATIONLOCATION, ZANNOTATIONSELECTEDTEXT FROM ZAEANNOTATION "
        "WHERE ZANNOTATIONASSETID=? AND ZANNOTATIONDELETED=0",
        (assetid,),
    ).fetchall()
    cfis = {r[0] for r in rows if r[0]}
    texts = {normalize_with_map(r[1])[0] for r in rows if r[1]}
    return cfis, texts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sync", help="import Kindle highlights into Apple Books")
    sp.add_argument("clippings", nargs="?",
                    help="path to a My Clippings.txt file to import from (default source "
                         "is the Kindle app database — no path needed)")
    sp.add_argument("--from-clippings", metavar="PATH",
                    help="import from this My Clippings.txt file instead of the Kindle app DB")
    sp.add_argument("--from-kindle", action="store_true",
                    help="fetch My Clippings.txt from a USB-connected Kindle (mass storage or MTP) and import from it")
    sp.add_argument("--apply", action="store_true", help="write annotations (default: dry run)")
    sp.add_argument("--download", action="store_true",
                    help="ask the Kindle app to download any cloud-only books first, so "
                         "their highlights become readable (app DB source only)")
    sp.add_argument("--books", help="only process books whose title contains this substring (case-insensitive)")
    sp.add_argument("--min-score", type=float, default=0.85, help="min fuzzy text match score to import")
    sp.add_argument("--accept-weak", type=float, metavar="SCORE",
                    help="also import weak matches scoring >= SCORE (between 0.55 and --min-score); "
                         "they are labeled WEAK-ACCEPTED in the report")
    args = ap.parse_args()

    print(CLIPPY)

    clip_path = args.from_clippings or args.clippings
    if args.from_kindle or clip_path:
        require_container_access(BOOKS_CONTAINER)
    else:
        require_container_access(KINDLE_CONTAINER, BOOKS_CONTAINER)
    if args.from_kindle:
        from clippy.kindle_fetch import fetch_clippings, KindleFetchError

        clip_path = clip_path or "My Clippings.txt"
        try:
            via = fetch_clippings(clip_path)
        except KindleFetchError as e:
            sys.exit(f"error: {e}")
        size = os.path.getsize(clip_path)
        print(f"fetched My Clippings.txt from Kindle via {via} ({size:,} bytes) -> {clip_path}")

    if args.from_kindle or clip_path:
        if not os.path.exists(clip_path):
            sys.exit(f"error: {clip_path} not found (use --from-kindle to pull it from a connected Kindle)")
        if args.download:
            print("note: --download only applies to the Kindle app DB source; ignored here")
        clips = dedupe_highlights(parse_clippings(clip_path))
        print(f"source: {clip_path}\n{len(clips)} highlights/underlines after dedupe")
    else:
        # default: the Kindle app's synced database (no USB, no clippings file).
        # The app only syncs annotations for books it holds locally, so anything
        # still in the cloud is invisible until it is downloaded.
        pending = ksdk.undownloaded_books(title_filter=args.books)
        if pending and args.download:
            print(f"{len(pending)} Kindle book(s) not downloaded — asking the app to fetch them:")
            fetch_cloud_books(pending)
            pending = ksdk.undownloaded_books(title_filter=args.books)
        clips, skipped = load_kindle_highlights(title_filter=args.books)
        print(f"source: Kindle app database (server_view)\n"
              f"{len(clips)} highlights/underlines across "
              f"{len({c.title for c in clips})} books")
        if pending:
            print(f"\n{len(pending)} Kindle book(s) NOT downloaded — any highlights in them "
                  "are invisible to this run:")
            print_titles([b.title for b in pending])
            if not args.download:
                print("  (pass --download to have the Kindle app fetch them)")
        if skipped:
            print("\nKindle books whose highlights could not be read:")
            for sk in skipped:
                print(f"  {sk.title[:55]}: {sk.reason}")

    library, books_cloud_only = load_library()
    print(f"\n{len(library)} books with local EPUBs in Apple Books library")
    if books_cloud_only:
        print(f"{len(books_cloud_only)} Apple Books title(s) not downloaded locally — "
              "they cannot be searched, so highlights for them will show as unmatched:")
        print_titles(books_cloud_only)

    # group clippings by kindle title
    by_book = {}
    for c in clips:
        by_book.setdefault((c.title, c.author), []).append(c)

    ann_con = sqlite3.connect(f"file:{ANN_DB}?mode=ro", uri=True)
    plan = []  # (assetid, book_title, path, [(clip, loc)], skipped_dupes)
    unmatched_books = []
    for (ktitle, kauthor), items in sorted(by_book.items()):
        if args.books and args.books.lower() not in ktitle.lower():
            continue
        m = match_book(ktitle, kauthor, library)
        if not m:
            unmatched_books.append((ktitle, len(items)))
            continue
        assetid, btitle, _author, path = m
        cfis, texts = existing_annotation_keys(ann_con, assetid)
        # The header is printed after the search so it can be coloured by the
        # outcome; the live progress line carries the title in the meantime.
        head = f"== {ktitle!r} -> {btitle!r}"
        try:
            epub = Epub(path)
        except Exception as e:
            print(f"\n{head}\n   EPUB parse failed: {e}")
            continue
        live = sys.stdout.isatty()
        found, dupes, misses, weak = [], 0, [], []
        for i, c in enumerate(items, 1):
            if live:
                print(f"\r   {btitle[:28]}: searching {i}/{len(items)}: {c.text[:40]!r}\x1b[K",
                      end="", flush=True)
            norm_text = normalize_with_map(c.text)[0]
            if norm_text in texts:
                dupes += 1
                continue
            loc = epub.find_text(c.text)
            if loc is None:
                misses.append(c)
            elif loc.score < args.min_score:
                weak.append((c, loc))
            elif loc.cfi in cfis:
                dupes += 1
            else:
                found.append((c, loc))
        if live:
            print("\r\x1b[K", end="")
        accepted = []
        if args.accept_weak is not None:
            still_weak = []
            for c, loc in weak:
                if loc.score >= args.accept_weak and loc.cfi not in cfis:
                    accepted.append((c, loc))
                    found.append((c, loc))
                else:
                    still_weak.append((c, loc))
            weak = still_weak
        extra = f" (incl. {len(accepted)} weak-accepted)" if accepted else ""
        new_matches = bool(found)
        print("\n" + maybe_green(head, new_matches))
        print(maybe_green(
            f"   matched {len(found)}{extra}, weak {len(weak)}, "
            f"missed {len(misses)}, already-present {dupes}", new_matches))
        for c in misses:
            print(f"     MISS: {c.text[:70]!r}")
        for c, loc in accepted:
            print(f"     WEAK-ACCEPTED ({loc.score:.2f}): {c.text[:55]!r} -> {loc.matched_text[:55]!r}")
        for c, loc in weak:
            print(f"     WEAK ({loc.score:.2f}): {c.text[:55]!r} -> {loc.matched_text[:55]!r}")
        if found:
            plan.append((assetid, btitle, found))
    ann_con.close()

    if unmatched_books:
        print("\nBooks not found in Apple Books library (skipped):")
        for t, n in unmatched_books:
            print(f"  {n:4d}  {t}")

    total = sum(len(f) for _, _, f in plan)
    print(f"\nTOTAL to import: {total} highlights across {len(plan)} books")
    if not args.apply:
        print("(dry run — pass --apply to write)")
        return

    # ---- apply ----------------------------------------------------------
    subprocess.run(["osascript", "-e", 'tell application "Books" to quit'], capture_output=True)
    time.sleep(2)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups", stamp)
    os.makedirs(bdir, exist_ok=True)
    for suffix in ("", "-shm", "-wal"):
        src = ANN_DB + suffix
        if os.path.exists(src):
            shutil.copy2(src, bdir)
    print(f"backed up annotation DB to {bdir}")

    con = sqlite3.connect(ANN_DB)
    cur = con.cursor()
    pk = cur.execute("SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME='AEAnnotation'").fetchone()[0]
    n = 0
    for assetid, btitle, found in plan:
        for c, loc in found:
            pk += 1
            ts = (
                time.mktime(c.added.timetuple()) - CORE_DATA_EPOCH
                if c.added
                else time.time() - CORE_DATA_EPOCH
            )
            # the date columns have NUMERIC affinity: a whole-second float is
            # stored as an SQLite INTEGER, which crashes Books (it expects a
            # Double). Keep a fractional part so the value stays REAL.
            ts += 0.123456
            spine_idx = int(loc.cfi.split("!")[0].split("/")[2].split("[")[0]) // 2 - 1
            is_ul = c.kind == "Underline"
            cur.execute(
                """INSERT INTO ZAEANNOTATION
                (Z_PK, Z_ENT, Z_OPT, ZANNOTATIONDELETED, ZANNOTATIONISUNDERLINE, ZANNOTATIONSTYLE,
                 ZANNOTATIONTYPE, ZPLABSOLUTEPHYSICALLOCATION, ZPLLOCATIONRANGEEND, ZPLLOCATIONRANGESTART,
                 ZANNOTATIONCREATIONDATE, ZANNOTATIONMODIFICATIONDATE, ZANNOTATIONASSETID,
                 ZANNOTATIONCREATORIDENTIFIER, ZANNOTATIONLOCATION, ZANNOTATIONREPRESENTATIVETEXT,
                 ZANNOTATIONSELECTEDTEXT, ZANNOTATIONUUID, ZFUTUREPROOFING6, ZFUTUREPROOFING11)
                VALUES (?,1,1,0,?,?,2,0,0,?,?,?,?,'com~apple~iBooks',?,?,?,?,?,?)""",
                (
                    pk,
                    1 if is_ul else 0,
                    STYLE_UNDERLINE if is_ul else STYLE_HIGHLIGHT,
                    spine_idx,
                    ts,
                    ts,
                    assetid,
                    loc.cfi,
                    loc.matched_text,
                    loc.matched_text,
                    str(uuid.uuid4()).upper(),
                    f"{ts:.6f}",
                    f"{ts:.6f}",
                ),
            )
            n += 1
        print(f"  wrote {len(found):4d} -> {btitle}")
    cur.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_NAME='AEAnnotation'", (pk,))
    con.commit()
    con.close()
    print(f"inserted {n} annotations")


if __name__ == "__main__":
    main()

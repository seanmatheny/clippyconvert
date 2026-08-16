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
    con = sqlite3.connect(f"file:{LIB_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ZASSETID, ZTITLE, ZAUTHOR, ZPATH FROM ZBKLIBRARYASSET "
        "WHERE ZTITLE IS NOT NULL AND ZPATH IS NOT NULL"
    ).fetchall()
    con.close()
    return [r for r in rows if os.path.exists(r[3]) and not r[3].endswith(".pdf")]


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
    sp.add_argument("--books", help="only process books whose title contains this substring (case-insensitive)")
    sp.add_argument("--min-score", type=float, default=0.85, help="min fuzzy text match score to import")
    sp.add_argument("--accept-weak", type=float, metavar="SCORE",
                    help="also import weak matches scoring >= SCORE (between 0.55 and --min-score); "
                         "they are labeled WEAK-ACCEPTED in the report")
    args = ap.parse_args()

    print(CLIPPY)

    clip_path = args.from_clippings or args.clippings
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
        clips = dedupe_highlights(parse_clippings(clip_path))
        print(f"source: {clip_path}\n{len(clips)} highlights/underlines after dedupe")
    else:
        # default: the Kindle app's synced database (no USB, no clippings file)
        clips, skipped = load_kindle_highlights(title_filter=args.books)
        print(f"source: Kindle app database (server_view)\n"
              f"{len(clips)} highlights/underlines across "
              f"{len({c.title for c in clips})} books")
        if skipped:
            print("Kindle books whose highlights could not be read (skipped):")
            for t, reason in skipped:
                print(f"  {t[:55]}: {reason}")

    library = load_library()
    print(f"{len(library)} books with local EPUBs in Apple Books library")

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
        print(f"\n== {ktitle!r} -> {btitle!r}", flush=True)
        try:
            epub = Epub(path)
        except Exception as e:
            print(f"   EPUB parse failed: {e}")
            continue
        live = sys.stdout.isatty()
        found, dupes, misses, weak = [], 0, [], []
        for i, c in enumerate(items, 1):
            if live:
                print(f"\r   searching {i}/{len(items)}: {c.text[:50]!r}\x1b[K", end="", flush=True)
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
        print(f"   matched {len(found)}{extra}, weak {len(weak)}, missed {len(misses)}, already-present {dupes}")
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

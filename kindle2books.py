#!/usr/bin/env python3
"""Convert Kindle "My Clippings.txt" highlights into Apple Books annotations.

Usage:
  python3 kindle2books.py "My Clippings.txt"                 # dry run: report matches
  python3 kindle2books.py "My Clippings.txt" --apply         # insert into Apple Books
  python3 kindle2books.py "My Clippings.txt" --books shogun  # limit to matching titles

Notes and Bookmarks in the clippings file are ignored. Kindle's progressive
re-highlight entries are deduplicated (the last version of an adjusted
highlight wins). Passages are located by text search in the sideloaded EPUB
and stored as EPUB CFI ranges — the same representation Apple Books itself
uses — so Kindle "locations" never need to map to page numbers.

Apple Books must be closed during --apply; the script quits it and backs up
the annotation database first.
"""

import argparse
import datetime
import difflib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clippy.parse_clippings import parse_clippings, dedupe_highlights
from clippy.epub_cfi import Epub, normalize_with_map

HOME = os.path.expanduser("~")
LIB_DB = f"{HOME}/Library/Containers/com.apple.iBooksX/Data/Documents/BKLibrary/BKLibrary-1-091020131601.sqlite"
ANN_DB = f"{HOME}/Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation/AEAnnotation_v10312011_1727_local.sqlite"
CORE_DATA_EPOCH = 978307200

STYLE_HIGHLIGHT = 3  # yellow
STYLE_UNDERLINE = 0


def norm_title(s: str) -> str:
    s = s.replace("_", " ")
    s = re.sub(r"--.*$", "", s)  # strip "-- Author -- Year -- ... Anna's Archive" tails
    s = re.sub(r"[^\w\s]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def title_tokens(s: str) -> set:
    stop = {"the", "a", "an", "of", "and", "in", "to", "for", "on", "s"}
    return {t for t in norm_title(s).split() if t not in stop}


def load_library():
    con = sqlite3.connect(f"file:{LIB_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ZASSETID, ZTITLE, ZAUTHOR, ZPATH FROM ZBKLIBRARYASSET "
        "WHERE ZTITLE IS NOT NULL AND ZPATH IS NOT NULL"
    ).fetchall()
    con.close()
    return [r for r in rows if os.path.exists(r[3]) and not r[3].endswith(".pdf")]


def match_book(kindle_title, kindle_author, library):
    """Return (assetid, title, path) or None."""
    kt = title_tokens(kindle_title)
    if not kt:
        return None
    best, best_score = None, 0.0
    for assetid, title, author, path in library:
        lt = title_tokens(title)
        if not lt:
            continue
        overlap = len(kt & lt) / min(len(kt), len(lt))
        ratio = difflib.SequenceMatcher(None, norm_title(kindle_title), norm_title(title)).ratio()
        score = max(overlap, ratio)
        # author corroboration
        if kindle_author and author:
            ka, la = title_tokens(kindle_author), title_tokens(author)
            if ka and la and not (ka & la):
                score -= 0.25
        if score > best_score:
            best, best_score = (assetid, title, path), score
    if best and best_score >= 0.75:
        return best
    return None


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
    ap = argparse.ArgumentParser()
    ap.add_argument("clippings")
    ap.add_argument("--apply", action="store_true", help="write annotations (default: dry run)")
    ap.add_argument("--books", help="only process books whose title contains this substring (case-insensitive)")
    ap.add_argument("--min-score", type=float, default=0.85, help="min fuzzy text match score to import")
    args = ap.parse_args()

    clips = dedupe_highlights(parse_clippings(args.clippings))
    print(f"{len(clips)} highlights/underlines after dedupe")

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
        assetid, btitle, path = m
        cfis, texts = existing_annotation_keys(ann_con, assetid)
        print(f"\n== {ktitle!r} -> {btitle!r}")
        try:
            epub = Epub(path)
        except Exception as e:
            print(f"   EPUB parse failed: {e}")
            continue
        found, dupes, misses, weak = [], 0, [], []
        for c in items:
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
        print(f"   matched {len(found)}, weak {len(weak)}, missed {len(misses)}, already-present {dupes}")
        for c in misses:
            print(f"     MISS: {c.text[:70]!r}")
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

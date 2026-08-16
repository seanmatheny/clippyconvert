#!/usr/bin/env python3
"""Sync Apple Books highlights into the Mac Kindle app — the reverse of
kindle2books.py.

Usage:
  python3 books2kindle.py capture              # one-time: learn the app's edit format
  python3 books2kindle.py validate             # check position math vs existing highlights
  python3 books2kindle.py sync                 # dry run: report what would sync
  python3 books2kindle.py sync --apply         # write into the Kindle app's sync queue
  python3 books2kindle.py sync --books stella  # limit to matching titles

Only highlights made natively in Apple Books are synced (rows imported by
kindle2books.py are excluded, so nothing echoes back). Passages are located
by text search in the decompressed MOBI stream and stored as Kindle byte
positions in the app's local_edit upload queue; the app then syncs them to
Amazon like any highlight made in the app, so they appear on all devices.

The Kindle app must be closed during --apply; the script quits it and backs
up the annotation database first. `capture` must be run once (offline, with
one manual test highlight) before --apply is allowed.
"""

import argparse
import datetime
import difflib
import json
import os
import shutil
import socket
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clippy import ksdk
from clippy import kfx
from clippy.books_export import load_books_highlights, load_books_library
from clippy.epub_cfi import normalize_with_map
from clippy.kindle_annots import open_book
from clippy.match import match_book
from clippy.mobi import UnsupportedBook
from clippy.parse_clippings import parse_clippings, dedupe_highlights
from clippy.textsearch import find_span, strip_footnote_markers

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

CLIPPY = r"""
   __
  /  \      ___________________________________________
  |  |     / It looks like you're exporting Apple      \
  @  @    |  Books highlights to Kindle.                |
  |  |    |  Would you like some help with that?        |
  || |/    \___________________________________________/
  || ||
  |\_/|
  \___/
"""


def norm_key(text: str) -> str:
    return normalize_with_map(strip_footnote_markers(text))[0]


def texts_similar(a: str, b: str, threshold: float) -> bool:
    """Fuzzy sameness of two normalized texts: containment either way, or a
    difflib ratio when lengths are comparable."""
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    if abs(len(a) - len(b)) > max(len(a), len(b)) * (1 - threshold):
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= threshold


# ---- capture -----------------------------------------------------------

def check_offline():
    try:
        socket.create_connection(("amazon.com", 443), timeout=3).close()
    except OSError:
        return
    sys.exit("error: you appear to be ONLINE. Turn off Wi-Fi first (the capture only "
             "works while the Kindle app cannot reach Amazon), then re-run.")


def diff_snapshots(before: dict, after: dict) -> dict:
    """{table: {"columns": [...], "added": [...], "removed": [...]}} for
    tables that changed."""
    out = {}
    for t, b in before.items():
        a = after[t]
        b_set, a_set = set(b["rows"]), set(a["rows"])
        added = [r for r in a["rows"] if r not in b_set]
        removed = [r for r in b["rows"] if r not in a_set]
        if added or removed:
            out[t] = {"columns": a["columns"], "added": added, "removed": removed}
    return out


def local_edit_dicts(diff: dict) -> list:
    entry = diff.get("local_edit")
    if not entry:
        return []
    return [dict(zip(entry["columns"], row)) for row in entry["added"]]


def cmd_capture(args):
    db = ksdk.find_ksdk_db()
    print("This records how the Kindle app itself writes annotation edits, so sync\n"
          "--apply can create records the app will upload as its own.\n")
    print("Step 0: quit the Kindle app and TURN OFF Wi-Fi now.")
    print("        (menu bar Wi-Fi icon, or: networksetup -setairportpower en0 off)")
    input("Press Enter when offline... ")
    check_offline()

    bdir = ksdk.backup_db(db, REPO_DIR, label="capture")
    baseline_dir = os.path.join(bdir, "baseline")
    os.makedirs(baseline_dir, exist_ok=True)
    for f in os.listdir(bdir):
        p = os.path.join(bdir, f)
        if os.path.isfile(p):
            shutil.move(p, baseline_dir)
    before = ksdk.snapshot_tables(db)
    print(f"baseline snapshot saved to {baseline_dir}")

    # suggest a MOBI book to annotate
    mobi = [b for b in ksdk.load_kindle_books() if b.is_mobi]
    if mobi:
        print(f"\nStep 1: launch Kindle (still offline), open e.g. {mobi[0].title!r},")
    else:
        print("\nStep 1: launch Kindle (still offline), open any downloaded book,")
    print("        create ONE highlight, then attach a note to that highlight,")
    print("        then quit the Kindle app.")
    input("Press Enter when done... ")
    check_offline()
    after_create = ksdk.snapshot_tables(db)
    shutil.copy2(db, os.path.join(bdir, "after-create.db"))
    create_diff = diff_snapshots(before, after_create)

    edits = local_edit_dicts(create_diff)
    if not edits:
        sv = create_diff.get("server_view")
        if sv and sv["added"]:
            sys.exit("error: new rows landed in server_view but not local_edit — the app "
                     "was ONLINE and synced immediately. Delete the test highlight, go "
                     "offline, and re-run capture.")
        sys.exit("error: no new local_edit rows found. Did you create a highlight and "
                 "quit the app? Re-run capture.")

    highlight = next((e for e in edits if e["dataset"] == ksdk.DATASET_HIGHLIGHT), None)
    note = next((e for e in edits if e["dataset"] == ksdk.DATASET_NOTE), None)
    if not highlight:
        sys.exit(f"error: no highlight row in local_edit (got datasets "
                 f"{sorted(e['dataset'] for e in edits)}). Re-run capture.")
    capture = {
        "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "create": {k: highlight[k] for k in
                   ("action", "dirty_flag", "sync_behavior", "retry_count",
                    "expiration_timestamp")},
        "create_rows": {t: d for t, d in create_diff.items()},
        "mirror_tables": [t for t in ("server_view", "staging_server_view",
                                      "nonsyncable_annotations")
                          if create_diff.get(t, {}).get("added")],
    }
    hp = json.loads(highlight["serialized_payload"])
    capture["highlight_payload_example"] = hp
    if note:
        np_ = json.loads(note["serialized_payload"])
        capture["note_payload_example"] = np_
        capture["note_offset"] = (np_["start_position"]["shortPosition"]
                                  - hp["end_position"]["shortPosition"])
    print(f"\ncaptured create: action={capture['create']['action']} "
          f"dirty_flag={capture['create']['dirty_flag']} "
          f"sync_behavior={capture['create']['sync_behavior']} "
          f"mirrors={capture['mirror_tables'] or 'none'}"
          + (f" note_offset={capture['note_offset']}" if note else " (no note captured)"))

    print("\nStep 2: relaunch Kindle (STILL offline), delete the test highlight and")
    print("        note again, then quit the app.")
    input("Press Enter when done... ")
    check_offline()
    after_delete = ksdk.snapshot_tables(db)
    shutil.copy2(db, os.path.join(bdir, "after-delete.db"))
    delete_diff = diff_snapshots(after_create, after_delete)
    del_edits = local_edit_dicts(delete_diff)
    changed = [e for e in del_edits if e["action"] != capture["create"]["action"]]
    if changed:
        capture["delete"] = {k: changed[0][k] for k in
                             ("action", "dirty_flag", "sync_behavior", "retry_count",
                              "expiration_timestamp")}
    capture["delete_rows"] = {t: d for t, d in delete_diff.items()}

    with open(ksdk.capture_path(), "w") as f:
        json.dump(capture, f, indent=2, default=str)
    print(f"\nwrote {ksdk.capture_path()}")
    print("Step 3: turn Wi-Fi back on. The app will sync the (net-zero) test edit.")


# ---- shared: books + annotations ---------------------------------------

def kindle_books_by_id():
    return {b.bookid: b for b in ksdk.load_kindle_books()}


def annotated_books(db):
    """[(KindleBook, [KindleAnnotation])] for MOBI/KFX books with synced
    highlights — the ground truth for `validate`."""
    books = kindle_books_by_id()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ids = [r[0] for r in con.execute(
        "SELECT DISTINCT dataset_id FROM server_view WHERE dataset IN (?,?)",
        (ksdk.DATASET_HIGHLIGHT, ksdk.DATASET_UNDERLINE))]
    con.close()
    out = []
    for did in ids:
        book = books.get(did.split("-")[0])
        if book and (book.is_mobi or book.is_kfx):
            anns = [a for a in ksdk.load_existing_annotations(db, did)
                    if a.source == "server_view"
                    and a.dataset in (ksdk.DATASET_HIGHLIGHT, ksdk.DATASET_UNDERLINE)]
            if anns:
                out.append((book, anns))
    return out


def load_clippings_texts():
    """{normalized kindle title: [normalized clipping texts]} from the local
    My Clippings.txt, for echo filtering and validation corroboration."""
    path = os.path.join(REPO_DIR, "My Clippings.txt")
    if not os.path.exists(path):
        return {}
    by_title = {}
    for c in dedupe_highlights(parse_clippings(path)):
        by_title.setdefault(c.title, []).append(norm_key(c.text))
    return by_title


# ---- validate ----------------------------------------------------------

def cmd_validate(args):
    db = ksdk.find_ksdk_db()
    targets = annotated_books(db)
    clippings = load_clippings_texts()
    kfx.prewarm([b.path for b, _ in targets if b.is_kfx])
    n_mobi = sum(1 for b, _ in targets if b.is_mobi)
    n_kfx = sum(1 for b, _ in targets if b.is_kfx)
    print(f"{sum(len(a) for _, a in targets)} ground-truth highlights across "
          f"{len(targets)} books ({n_mobi} MOBI, {n_kfx} KFX)\n")
    t_exact = t_close = t_miss = 0
    for book, anns in sorted(targets, key=lambda t: t[0].title):
        try:
            mb = open_book(book)
        except (UnsupportedBook, OSError) as e:
            print(f"== {book.title[:60]!r}: PARSE FAIL {e}")
            continue
        length_ok = "" if mb.text_length == book.maxpos else \
            f"  [WARN textLength {mb.text_length} != app maxpos {book.maxpos}]"
        clip_texts = []
        m = match_book(book.title, "", [(t, t, "") for t in clippings])
        if m:
            clip_texts = clippings[m[0]]
        exact = close = miss = corroborated = 0
        for a in anns:
            extracted = mb.extract_text(a.start, a.end)
            needle = norm_key(extracted)
            if not needle:
                miss += 1
                print(f"   MISS (empty extraction) [{a.start},{a.end}]")
                continue
            hit = find_span(mb.norm_text, needle)
            if not hit:
                miss += 1
                print(f"   MISS (round-trip not found) [{a.start},{a.end}] {extracted[:60]!r}")
                continue
            start, ln, _score = hit
            got = mb.byte_range_for_norm(start, ln)
            if abs(got[0] - a.start) <= 2 and abs(got[1] - a.end) <= 2:
                exact += 1
            else:
                close += 1
                print(f"   DIFF want [{a.start},{a.end}] got [{got[0]},{got[1]}] "
                      f"{extracted[:60]!r}")
            if any(texts_similar(needle, ct, 0.85) for ct in clip_texts):
                corroborated += 1
        corr = f", {corroborated} clippings-corroborated" if clip_texts else ""
        print(f"== {book.title[:60]!r}: exact={exact} close={close} miss={miss}{corr}{length_ok}")
        t_exact += exact
        t_close += close
        t_miss += miss
    total = t_exact + t_close + t_miss
    if total:
        pct = 100.0 * t_exact / total
        print(f"\nTOTAL exact={t_exact} close={t_close} miss={t_miss}  ({pct:.1f}% exact)")
        print("OK to sync --apply" if pct >= 95 else
              "BELOW the 95% gate — do not --apply until this is fixed")
    else:
        print("no ground-truth highlights found")


# ---- sync --------------------------------------------------------------

def cmd_sync(args):
    print(CLIPPY)
    db = ksdk.find_ksdk_db()

    highlights = [h for h in load_books_highlights() if h.native]
    print(f"{len(highlights)} native Apple Books highlights "
          f"(kindle2books imports excluded)")
    by_asset = {}
    for h in highlights:
        by_asset.setdefault(h.assetid, []).append(h)
    titles = {aid: (t, a or "") for aid, t, a in load_books_library()}

    kindle_books = ksdk.load_kindle_books()
    kindle_lib = [(b.bookid, b.title, b.author) for b in kindle_books]
    by_id = {b.bookid: b for b in kindle_books}
    print(f"{len(kindle_books)} books in the Kindle app library")
    clippings = load_clippings_texts()

    # Extract KFX content up front so Calibre starts once, not once per book.
    kfx_dirs = []
    for aid, items in by_asset.items():
        btitle, bauthor = titles.get(aid, (aid, ""))
        if args.books and args.books.lower() not in btitle.lower():
            continue
        m = match_book(btitle, bauthor, kindle_lib)
        if m and by_id[m[0]].is_kfx:
            kfx_dirs.append(by_id[m[0]].path)
    if kfx_dirs:
        print(f"extracting {len(kfx_dirs)} KFX book(s) via Calibre "
              "(first run only; cached after)...", flush=True)
        kfx.prewarm(kfx_dirs)

    plan = []  # (book, [(highlight, start, end)])
    unmatched = []
    for aid, items in sorted(by_asset.items(), key=lambda kv: titles.get(kv[0], ("~",))[0]):
        btitle, bauthor = titles.get(aid, (aid, ""))
        if args.books and args.books.lower() not in btitle.lower():
            continue
        m = match_book(btitle, bauthor, kindle_lib)
        if not m:
            unmatched.append((btitle, len(items)))
            continue
        book = by_id[m[0]]
        if not (book.is_mobi or book.is_kfx):
            unmatched.append((f"{btitle} [unsupported format {book.mime}]", len(items)))
            continue
        print(f"\n== {btitle!r} -> {book.title!r}", flush=True)
        try:
            mb = open_book(book)
        except (UnsupportedBook, OSError) as e:
            print(f"   {'KFX' if book.is_kfx else 'MOBI'} parse failed: {e}")
            continue
        if book.maxpos is not None and mb.text_length != book.maxpos:
            print(f"   SKIP: textLength {mb.text_length} != app maxpos {book.maxpos} "
                  "(position space mismatch)")
            continue
        existing = ksdk.load_existing_annotations(db, book.dataset_id)
        existing_ranges = [(a.start, a.end) for a in existing
                           if a.dataset in (ksdk.DATASET_HIGHLIGHT, ksdk.DATASET_UNDERLINE)]
        existing_texts = [norm_key(mb.extract_text(s, e)) for s, e in existing_ranges]
        existing_ids = {a.annotation_id for a in existing}

        clip_texts = []
        cm = match_book(btitle, bauthor, [(t, t, "") for t in clippings])
        if cm:
            clip_texts = clippings[cm[0]]

        live = sys.stdout.isatty()
        found, dupes, echoes, misses, weak = [], 0, 0, [], []
        for i, h in enumerate(items, 1):
            if live:
                print(f"\r   searching {i}/{len(items)}: {h.text[:50]!r}\x1b[K",
                      end="", flush=True)
            needle = norm_key(h.text)
            if not needle:
                continue
            if any(texts_similar(needle, ct, 0.9) for ct in clip_texts):
                echoes += 1
                continue
            hit = find_span(mb.norm_text, needle)
            if hit is None:
                misses.append(h)
                continue
            start_n, ln, score = hit
            if score < args.min_score:
                weak.append((h, score))
                continue
            s, e = mb.byte_range_for_norm(start_n, ln)
            # self-check: what Kindle will show at [s,e] must be the passage
            back = norm_key(mb.extract_text(s, e))
            if not texts_similar(back, needle, 0.9):
                misses.append(h)
                continue
            if any(min(e, e2) - max(s, s2) >= 0.5 * min(e - s, e2 - s2)
                   for s2, e2 in existing_ranges) or \
               any(texts_similar(needle, t, 0.8) for t in existing_texts):
                dupes += 1
                continue
            ann_id = f"kindle.{'underline' if h.is_underline else 'highlight'}-{s}"
            if ann_id in existing_ids or any(s == s3 for _, s3, _ in found):
                print(f"\r   COLLISION at start byte {s}, keeping first: "
                      f"{h.text[:50]!r}\x1b[K")
                dupes += 1
                continue
            found.append((h, s, e))
        if live:
            print("\r\x1b[K", end="")
        n_notes = sum(1 for h, _, _ in found if h.note)
        print(f"   matched {len(found)} ({n_notes} with notes), weak {len(weak)}, "
              f"missed {len(misses)}, duplicates {dupes}, clippings-echoes {echoes}")
        for h in misses:
            print(f"     MISS: {h.text[:70]!r}")
        for h, score in weak:
            print(f"     WEAK ({score:.2f}): {h.text[:70]!r}")
        if found:
            plan.append((book, found))

    if unmatched:
        print("\nBooks not found in the Kindle library (skipped):")
        for t, n in unmatched:
            print(f"  {n:4d}  {t}")

    total = sum(len(f) for _, f in plan)
    if args.limit is not None and total > args.limit:
        kept = args.limit
        limited = []
        for book, found in plan:
            take = found[:kept]
            kept -= len(take)
            if take:
                limited.append((book, take))
            if kept == 0:
                break
        plan = limited
        total = sum(len(f) for _, f in plan)
        print(f"\n--limit: capped at {total}")
    print(f"\nTOTAL to sync: {total} highlights across {len(plan)} books")
    if not args.apply:
        print("(dry run — pass --apply to write; requires a prior `capture` run)")
        return
    if not plan:
        return

    # ---- apply ----------------------------------------------------------
    capture = ksdk.load_capture()
    ksdk.quit_kindle(db)
    bdir = ksdk.backup_db(db, REPO_DIR)
    print(f"backed up Kindle annotation DB to {bdir}")

    note_offset = capture.get("note_offset", 6)
    rows = []
    for book, found in plan:
        for h, s, e in found:
            created_ms = int((h.created.timestamp() if h.created else
                              datetime.datetime.now().timestamp()) * 1000)
            ann_id, dataset, payload = ksdk.make_highlight(
                book, s, e, h.color, created_ms, underline=h.is_underline)
            rows.append((ann_id, dataset, book.dataset_id, payload, created_ms))
            if h.note:
                nid, ndataset, npayload = ksdk.make_note(
                    book, e + note_offset, h.note, created_ms)
                rows.append((nid, ndataset, book.dataset_id, npayload, created_ms))
        print(f"  queueing {len(found):4d} -> {book.title}")
    n = ksdk.insert_local_edits(db, rows, capture)
    print(f"inserted {n} rows into the Kindle app's local_edit queue")
    if capture.get("mirror_tables"):
        print(f"note: during capture the app also wrote to {capture['mirror_tables']}; "
              "the app is expected to reconcile these itself after upload")
    print("\nNext: launch the Kindle app, open a synced book and confirm the "
          "highlights sit on the right passages.\nAfter it syncs, re-run "
          "`books2kindle.py sync` (dry run) — everything should now count as "
          "duplicates, and the local_edit queue should be empty (check with:\n"
          f"  sqlite3 '{db}' 'SELECT COUNT(*) FROM local_edit')\n"
          f"If rows linger with growing retry_count, restore the backup from {bdir}.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capture", help="learn the Kindle app's local_edit record format "
                                   "(one-time, offline)")
    sub.add_parser("validate", help="round-trip position math against existing "
                                    "Kindle highlights")
    sp = sub.add_parser("sync", help="sync Apple Books highlights into the Kindle app")
    sp.add_argument("--apply", action="store_true",
                    help="write into the Kindle app (default: dry run)")
    sp.add_argument("--books", help="only books whose Apple Books title contains this "
                                    "substring (case-insensitive)")
    sp.add_argument("--limit", type=int, help="cap the number of highlights written")
    sp.add_argument("--min-score", type=float, default=0.85,
                    help="min fuzzy text match score to sync")
    args = ap.parse_args()
    {"capture": cmd_capture, "validate": cmd_validate, "sync": cmd_sync}[args.cmd](args)


if __name__ == "__main__":
    main()

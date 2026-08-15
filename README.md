# clippyconvert

```
   __
  /  \      ___________________________________________
  |  |     / It looks like you're importing Kindle     \
  @  @    |  highlights into Apple Books.               |
  |  |    |  Would you like some help with that?        |
  || |/    \___________________________________________/
  || ||
  |\_/|
  \___/
```

Convert Kindle `My Clippings.txt` highlights into native Apple Books highlights
(`kindle2books.py`) — and sync Apple Books highlights back into the Mac Kindle
app (`books2kindle.py`).

## How it works

Apple Books stores each highlight as an **EPUB CFI** (a structural pointer into
the book's XHTML) plus the selected text, in a local Core Data SQLite database:

```
~/Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation/AEAnnotation_v10312011_1727_local.sqlite
```

Kindle "locations" and Apple Books "pages" never need to be reconciled: the
highlight text itself is searched for in the sideloaded EPUB, and the matching
range is converted to a CFI — the same representation Books itself writes.
The CFI generator was validated against 161 real Apple Books annotations across
4 books (byte-identical CFIs for 90%, ±1-char boundary differences for the rest).

## Usage

```sh
# Dry run: parse, match books, locate passages, report — writes nothing
python3 kindle2books.py "My Clippings.txt"

# Limit to books whose Kindle title contains a substring
python3 kindle2books.py "My Clippings.txt" --books "red mars"

# Actually import (quits Books, backs up the annotation DB to backups/<timestamp>/ first)
python3 kindle2books.py "My Clippings.txt" --apply

# One-command workflow: pull My Clippings.txt from a USB-connected Kindle, then import
python3 kindle2books.py --from-kindle --apply
```

`--from-kindle` fetches the clippings file straight off the Kindle into the
clippings path (default `./My Clippings.txt`), then continues as normal.
It supports both transports: older Kindles that mount as a USB drive
(`/Volumes/Kindle/documents/My Clippings.txt`) and newer Kindles / Scribe
that use MTP. The MTP path requires libmtp (`brew install libmtp`) and matches
any device identifying as Kindle / Scribe / Amazon / Lab126. Quit OpenMTP or
Android File Transfer first — only one program can hold the MTP connection.

Details:

- `Your Note` and `Bookmark` entries are ignored; `<You have reached the
  clipping limit>` placeholders are dropped.
- Kindle logs a new entry each time a highlight is adjusted; overlapping
  location ranges in the same book are deduplicated (last version wins).
- Kindle titles are fuzzy-matched against the Books library
  (`BKLibrary-1-091020131601.sqlite`); books without a local EPUB are skipped
  and reported.
- Passage search is exact on normalized text (quotes/dashes/whitespace
  unified), with anchor and fuzzy fallbacks for edition differences and
  footnote markers. Matches below `--min-score` (default 0.85) are reported
  as WEAK and skipped.
- Re-running is safe: highlights whose text or CFI already exists in the
  annotation DB are skipped as duplicates.
- Kindle Highlights become yellow highlights (style 3); Kindle Underlines
  become Books underlines (style 0, underline flag).

## Reverse direction: Apple Books → Kindle app

`books2kindle.py` pushes highlights (and their notes) made in Apple Books into
the Mac App Store Kindle app, where they sync to Amazon and appear on all your
Kindle devices.

The Kindle app stores annotations as **byte positions** into the book's
decompressed HTML stream, with no text, in
`~/Library/Containers/com.amazon.Lassen/Data/Library/KSDK/<account>/ksdk_annotation_v1.db`.
The bridge is the same trick in reverse: Books stores the highlighted text
verbatim, so the passage is searched for in the decompressed MOBI stream and
converted to the exact byte range Kindle expects. The position math was
validated by round-tripping all 70 existing Kindle highlights across 13 MOBI
books (100% exact byte ranges), and every planned highlight is additionally
self-checked (text extracted back from the computed range must match) before
it can be written.

Rather than forging "already-synced" rows (which Amazon's delta sync could
reconcile away), records are inserted into the app's own `local_edit` upload
queue, shaped exactly like a user-made highlight — the app then uploads them
itself. The record format is learned once from the app with `capture`:

```sh
# One-time, offline: make one test highlight+note in the Kindle app while
# Wi-Fi is off; the tool diffs the DB before/after to learn the app's codes,
# writing ksdk_capture.json. Follow the prompts.
python3 books2kindle.py capture

# Check the text→position math against your existing Kindle highlights
python3 books2kindle.py validate

# Dry run: match books, locate passages, dedupe, report — writes nothing
python3 books2kindle.py sync

# First real run: keep it small, on one book
python3 books2kindle.py sync --apply --limit 3 --books stella

# Then everything
python3 books2kindle.py sync --apply
```

Details:

- Only highlights made **natively in Books** are synced; rows imported by
  kindle2books.py are excluded (they lack the chapter-title field native rows
  carry), and anything matching a `My Clippings.txt` entry or an existing
  Kindle highlight is skipped — so nothing echoes back and re-runs are
  idempotent.
- Books notes ride along as Kindle note annotations attached at the
  highlight's end position.
- Book matching is fuzzy on titles between the Books library and the Kindle
  app's `BookData.sqlite`.
- MOBI/AZW books only for now (PalmDOC, unencrypted — which is what the app
  stores for personal documents). KFX books are detected and reported but
  skipped; PDF is unsupported.
- `--apply` quits the Kindle app and backs up `ksdk_annotation_v1.db` to
  `backups/<timestamp>/` first. After the app relaunches and syncs, the
  `local_edit` queue should drain to zero — if rows linger with a growing
  `retry_count`, restore the backup.

## Restore from backup

Quit Books, then:

```sh
cp backups/<timestamp>/AEAnnotation_v10312011_1727_local.sqlite* \
   ~/Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation/
```

For the Kindle direction, quit the Kindle app, then:

```sh
cp backups/<timestamp>/ksdk_annotation_v1.db* \
   ~/Library/Containers/com.amazon.Lassen/Data/Library/KSDK/<account-dir>/
```

## Files

- `kindle2books.py` — Kindle → Books CLI entry point
- `books2kindle.py` — Books → Kindle CLI entry point (capture / validate / sync)
- `clippy/parse_clippings.py` — My Clippings.txt parser + dedupe
- `clippy/epub_cfi.py` — EPUB spine/XHTML parsing, CFI generation
- `clippy/textsearch.py` — normalized passage search (exact/anchor/fuzzy), shared by both directions
- `clippy/match.py` — fuzzy title/author book matching, shared by both directions
- `clippy/mobi.py` — minimal MOBI/AZW reader: PalmDOC decompression, byte↔text position maps
- `clippy/books_export.py` — reads Apple Books annotations as a sync source
- `clippy/ksdk.py` — Kindle app DB access (BookData.sqlite, ksdk_annotation_v1.db) and the local_edit writer
- `clippy/kindle_fetch.py` — pulls My Clippings.txt off a USB Kindle (mass storage or MTP)
- `clippy/validate.py` — validates CFI generation against existing Books annotations
- `ksdk_capture.json` — captured Kindle app edit-record codes (created by `books2kindle.py capture`)

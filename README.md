# clippyconvert

Convert Kindle `My Clippings.txt` highlights into native Apple Books highlights.

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

## Restore from backup

Quit Books, then:

```sh
cp backups/<timestamp>/AEAnnotation_v10312011_1727_local.sqlite* \
   ~/Library/Containers/com.apple.iBooksX/Data/Documents/AEAnnotation/
```

## Files

- `kindle2books.py` — CLI entry point
- `clippy/parse_clippings.py` — My Clippings.txt parser + dedupe
- `clippy/epub_cfi.py` — EPUB spine/XHTML parsing, text search, CFI generation
- `clippy/kindle_fetch.py` — pulls My Clippings.txt off a USB Kindle (mass storage or MTP)
- `clippy/validate.py` — validates CFI generation against existing Books annotations

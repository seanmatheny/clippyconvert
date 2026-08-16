#!/usr/bin/env python3
"""KFX content extractor — must run UNDER Calibre's own Python, not system
python3, e.g.:

    calibre-debug -e clippy/kfx_extract.py

It reads two environment variables set by clippy/kfx.py:

  KFX_PLUGIN  directory of the unzipped Calibre "KFX Input" plugin (so that
              `import kfxlib` works; the plugin bundles the only maintained
              KFX/Ion parser, jhowell's kfxlib).
  KFX_JOBS    path to a JSON file: [[book_dir, out_json_path], ...].

For each job it locates the book's KFX `CONT` container, decodes it, walks the
content position map, and writes {"max_position": int, "chunks": [[pid, text],
...]} to out_json_path. A KFX annotation's Kindle shortPosition is exactly the
`pid` of its first/last character, so these (pid, text) chunks are all
clippy/kfx.py needs to map highlight text back to shortPositions.

One status line per job goes to stdout: "OK <dir>" or "ERR <reason> <dir>".
Books that need DRM credentials (which the Mac Kindle app's stored copies do
not) surface as ERR and are simply skipped upstream.
"""

import glob
import json
import os
import sys
import traceback


def find_container(book_dir):
    """Largest file in book_dir whose first 4 bytes are the KFX 'CONT' magic.
    (BookManifest.kfx is a SQLite manifest, not a container, so it's skipped.)"""
    best, best_sz = None, -1
    for f in sorted(glob.glob(os.path.join(book_dir, "*"))):
        if not os.path.isfile(f):
            continue
        try:
            with open(f, "rb") as fh:
                if fh.read(4) != b"CONT":
                    continue
        except OSError:
            continue
        sz = os.path.getsize(f)
        if sz > best_sz:
            best, best_sz = f, sz
    return best


class _QuietLog:
    """kfxlib logs verbosely; swallow everything (unknown methods included)."""
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def __getattr__(self, _): return lambda *a, **k: None


def extract_one(YJ_Book, book_dir, out):
    azw = find_container(book_dir)
    if not azw:
        return "ERR no-container"
    book = YJ_Book(azw)
    book.decode_book()
    chunks = []
    max_position = 0
    for c in book.collect_content_position_info():
        # Every chunk (text, image, or other) occupies `length` positions;
        # the book's max position must span them all — the last position is
        # often a trailing non-text marker with no text — or it undercounts
        # and trips books2kindle's maxpos guard (off-by-one skips the book).
        end = c.pid + getattr(c, "length", 0) - 1
        if end > max_position:
            max_position = end
        text = getattr(c, "text", None)
        if text:
            chunks.append([c.pid, text])
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"max_position": max_position, "chunks": chunks}, f)
    os.replace(tmp, out)
    return "OK"


def main():
    sys.path.insert(0, os.environ["KFX_PLUGIN"])
    from kfxlib import YJ_Book, set_logger
    set_logger(_QuietLog())

    with open(os.environ["KFX_JOBS"]) as f:
        jobs = json.load(f)

    for book_dir, out in jobs:
        try:
            status = extract_one(YJ_Book, book_dir, out)
            print(f"{status} {book_dir}", flush=True)
            if status.startswith("ERR"):
                continue
        except Exception as e:  # DRM, parse errors, anything — skip the book
            print(f"ERR {type(e).__name__} {book_dir}", flush=True)
            traceback.print_exc()


if __name__ == "__main__":
    main()

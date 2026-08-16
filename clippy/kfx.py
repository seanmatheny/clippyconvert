"""KFX reader for mapping Apple Books highlight text to Kindle shortPositions.

The counterpart to clippy/mobi.py for Amazon's newer KFX format. Where MOBI
shortPositions are byte offsets into a decompressed HTML stream, a KFX
annotation's shortPosition is a `pid` — a global position counter that
advances by one for each rendered character (images and other non-text content
consume positions too, leaving gaps). Parsing KFX means parsing the Amazon Ion
container it's built from; rather than reimplement that, we drive Calibre's
"KFX Input" plugin (jhowell's kfxlib), the maintained KFX parser, out of
process — it must run under Calibre's own Python, so clippy/kfx_extract.py is
invoked via `calibre-debug -e`. That helper emits (pid, text) chunks as JSON,
which we cache per book and turn into the same text<->position machinery
MobiBook exposes, so books2kindle.py can treat both formats through one
interface (norm_text / byte_range_for_norm / extract_text / text_length).

Requires Calibre with the "KFX Input" plugin installed. The Mac Kindle app's
stored KFX books decode without DRM credentials; any that don't (or otherwise
fail to parse) raise UnsupportedBook and are skipped, exactly like an
unsupported MOBI.
"""

import bisect
import glob
import hashlib
import json
import os
import shutil
import subprocess
import zipfile

from clippy.epub_cfi import normalize_with_map
from clippy.mobi import UnsupportedBook  # shared skip signal for both formats

CALIBRE_DEBUG = os.environ.get(
    "CALIBRE_DEBUG", "/Applications/calibre.app/Contents/MacOS/calibre-debug")
PLUGIN_ZIP = os.environ.get(
    "KFX_INPUT_PLUGIN",
    os.path.expanduser("~/Library/Preferences/calibre/plugins/KFX Input.zip"))
CACHE_DIR = os.path.expanduser("~/Library/Caches/clippyconvert")
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kfx_extract.py")


# ---- container discovery + cache keys ----------------------------------

def _find_container(book_dir: str):
    """Largest 'CONT'-magic file in book_dir (the KFX content container)."""
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


def _cache_path(book_dir: str) -> str:
    """Where the extracted chunks JSON lives, keyed on the container's
    identity so a re-downloaded book re-extracts."""
    c = _find_container(book_dir)
    if not c:
        raise UnsupportedBook(f"no KFX CONT container in {book_dir}")
    st = os.stat(c)
    key = hashlib.sha1(
        f"{c}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, "chunks", key + ".json")


# ---- calibre / plugin plumbing -----------------------------------------

def _ensure_plugin() -> str:
    """Unzip the KFX Input plugin into the cache (once per plugin version) so
    kfx_extract.py can `import kfxlib`. Returns the extracted dir."""
    if not os.path.exists(PLUGIN_ZIP):
        raise UnsupportedBook(
            f"Calibre 'KFX Input' plugin not found at {PLUGIN_ZIP} — install it "
            "in Calibre (Preferences > Plugins > Get new plugins > KFX Input)")
    if not os.path.exists(CALIBRE_DEBUG):
        raise UnsupportedBook(
            f"calibre-debug not found at {CALIBRE_DEBUG} — install Calibre or set "
            "the CALIBRE_DEBUG env var to its path")
    pdir = os.path.join(CACHE_DIR, "kfx-input")
    stamp = os.path.join(pdir, ".zip-mtime")
    zmt = str(int(os.path.getmtime(PLUGIN_ZIP)))
    fresh = (os.path.exists(stamp)
             and open(stamp).read() == zmt
             and os.path.isdir(os.path.join(pdir, "kfxlib")))
    if not fresh:
        shutil.rmtree(pdir, ignore_errors=True)
        os.makedirs(pdir, exist_ok=True)
        with zipfile.ZipFile(PLUGIN_ZIP) as z:
            z.extractall(pdir)
        with open(stamp, "w") as f:
            f.write(zmt)
    return pdir


def prewarm(book_dirs) -> None:
    """Extract (pid, text) chunks for any uncached KFX books in one calibre
    invocation — amortizes Calibre's multi-second startup across the batch.
    Already-cached books and non-KFX dirs are skipped silently."""
    jobs = []
    for d in book_dirs:
        try:
            out = _cache_path(d)
        except UnsupportedBook:
            continue
        if not os.path.exists(out):
            jobs.append([d, out])
    if not jobs:
        return
    plugin = _ensure_plugin()
    os.makedirs(os.path.join(CACHE_DIR, "chunks"), exist_ok=True)
    jobs_file = os.path.join(CACHE_DIR, "jobs.json")
    with open(jobs_file, "w") as f:
        json.dump(jobs, f)
    env = dict(os.environ, KFX_PLUGIN=plugin, KFX_JOBS=jobs_file)
    subprocess.run([CALIBRE_DEBUG, "-e", HELPER], env=env,
                   capture_output=True, text=True)
    # Missing outputs (DRM/parse failures) surface when from_dir tries to load.


# ---- the book ----------------------------------------------------------

class KfxBook:
    """Text<->shortPosition map for one KFX book. Mirrors the MobiBook
    interface used by books2kindle.py."""

    def __init__(self, path, text_length, stripped, pos, norm_text, norm_map):
        self.path = path
        self.text_length = text_length  # max inclusive position (== app maxpos)
        self.stripped = stripped        # concatenated chunk text, reading order
        self.pos = pos                  # pos[i] = pid of stripped[i] (ascending)
        self.norm_text = norm_text
        self.norm_map = norm_map        # norm idx -> index into stripped

    @classmethod
    def from_chunks(cls, path: str, data: dict) -> "KfxBook":
        chunks = sorted(data["chunks"], key=lambda c: c[0])
        parts, pos = [], []
        for pid, text in chunks:
            parts.append(text)
            pos.extend(range(pid, pid + len(text)))
        stripped = "".join(parts)
        norm_text, norm_map = normalize_with_map(stripped)
        return cls(path, data["max_position"], stripped, pos, norm_text, norm_map)

    @classmethod
    def from_dir(cls, book_dir: str) -> "KfxBook":
        out = _cache_path(book_dir)
        if not os.path.exists(out):
            prewarm([book_dir])
        if not os.path.exists(out):
            raise UnsupportedBook(
                f"KFX extraction failed (DRM or unsupported): {book_dir}")
        with open(out) as f:
            return cls.from_chunks(book_dir, json.load(f))

    def byte_range_for_norm(self, norm_start: int, norm_len: int):
        """Map a span in norm_text to an inclusive (start, end) shortPosition
        range. Named to match MobiBook so callers stay format-agnostic; the
        values are KFX positions, not bytes."""
        first_char = self.norm_map[norm_start]
        last_char = self.norm_map[norm_start + norm_len - 1]
        return self.pos[first_char], self.pos[last_char]

    def extract_text(self, start: int, end: int) -> str:
        """Text occupying the inclusive shortPosition range (for validation
        and dedupe against existing Kindle highlights)."""
        lo = bisect.bisect_left(self.pos, start)
        hi = bisect.bisect_right(self.pos, end)
        return self.stripped[lo:hi]

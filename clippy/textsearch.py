"""Fuzzy passage search over normalized text, shared by the EPUB (CFI) and
MOBI (byte-position) pipelines.

The cascade is exact find -> head/tail anchor match -> seeded fuzzy window
diff. Epub.find_text runs the stages itself so it can interleave them across
spine documents; find_span runs the whole cascade over a single haystack
(a MOBI book is one continuous text stream).
"""

import difflib
import re
from typing import Optional


def strip_footnote_markers(s: str) -> str:
    """Drop Kindle-side footnote markers like [*4] or [12] — they rarely
    appear identically in the book's text flow."""
    return re.sub(r"\[\*?\d+\]|\[\*\]", "", s)


def seed_candidates(hay: str, needle: str, seed_len: int = 12, max_hits: int = 50):
    """Yield (start, votes) for offsets in hay where a fuzzy match of needle
    could plausibly begin.

    Takes exact seed_len chunks of the needle at seed_len strides and locates
    them in hay with str.find (C speed). A fuzzy match must contain at least
    one unbroken seed-sized run, so every real match yields a candidate — and
    since most of its seeds land intact at consistent alignment, it collects
    many votes, while a coincidental phrase hit collects one. Candidates
    within seed_len of each other are merged, summing votes.
    """
    if len(needle) < seed_len:
        seed_len = max(6, len(needle))
    starts = []
    for off in range(0, len(needle) - seed_len + 1, seed_len):
        seed = needle[off : off + seed_len]
        i = hay.find(seed)
        hits = 0
        while i >= 0 and hits < max_hits:
            starts.append(max(0, i - off))
            hits += 1
            i = hay.find(seed, i + 1)
    merged = []  # [start, votes]
    for s in sorted(starts):
        if merged and s - merged[-1][0] < seed_len:
            merged[-1][1] += 1
        else:
            merged.append([s, 1])
    return [(s, v) for s, v in merged]


def anchor_find(hay: str, needle: str, anchor: int = 60) -> Optional[tuple]:
    """Exact-match the needle's head and tail fragments, tolerating junk
    (footnote refs etc.) in the middle. Returns (start, length) or None."""
    if len(needle) <= 2 * anchor:
        return None
    head, tail = needle[:anchor], needle[-anchor:]
    i = hay.find(head)
    if i < 0:
        return None
    j = hay.find(tail, i)
    if j < 0:
        return None
    end = j + len(tail)
    if end - i > len(needle) * 1.3 + 60:
        return None
    return i, end - i


def fuzzy_window(hay: str, needle: str, start: int, min_score: float,
                 min_block: int) -> Optional[tuple]:
    """Diff needle against a small window of hay around start.
    Returns (start, length, score) or None."""
    lo = max(0, start - 40)
    hi = min(len(hay), start + len(needle) + 40)
    window = hay[lo:hi]
    sm = difflib.SequenceMatcher(None, window, needle, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= min_block]
    if not blocks:
        return None
    covered = sum(b.size for b in blocks)
    score = covered / len(needle)
    if score < min_score:
        return None
    w_start = blocks[0].a
    w_end = blocks[-1].a + blocks[-1].size
    # sanity: matched window shouldn't be wildly longer than needle
    if w_end - w_start > len(needle) * 1.6 + 40:
        return None
    return lo + w_start, w_end - w_start, score


def min_block_for(needle: str) -> int:
    return max(6, min(12, len(needle) // 6))


def find_span(norm_hay: str, norm_needle: str, min_score: float = 0.55) -> Optional[tuple]:
    """Full cascade over one haystack. Returns (start, length, score) in
    normalized coordinates, or None. Inputs must already be normalized
    (see epub_cfi.normalize_with_map)."""
    if not norm_needle:
        return None
    idx = norm_hay.find(norm_needle)
    if idx >= 0:
        return idx, len(norm_needle), 1.0
    hit = anchor_find(norm_hay, norm_needle)
    if hit:
        return hit[0], hit[1], 0.95
    best = None
    min_block = min_block_for(norm_needle)
    candidates = seed_candidates(norm_hay, norm_needle)
    # The window diff is O(needle²); only the best-voted few can afford it.
    candidates.sort(key=lambda t: -t[1])
    for start, _votes in candidates[:5]:
        hit = fuzzy_window(norm_hay, norm_needle, start, min_score, min_block)
        if hit and (best is None or hit[2] > best[2]):
            best = hit
    return best

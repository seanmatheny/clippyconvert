"""Fuzzy title/author matching between book libraries (Kindle, Apple Books)."""

import difflib
import re


def norm_title(s: str) -> str:
    s = s.replace("_", " ")
    s = re.sub(r"--.*$", "", s)  # strip "-- Author -- Year -- ... Anna's Archive" tails
    s = re.sub(r"[^\w\s]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def title_tokens(s: str) -> set:
    stop = {"the", "a", "an", "of", "and", "in", "to", "for", "on", "s"}
    return {t for t in norm_title(s).split() if t not in stop}


def match_book(title, author, library, threshold: float = 0.75):
    """Find the best library entry for a (title, author). library is a list of
    tuples whose first three fields are (key, title, author); the winning
    tuple is returned whole (extra fields pass through), or None."""
    kt = title_tokens(title)
    if not kt:
        return None
    best, best_score = None, 0.0
    for row in library:
        lib_title, lib_author = row[1], row[2]
        lt = title_tokens(lib_title)
        if not lt:
            continue
        overlap = len(kt & lt) / min(len(kt), len(lt))
        ratio = difflib.SequenceMatcher(None, norm_title(title), norm_title(lib_title)).ratio()
        score = max(overlap, ratio)
        # author corroboration
        if author and lib_author:
            ka, la = title_tokens(author), title_tokens(lib_author)
            if ka and la and not (ka & la):
                score -= 0.25
        if score > best_score:
            best, best_score = row, score
    if best and best_score >= threshold:
        return best
    return None

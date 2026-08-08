"""Parse a Kindle "My Clippings.txt" file into structured highlight records."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

ENTRY_SEP = "=========="

META_RE = re.compile(
    r"- Your (?P<kind>Highlight|Underline|Note|Bookmark)"
    r"(?: on (?:page (?P<page>[\divxlc-]+)|[Ll]ocation (?P<loc_start2>\d+)(?:-(?P<loc_end2>\d+))?))?"
    r"(?:\s*\|\s*[Ll]ocation (?P<loc_start>\d+)(?:-(?P<loc_end>\d+))?)?"
    r"(?:\s*\|\s*Added on (?P<added>.+))?$"
)


@dataclass
class Clipping:
    title: str
    author: Optional[str]
    kind: str  # Highlight | Underline | Note | Bookmark
    page: Optional[str]
    loc_start: Optional[int]
    loc_end: Optional[int]
    added: Optional[datetime]
    text: str


def _parse_title_author(line: str):
    line = line.strip().lstrip("﻿").strip()
    m = re.match(r"^(?P<title>.*)\((?P<author>[^()]*)\)\s*$", line)
    if m:
        return m.group("title").strip(), m.group("author").strip()
    return line, None


def parse_clippings(path: str) -> list[Clipping]:
    raw = open(path, encoding="utf-8-sig").read()
    entries = [e.strip("\n\r ") for e in raw.split(ENTRY_SEP)]
    out = []
    for entry in entries:
        if not entry.strip():
            continue
        lines = entry.split("\n")
        if len(lines) < 2:
            continue
        title, author = _parse_title_author(lines[0])
        m = META_RE.match(lines[1].strip().lstrip("﻿"))
        if not m:
            continue
        added = None
        if m.group("added"):
            try:
                added = datetime.strptime(m.group("added").strip(), "%A, %d %B %Y %H:%M:%S")
            except ValueError:
                pass
        text = "\n".join(lines[2:]).strip()
        out.append(
            Clipping(
                title=title,
                author=author,
                kind=m.group("kind"),
                page=m.group("page"),
                loc_start=int(m.group("loc_start") or m.group("loc_start2") or 0) or None,
                loc_end=int(m.group("loc_end") or m.group("loc_end2") or 0) or None,
                added=added,
                text=text,
            )
        )
    return out


def dedupe_highlights(clips: list[Clipping]) -> list[Clipping]:
    """Keep only Highlights/Underlines; drop progressive re-highlights.

    Kindle logs a new entry every time a highlight is adjusted. Later entries with
    an overlapping location range in the same book supersede earlier ones when one
    text contains the other. We iterate in file order (chronological) and drop an
    earlier entry when a later overlapping entry's text contains it, or replace it.
    """
    kept: list[Clipping] = []
    for c in clips:
        if c.kind not in ("Highlight", "Underline"):
            continue
        if not c.text or c.text.startswith("<You have reached the clipping limit"):
            continue
        superseded = []
        skip = False
        for i, k in enumerate(kept):
            if k.title != c.title:
                continue
            if k.loc_start is None or c.loc_start is None:
                continue
            k_end = k.loc_end or k.loc_start
            c_end = c.loc_end or c.loc_start
            if k.loc_start <= c_end and c.loc_start <= k_end:  # overlap
                a, b = k.text.strip(), c.text.strip()
                if a in b or b in a or _share_edge(a, b):
                    superseded.append(i)
        for i in reversed(superseded):
            del kept[i]
        if not skip:
            kept.append(c)
    return kept


def _share_edge(a: str, b: str, n: int = 20) -> bool:
    """True if one string starts or ends with a decent chunk of the other."""
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    probe = short[: min(n, len(short))]
    probe2 = short[-min(n, len(short)):]
    return long.startswith(probe) or long.endswith(probe2)


if __name__ == "__main__":
    import sys
    from collections import Counter

    clips = parse_clippings(sys.argv[1])
    print(f"parsed {len(clips)} entries")
    hl = dedupe_highlights(clips)
    print(f"kept {len(hl)} highlights/underlines after dedupe")
    books = Counter(c.title for c in hl)
    for b, n in books.most_common():
        print(f"  {n:4d}  {b}")

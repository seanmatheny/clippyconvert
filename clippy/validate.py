"""Validate the CFI generator against real Apple Books annotations."""

import sqlite3
import sys

sys.path.insert(0, "/Users/smat924/git/clippyconvert")
from clippy.epub_cfi import Epub

DB = sys.argv[1]
ASSET = sys.argv[2]
EPUB = sys.argv[3]

con = sqlite3.connect(DB)
rows = con.execute(
    "SELECT ZANNOTATIONLOCATION, ZANNOTATIONSELECTEDTEXT FROM ZAEANNOTATION "
    "WHERE ZANNOTATIONASSETID=? AND ZANNOTATIONDELETED=0 "
    "AND ZANNOTATIONSELECTEDTEXT IS NOT NULL AND ZANNOTATIONLOCATION LIKE 'epubcfi%'",
    (ASSET,),
).fetchall()
print(f"{len(rows)} ground-truth annotations")

epub = Epub(EPUB)
exact = close = miss = 0
for want_cfi, text in rows:
    loc = epub.find_text(text)
    got = loc.cfi if loc else None
    if got == want_cfi:
        exact += 1
        status = "EXACT"
    elif got and got.split("!")[0] == want_cfi.split("!")[0]:
        close += 1
        status = "DIFF "
    else:
        miss += 1
        status = "MISS "
    if status != "EXACT":
        print(f"[{status}] text: {text[:60]!r}")
        print(f"   want: {want_cfi}")
        print(f"   got : {got}  (score={loc.score if loc else 0:.2f})")
print(f"\nexact={exact} same-chapter-diff={close} miss={miss}")

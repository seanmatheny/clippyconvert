"""Locate passages of text in an EPUB and emit Apple Books-style EPUB CFI ranges.

Works against an unzipped EPUB directory (how Apple Books stores sideloaded
books) or a .epub zip file.

CFI child numbering (per the EPUB CFI spec):
  - element children get even indices 2, 4, 6, ... in document order
  - character data between/around elements gets the interleaved odd indices;
    a text run before the first element is 1, between element k and k+1 is 2k+1
  - character offsets (:n) index into the text run
Apple appends [id] assertions on steps whose element carries an id attribute,
and uses the spine itemref idref as the assertion on the spine step.
"""

import os
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from lxml import etree

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

SKIP_TAGS = {"script", "style", "head", "title"}


def _localname(el) -> Optional[str]:
    if not isinstance(el.tag, str):
        return None
    return etree.QName(el).localname if el.tag.startswith("{") else el.tag


class EpubFiles:
    """Uniform file access over an unzipped epub dir or a .epub zip."""

    def __init__(self, path: str):
        self.path = path
        self.zf = None
        if os.path.isfile(path):
            self.zf = zipfile.ZipFile(path)

    def read(self, name: str) -> bytes:
        if self.zf:
            return self.zf.read(name)
        return open(os.path.join(self.path, name), "rb").read()

    def exists(self, name: str) -> bool:
        if self.zf:
            return name in self.zf.namelist()
        return os.path.exists(os.path.join(self.path, name))


@dataclass
class TextRun:
    steps: tuple  # CFI steps from document root down to the odd text index
    text: str
    doc_start: int  # offset of run start in the concatenated document text


@dataclass
class SpineDoc:
    idref: str
    href: str
    spine_step: str  # e.g. "/6/12[introduction]"
    runs: list = field(default_factory=list)
    full_text: str = ""
    norm_text: str = ""
    norm_map: list = field(default_factory=list)  # norm idx -> doc idx


@dataclass
class Location:
    doc: SpineDoc
    cfi: str  # full epubcfi(...) range
    matched_text: str
    score: float  # 1.0 = exact normalized match


class CfiStep:
    __slots__ = ("index", "assertion")

    def __init__(self, index: int, assertion: Optional[str] = None):
        self.index = index
        self.assertion = assertion

    def __str__(self):
        return f"/{self.index}[{self.assertion}]" if self.assertion else f"/{self.index}"

    def __repr__(self):
        return str(self)


def _norm_char(ch: str) -> str:
    """Normalize a character for fuzzy-tolerant comparison."""
    if ch in "‘’ʼ'":
        return "'"
    if ch in "“”„\"":
        return '"'
    if ch in "–—―-":
        return "-"
    if ch == "…":
        return "."  # ellipsis -> single char to keep mapping simple
    if ch in "     \t\r\n\f\v ":
        return " "
    return ch.lower()


def normalize_with_map(s: str):
    """Return (normalized_string, map) where map[i] = index into s.

    Collapses whitespace runs into a single space and applies _norm_char.
    Leading/trailing whitespace is dropped.
    """
    out = []
    idx_map = []
    prev_space = True
    for i, ch in enumerate(s):
        c = _norm_char(ch)
        if c == " ":
            if prev_space:
                continue
            prev_space = True
            out.append(" ")
            idx_map.append(i)
        else:
            prev_space = False
            out.append(c)
            idx_map.append(i)
    while out and out[-1] == " ":
        out.pop()
        idx_map.pop()
    return "".join(out), idx_map


from clippy.textsearch import (
    anchor_find,
    fuzzy_window,
    min_block_for,
    seed_candidates,
    strip_footnote_markers,
)


class Epub:
    def __init__(self, path: str):
        self.files = EpubFiles(path)
        self.opf_path = self._find_opf()
        self.opf_dir = posixpath.dirname(self.opf_path)
        self.spine: list[SpineDoc] = []
        self._parse_opf()

    def _find_opf(self) -> str:
        container = etree.fromstring(self.files.read("META-INF/container.xml"))
        rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
        return rootfile.get("full-path")

    def _parse_opf(self):
        root = etree.fromstring(self.files.read(self.opf_path))
        # index of <spine> among package's element children (1-based) * 2
        pkg_children = [c for c in root if isinstance(c.tag, str)]
        spine_el = manifest_el = None
        spine_cfi_idx = None
        for i, c in enumerate(pkg_children):
            ln = etree.QName(c).localname
            if ln == "spine":
                spine_el = c
                spine_cfi_idx = (i + 1) * 2
            elif ln == "manifest":
                manifest_el = c
        items = {}
        for item in manifest_el:
            if isinstance(item.tag, str):
                items[item.get("id")] = item.get("href")
        itemrefs = [c for c in spine_el if isinstance(c.tag, str)]
        for j, ref in enumerate(itemrefs):
            idref = ref.get("idref")
            href = items.get(idref)
            if not href:
                continue
            step = f"/{spine_cfi_idx}/{(j + 1) * 2}[{idref}]"
            self.spine.append(SpineDoc(idref=idref, href=href, spine_step=step))

    # ---- document text extraction -------------------------------------

    def _doc_bytes(self, doc: SpineDoc) -> bytes:
        name = posixpath.normpath(posixpath.join(self.opf_dir, doc.href))
        return self.files.read(name)

    def load_doc(self, doc: SpineDoc):
        if doc.runs:
            return
        data = self._doc_bytes(doc)
        parser = etree.XMLParser(recover=True, resolve_entities=True)
        root = etree.fromstring(data, parser=parser)
        if root is None:
            raise ValueError(f"cannot parse {doc.href}")
        runs: list[TextRun] = []
        pieces: list[str] = []
        pos = 0

        def walk(el, steps):
            nonlocal pos
            # children with CFI indices: elements even, text runs odd
            elem_children = [c for c in el if isinstance(c.tag, str)]
            # text run before first element (odd index 1)
            run_texts = {}  # odd index -> text
            if el.text:
                run_texts[1] = el.text
            for k, child in enumerate(elem_children):
                if child.tail:
                    run_texts[2 * (k + 1) + 1] = child.tail
            # traverse in document order: text(1), elem(2), text(3), elem(4)...
            order = []
            if 1 in run_texts:
                order.append(("t", 1))
            for k, child in enumerate(elem_children):
                order.append(("e", k, child))
                if 2 * (k + 1) + 1 in run_texts:
                    order.append(("t", 2 * (k + 1) + 1))
            for entry in order:
                if entry[0] == "t":
                    odd = entry[1]
                    txt = run_texts[odd]
                    runs.append(TextRun(steps + (CfiStep(odd),), txt, pos))
                    pieces.append(txt)
                    pos += len(txt)
                else:
                    _, k, child = entry
                    ln = _localname(child)
                    if ln in SKIP_TAGS:
                        continue
                    assertion = child.get("id")
                    walk(child, steps + (CfiStep(2 * (k + 1), assertion),))

        # find body: html children
        html_children = [c for c in root if isinstance(c.tag, str)]
        for k, child in enumerate(html_children):
            if _localname(child) == "body":
                walk(child, (CfiStep(2 * (k + 1), child.get("id")),))
                break
        doc.runs = runs
        doc.full_text = "".join(pieces)
        doc.norm_text, doc.norm_map = normalize_with_map(doc.full_text)

    # ---- position -> CFI ------------------------------------------------

    def _point(self, doc: SpineDoc, doc_offset: int, is_end: bool):
        """Map a document text offset to (steps, char_offset_in_run)."""
        lo, hi = 0, len(doc.runs) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if doc.runs[mid].doc_start <= doc_offset - (1 if is_end else 0):
                lo = mid
            else:
                hi = mid - 1
        run = doc.runs[lo]
        off = doc_offset - run.doc_start
        off = max(0, min(off, len(run.text)))
        return run.steps, off

    def make_cfi(self, doc: SpineDoc, start: int, end: int) -> str:
        s_steps, s_off = self._point(doc, start, is_end=False)
        e_steps, e_off = self._point(doc, end, is_end=True)
        # common prefix by (index, assertion); sibling steps at the same depth
        # under the same parent always have distinct indices, so this is safe
        max_n = min(len(s_steps), len(e_steps))
        n = 0
        while (
            n < max_n
            and s_steps[n].index == e_steps[n].index
            and s_steps[n].assertion == e_steps[n].assertion
        ):
            n += 1
        common = s_steps[:n]
        s_rel = s_steps[n:]
        e_rel = e_steps[n:]
        parent = doc.spine_step + "!" + "".join(str(st) for st in common)
        s_part = "".join(str(st) for st in s_rel) + f":{s_off}"
        e_part = "".join(str(st) for st in e_rel) + f":{e_off}"
        return f"epubcfi({parent},{s_part},{e_part})"

    # ---- search ---------------------------------------------------------

    def find_text(self, needle: str, min_score: float = 0.55) -> Optional[Location]:
        """Find the passage in the book; return best Location or None."""
        norm_needle, _ = normalize_with_map(strip_footnote_markers(needle))
        if not norm_needle:
            return None
        for doc in self.spine:
            try:
                self.load_doc(doc)
            except Exception:
                continue
            idx = doc.norm_text.find(norm_needle)
            if idx >= 0:
                return self._loc_from_norm(doc, idx, len(norm_needle), 1.0)
        for doc in self.spine:
            if not doc.norm_text:
                continue
            hit = anchor_find(doc.norm_text, norm_needle)
            if hit:
                return self._loc_from_norm(doc, hit[0], hit[1], 0.95)
        # fuzzy fallback: diffing the needle against a whole book is O(book *
        # needle) in pure Python — minutes per clipping on a long book. Instead
        # find exact seed chunks of the needle at C speed, then diff only small
        # candidate windows around each hit.
        best = None
        min_block = min_block_for(norm_needle)
        candidates = []
        for doc in self.spine:
            if not doc.norm_text:
                continue
            for start, votes in seed_candidates(doc.norm_text, norm_needle):
                candidates.append((votes, doc, start))
        # The window diff is O(needle²); only the best-voted few can afford it.
        candidates.sort(key=lambda t: -t[0])
        for _votes, doc, start in candidates[:5]:
            hit = fuzzy_window(doc.norm_text, norm_needle, start, min_score, min_block)
            if hit and (best is None or hit[2] > best[3]):
                best = (doc, hit[0], hit[1], hit[2])
        if best:
            doc, start, ln, score = best
            return self._loc_from_norm(doc, start, ln, score)
        return None

    def _loc_from_norm(self, doc: SpineDoc, norm_idx: int, norm_len: int, score: float):
        doc_start = doc.norm_map[norm_idx]
        last_norm = norm_idx + norm_len - 1
        doc_end = doc.norm_map[last_norm] + 1
        cfi = self.make_cfi(doc, doc_start, doc_end)
        return Location(
            doc=doc,
            cfi=cfi,
            matched_text=doc.full_text[doc_start:doc_end],
            score=score,
        )

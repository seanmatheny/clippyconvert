"""Minimal MOBI/AZW reader for mapping text to Kindle shortPositions.

A Kindle annotation's shortPosition is a byte offset into the concatenated
decompressed text records — the raw HTML stream including tags. start is the
first byte of the selection, end the last byte (inclusive). This module
decompresses that stream and builds byte maps between it and a tag-stripped,
searchable text, so a passage found by text search can be converted back to
the exact byte range Kindle expects.

Supports PalmDOC-compressed (and uncompressed), unencrypted MOBI only — which
is what the Mac Kindle app stores for MOBI personal documents. HUFF/CDIC and
DRM raise UnsupportedBook.
"""

import struct
from dataclasses import dataclass, field
from html.entities import name2codepoint

from clippy.epub_cfi import normalize_with_map

COMPRESSION_NONE = 1
COMPRESSION_PALMDOC = 2
COMPRESSION_HUFF = 17480

# tags that break text flow; stripping them emits a space so passages spanning
# paragraphs stay word-separated. Inline tags (<i>, <a>, ...) emit nothing so
# words split by markup ("any<i>where</i>") survive intact.
BLOCK_TAGS = {
    "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol",
    "tr", "td", "th", "table", "blockquote", "hr", "dd", "dt", "dl", "pre",
    "mbp:pagebreak", "body", "html", "head",
}


class UnsupportedBook(Exception):
    pass


def _palmdoc_decompress(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if c == 0:
            out.append(0)
        elif c <= 8:  # literal run
            out += data[i : i + c]
            i += c
        elif c <= 0x7F:
            out.append(c)
        elif c <= 0xBF:  # LZ77 back-reference
            c = (c << 8) | data[i]
            i += 1
            dist = (c >> 3) & 0x7FF
            length = (c & 7) + 3
            for _ in range(length):
                out.append(out[-dist])
        else:  # 0xC0-0xFF: space + char
            out.append(32)
            out.append(c ^ 0x80)
    return bytes(out)


def _trailing_entry_size(data: bytes, size: int) -> int:
    """Backward varint at data[:size]'s end; value includes its own bytes."""
    bitpos, result = 0, 0
    while size > 0:
        v = data[size - 1]
        result |= (v & 0x7F) << bitpos
        bitpos += 7
        size -= 1
        if v & 0x80 or bitpos >= 28:
            break
    return result


def _trim_trailing(data: bytes, extra_flags: int) -> bytes:
    num = 0
    flags = extra_flags >> 1
    while flags:
        if flags & 1:
            num += _trailing_entry_size(data, len(data) - num)
        flags >>= 1
    if extra_flags & 1:  # multibyte char overlap entry
        num += (data[len(data) - num - 1] & 0x3) + 1
    return data[: len(data) - num]


@dataclass
class MobiBook:
    path: str
    raw: bytes  # decompressed text stream; shortPositions index into this
    encoding: str
    text_length: int
    stripped: str = field(default="", repr=False)
    strip_map: list = field(default_factory=list, repr=False)  # char -> first raw byte
    strip_end_map: list = field(default_factory=list, repr=False)  # char -> last raw byte
    norm_text: str = field(default="", repr=False)
    norm_map: list = field(default_factory=list, repr=False)  # norm idx -> char idx

    @classmethod
    def from_file(cls, path: str) -> "MobiBook":
        with open(path, "rb") as f:
            data = f.read()
        if len(data) < 78 or data[60:68] != b"BOOKMOBI":
            raise UnsupportedBook(f"not a MOBI PalmDB: {path}")
        (num_records,) = struct.unpack_from(">H", data, 76)
        offsets = [struct.unpack_from(">I", data, 78 + 8 * i)[0] for i in range(num_records)]
        offsets.append(len(data))

        def record(i: int) -> bytes:
            return data[offsets[i] : offsets[i + 1]]

        rec0 = record(0)
        compression, _, text_length, record_count, _, encryption = struct.unpack_from(
            ">HHIHHH", rec0, 0
        )
        if encryption != 0:
            raise UnsupportedBook(f"encrypted book (encryption={encryption})")
        if compression == COMPRESSION_HUFF:
            raise UnsupportedBook("HUFF/CDIC compression not supported")
        if compression not in (COMPRESSION_NONE, COMPRESSION_PALMDOC):
            raise UnsupportedBook(f"unknown compression {compression}")
        if rec0[16:20] != b"MOBI":
            raise UnsupportedBook("missing MOBI header")
        (hdrlen,) = struct.unpack_from(">I", rec0, 20)
        (enc_code,) = struct.unpack_from(">I", rec0, 28)
        encoding = {65001: "utf-8", 1252: "cp1252"}.get(enc_code)
        if encoding is None:
            raise UnsupportedBook(f"unknown text encoding {enc_code}")
        extra_flags = 0
        if hdrlen >= 0xE4 and len(rec0) >= 0xF4:
            (extra_flags,) = struct.unpack_from(">H", rec0, 0xF2)

        pieces = []
        for i in range(1, record_count + 1):
            rec = _trim_trailing(record(i), extra_flags)
            pieces.append(_palmdoc_decompress(rec) if compression == COMPRESSION_PALMDOC else rec)
        raw = b"".join(pieces)
        if len(raw) < text_length:
            raise UnsupportedBook(
                f"decompressed {len(raw)} bytes < declared textLength {text_length}"
            )
        raw = raw[:text_length]

        book = cls(path=path, raw=raw, encoding=encoding, text_length=text_length)
        book.stripped, book.strip_map, book.strip_end_map = strip_html(raw, encoding)
        book.norm_text, book.norm_map = normalize_with_map(book.stripped)
        return book

    def byte_range_for_norm(self, norm_start: int, norm_len: int):
        """Map a span in norm_text to a (start, end) raw byte range, end
        inclusive — the shortPosition convention."""
        first_char = self.norm_map[norm_start]
        last_char = self.norm_map[norm_start + norm_len - 1]
        return self.strip_map[first_char], self.strip_end_map[last_char]

    def extract_text(self, start_byte: int, end_byte: int) -> str:
        """Tag-stripped text of the inclusive raw byte range (for validation
        and dedupe against existing Kindle highlights)."""
        text, _, _ = strip_html(self.raw[start_byte : end_byte + 1], self.encoding)
        return text


def _decode_entity(seg: bytes):
    """Parse an HTML entity at the start of seg. Returns (char, length_bytes)
    or None if seg doesn't start with a well-formed entity."""
    end = seg.find(b";", 1, 12)
    if end < 0:
        return None
    body = seg[1:end]
    try:
        if body.startswith(b"#x") or body.startswith(b"#X"):
            cp = int(body[2:], 16)
        elif body.startswith(b"#"):
            cp = int(body[1:], 10)
        else:
            cp = name2codepoint.get(body.decode("ascii"))
            if cp is None:
                return None
        return chr(cp), end + 1
    except (ValueError, UnicodeDecodeError, OverflowError):
        return None


def _utf8_char_len(lead: int) -> int:
    if lead < 0x80:
        return 1
    if lead >= 0xF0:
        return 4
    if lead >= 0xE0:
        return 3
    if lead >= 0xC0:
        return 2
    return 1  # stray continuation byte; consume alone


def strip_html(raw: bytes, encoding: str):
    """Strip tags/entities from raw HTML bytes, keeping byte maps.

    Returns (text, start_map, end_map) where start_map[i] is the raw byte
    offset of text[i]'s first byte and end_map[i] of its last byte. Block-level
    tags emit a space (mapped to the tag's first byte) so paragraph boundaries
    stay searchable; inline tags emit nothing.
    """
    out = []
    start_map = []
    end_map = []
    i, n = 0, len(raw)

    def emit(ch, start, end):
        out.append(ch)
        start_map.append(start)
        end_map.append(end)

    while i < n:
        b = raw[i]
        if b == 0x3C:  # '<'
            if raw[i : i + 4] == b"<!--":
                close = raw.find(b"-->", i + 4)
                tag_end = n if close < 0 else close + 3
                name = b""
            else:
                close = raw.find(b">", i + 1)
                tag_end = n if close < 0 else close + 1
                inner = raw[i + 1 : tag_end - 1].lstrip(b"/")
                name = inner.split()[0].lower() if inner.split() else b""
            if name.decode("ascii", "replace") in BLOCK_TAGS:
                emit(" ", i, tag_end - 1)
            i = tag_end
        elif b == 0x26:  # '&'
            ent = _decode_entity(raw[i : i + 12])
            if ent:
                ch, length = ent
                if ch == "\xa0":
                    ch = " "
                if ch != "\xad":  # drop soft hyphens
                    emit(ch, i, i + length - 1)
                i += length
            else:
                emit("&", i, i)
                i += 1
        else:
            if encoding == "utf-8":
                length = _utf8_char_len(b)
                ch = raw[i : i + length].decode("utf-8", "replace")
                if len(ch) != 1:
                    ch = "�"
            else:
                length = 1
                ch = raw[i : i + 1].decode(encoding, "replace")
            if ch == "\xa0":
                ch = " "
            if ch != "\xad":
                emit(ch, i, i + length - 1)
            i += length

    return "".join(out), start_map, end_map

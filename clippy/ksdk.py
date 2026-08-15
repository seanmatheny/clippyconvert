"""Access to the Mac App Store Kindle app's (com.amazon.Lassen) databases.

Annotations live in ksdk_annotation_v1.db: `server_view` holds cloud-synced
truth, `local_edit` is the app's pending-upload queue. We write new
annotations into `local_edit` shaped exactly like the app's own edits (codes
learned by `books2kindle.py capture`), so the app legitimately uploads them
to Amazon and they become durable, device-visible highlights.
"""

import datetime
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

LASSEN_DATA = os.path.expanduser("~/Library/Containers/com.amazon.Lassen/Data")
BOOKDATA_DB = f"{LASSEN_DATA}/Library/Protected/BookData.sqlite"

ANNOTATION_TABLES = (
    "server_view",
    "staging_server_view",
    "local_edit",
    "nonsyncable_annotations",
    "book_state",
    "delta_sync_tokens",
    "key_value_storage",
)

DATASET_HIGHLIGHT = 1
DATASET_BOOKMARK = 2
DATASET_NOTE = 3
DATASET_UNDERLINE = 12


def find_ksdk_db() -> str:
    hits = glob.glob(f"{LASSEN_DATA}/Library/KSDK/amzn1.account.*/ksdk_annotation_v1.db")
    if not hits:
        raise FileNotFoundError("ksdk_annotation_v1.db not found — is the Kindle app installed and signed in?")
    if len(hits) > 1:
        raise RuntimeError(f"multiple Kindle accounts found: {hits}")
    return hits[0]


@dataclass
class KindleBook:
    bookid: str  # ASIN or PDOC guid
    title: str
    author: str  # usually empty — ZDISPLAYAUTHOR blob is not parseable
    path: str  # absolute path to the book file/dir
    mime: str
    guid: str  # ZPERASINGUID, used verbatim in dataset_id and payloads
    maxpos: Optional[int]  # ZRAWMAXPOSITION == decompressed textLength

    @property
    def content_type(self) -> str:
        return "EBOK" if len(self.bookid) == 10 else "PDOC"

    @property
    def dataset_id(self) -> str:
        return f"{self.bookid}-{self.content_type}-{self.guid}-1"

    @property
    def is_mobi(self) -> bool:
        return "mobipocket" in (self.mime or "")

    @property
    def is_kfx(self) -> bool:
        return "kfx" in (self.mime or "")


def load_kindle_books() -> list:
    con = sqlite3.connect(f"file:{BOOKDATA_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ZBOOKID, ZDISPLAYTITLE, ZPATH, ZMIMETYPE, ZPERASINGUID, ZRAWMAXPOSITION "
        "FROM ZBOOK WHERE ZDISPLAYTITLE IS NOT NULL AND ZPATH IS NOT NULL"
    ).fetchall()
    con.close()
    books = []
    for bookid, title, path, mime, guid, maxpos in rows:
        if bookid.startswith("A:"):
            bookid = bookid[2:]
        if bookid.endswith("-0"):
            bookid = bookid[:-2]
        books.append(
            KindleBook(
                bookid=bookid,
                title=title,
                author="",
                path=os.path.join(LASSEN_DATA, path),
                mime=mime or "",
                guid=guid or "",
                maxpos=maxpos,
            )
        )
    return books


@dataclass
class KindleAnnotation:
    annotation_id: str
    dataset: int
    dataset_id: str
    start: int
    end: int
    payload: dict
    source: str  # server_view / local_edit / nonsyncable_annotations


def load_existing_annotations(db_path: str, dataset_id: str) -> list:
    """All annotations the app knows about for one book, across the synced
    view, the pending-upload queue and the non-syncable store."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = []
    for table, where, args in (
        ("server_view", "dataset_id=?", (dataset_id,)),
        ("local_edit", "dataset_id=?", (dataset_id,)),
        ("nonsyncable_annotations", "book_id=?", (dataset_id.split("-")[0],)),
    ):
        for ann_id, dataset, payload in con.execute(
            f"SELECT annotation_id, dataset, serialized_payload FROM {table} WHERE {where}", args
        ):
            try:
                p = json.loads(payload)
                start = p["start_position"]["shortPosition"]
                end = p["end_position"]["shortPosition"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            out.append(KindleAnnotation(ann_id, dataset, dataset_id, start, end, p, table))
    con.close()
    return out


def snapshot_tables(db_path: str) -> dict:
    """{table: [row tuples]} for every annotation table — used by capture to
    diff the app's writes."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    snap = {}
    for t in ANNOTATION_TABLES:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        snap[t] = {"columns": cols, "rows": con.execute(f"SELECT * FROM {t}").fetchall()}
    con.close()
    return snap


# ---- payloads ----------------------------------------------------------

def _payload(book: KindleBook, ann_type: str, start: int, end: int,
             metadata: dict, created_ms: int) -> str:
    p = {
        "book_data": {
            "asin": book.bookid,
            "contentType": book.content_type,
            "guid": book.guid,
            "isOwnedByCustomer": 1,
            "isSample": 0,
        },
        "created_time": created_ms,
        "end_position": {"longPosition": "", "shortPosition": end},
        "json_metadata": json.dumps(metadata, separators=(",", ":")),
        "last_modified": created_ms,
        "position_type": 0,
        "start_position": {"longPosition": "", "shortPosition": start},
        "type": ann_type,
    }
    return json.dumps(p, sort_keys=True, separators=(",", ":"))


def make_highlight(book: KindleBook, start: int, end: int, color: str, created_ms: int,
                   underline: bool = False):
    """(annotation_id, dataset, payload_json) for a highlight/underline."""
    if underline:
        return (f"kindle.underline-{start}", DATASET_UNDERLINE,
                _payload(book, "UNDERLINE", start, end, {"mchl_color": color}, created_ms))
    return (f"kindle.highlight-{start}", DATASET_HIGHLIGHT,
            _payload(book, "HIGHLIGHT", start, end, {"mchl_color": color}, created_ms))


def make_note(book: KindleBook, pos: int, text: str, created_ms: int):
    return (f"kindle.note-{pos}", DATASET_NOTE,
            _payload(book, "NOTE", pos, pos, {"note_text": text}, created_ms))


# ---- capture codes -----------------------------------------------------

def capture_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ksdk_capture.json")


def load_capture() -> dict:
    path = capture_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            "ksdk_capture.json not found — run `books2kindle.py capture` first "
            "to learn the app's local_edit record format"
        )
    with open(path) as f:
        return json.load(f)


# ---- writing -----------------------------------------------------------

def quit_kindle(db_path: str):
    subprocess.run(["osascript", "-e", 'tell application "Amazon Kindle" to quit'],
                   capture_output=True)
    for _ in range(20):
        if subprocess.run(["pgrep", "-x", "Amazon Kindle"], capture_output=True).returncode != 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Kindle app still running — quit it manually and retry")
    if os.path.exists(db_path + "-journal"):
        raise RuntimeError(f"hot journal at {db_path}-journal — refusing to write")


def backup_db(db_path: str, repo_dir: str, label: str = "") -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if label:
        stamp = f"{label}-{stamp}"
    bdir = os.path.join(repo_dir, "backups", stamp)
    os.makedirs(bdir, exist_ok=True)
    for suffix in ("", "-journal"):
        src = db_path + suffix
        if os.path.exists(src):
            shutil.copy2(src, bdir)
    return bdir


def insert_local_edits(db_path: str, rows: list, capture: dict) -> int:
    """rows: [(annotation_id, dataset, dataset_id, payload_json, created_ms)].
    Column codes (action, dirty_flag, ...) come verbatim from the capture."""
    codes = capture["create"]
    con = sqlite3.connect(db_path)
    # fail loudly if an app update changed the schema
    cols = {r[1] for r in con.execute("PRAGMA table_info(local_edit)")}
    expected = {"annotation_id", "action", "dataset", "dataset_id", "serialized_payload",
                "dirty_flag", "created_time", "modified_time", "retry_count",
                "expiration_timestamp", "sync_behavior"}
    if not expected <= cols:
        con.close()
        raise RuntimeError(f"local_edit schema changed: missing {expected - cols}")
    n = 0
    for ann_id, dataset, dataset_id, payload, created_ms in rows:
        con.execute(
            "INSERT INTO local_edit (annotation_id, action, dataset, dataset_id, "
            "serialized_payload, dirty_flag, created_time, modified_time, retry_count, "
            "expiration_timestamp, sync_behavior) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ann_id, codes["action"], dataset, dataset_id, payload, codes["dirty_flag"],
             created_ms, created_ms, codes.get("retry_count", 0),
             codes.get("expiration_timestamp"), codes.get("sync_behavior", 0)),
        )
        n += 1
    con.commit()
    con.close()
    return n

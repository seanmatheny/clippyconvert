"""Ask the Mac Kindle app to download books it is holding only in the cloud.

The app syncs annotations per *downloaded* book: a book it has not fetched has
no rows in ksdk_annotation_v1.db at all, and its highlights could not be turned
back into text anyway (that needs the book file). So an undownloaded book is
invisible to kindle2books until the app fetches it.

The app registers a `kindle://` URL scheme, and its library deep-link handler
(DeepLinkLibraryActionHandler in the app binary) takes `action=download` with
an `asin` — which is how we nudge it. Personal documents (PDOC) carry a guid
rather than a real ASIN and the handler does not accept them, so those must
still be downloaded by hand in the app.
"""

import subprocess
import time

from clippy import ksdk

DOWNLOAD_URL = "kindle://library?action=download&asin={asin}"


def can_request(book) -> bool:
    """Only store books (10-character ASINs) can be asked for by deep link."""
    return book.content_type == "EBOK"


def request_download(book) -> bool:
    """Ask the Kindle app to fetch one book. Returns False for books the deep
    link cannot address. Launches the app if it is not already running."""
    if not can_request(book):
        return False
    subprocess.run(["open", "-g", DOWNLOAD_URL.format(asin=book.bookid)],
                   capture_output=True)
    return True


def wait_for_downloads(bookids, timeout=600, poll=5, progress=None):
    """Poll the app's library until the requested books have files on disk.

    Returns (arrived, still_missing) as lists of KindleBook, re-read from
    BookData.sqlite so paths reflect whatever the app has just written."""
    wanted = set(bookids)
    deadline = time.time() + timeout
    while True:
        books = {b.bookid: b for b in ksdk.load_kindle_books() if b.bookid in wanted}
        arrived = [b for b in books.values() if b.downloaded]
        missing = sorted(wanted - {b.bookid for b in arrived})
        if not missing or time.time() >= deadline:
            return arrived, [books[i] for i in missing if i in books]
        if progress:
            progress(len(arrived), len(wanted), int(deadline - time.time()))
        time.sleep(poll)

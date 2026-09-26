"""Preflight check that macOS lets us read other apps' sandbox containers.

Both scripts read the Kindle app's and Apple Books' databases straight out of
~/Library/Containers. macOS's app-data protection blocks that unless the
program running Python (Terminal, iTerm, an IDE...) has Full Disk Access, and
upgrades can revoke or newly enforce it. Without this check the failure
surfaces as a baffling "sqlite3.OperationalError: unable to open database
file" (or an empty glob that looks like the app isn't installed).
"""

import os
import sys

HOME = os.path.expanduser("~")
KINDLE_CONTAINER = f"{HOME}/Library/Containers/com.amazon.Lassen/Data"
BOOKS_CONTAINER = f"{HOME}/Library/Containers/com.apple.iBooksX/Data"


def require_container_access(*containers: str) -> None:
    """Exit with an actionable message if any container can't be listed.

    A container that doesn't exist at all is left for the caller to report
    (the app may simply not be installed)."""
    blocked = []
    for path in containers:
        try:
            os.listdir(path)
        except FileNotFoundError:
            continue
        except PermissionError:
            blocked.append(path)
    if not blocked:
        return
    host = os.environ.get("TERM_PROGRAM") or "the app running this script"
    sys.exit(
        "error: macOS is blocking access to another app's data:\n"
        + "".join(f"  {p}\n" for p in blocked)
        + f"\nGrant Full Disk Access to {host} in System Settings > Privacy & Security >\n"
        "Full Disk Access (toggle it off and on again if it is already listed, which\n"
        "macOS upgrades sometimes require), then quit and reopen it and rerun."
    )

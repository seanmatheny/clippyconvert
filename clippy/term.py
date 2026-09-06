"""Minimal ANSI colouring for the reports both scripts print.

Colour is used sparingly — only to make the books that gained *new*
annotations stand out from the many that are already fully in sync. Honours
NO_COLOR / FORCE_COLOR and stays out of the way when stdout is a pipe.
"""

import os
import sys

GREEN = "\x1b[32m"
RESET = "\x1b[0m"


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def green(text: str) -> str:
    return f"{GREEN}{text}{RESET}" if color_enabled() else text


def maybe_green(text: str, highlight: bool) -> str:
    """green(text) when `highlight`, otherwise the text unchanged."""
    return green(text) if highlight else text

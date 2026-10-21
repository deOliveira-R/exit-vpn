"""Reading an HTML document without a browser (the HTTP engine's page text and title)."""

from __future__ import annotations

import html as _html
import re


def visible_text(doc: str) -> str:
    """Rough visible text: scripts, styles, templates and comments removed."""
    h = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", doc)
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", h))).strip()


def title_of(doc: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", doc)
    return re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip() if m else ""

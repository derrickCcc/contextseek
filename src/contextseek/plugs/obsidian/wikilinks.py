"""Parse Obsidian ``[[wikilinks]]`` and resolve them to typed ``Link`` edges.

Obsidian wikilink syntax::

    [[PageName]]           — link to ``PageName``
    [[PageName|Alias]]     — link to ``PageName`` with display text ``Alias``
    [[PageName#Heading]]   — link to a heading within ``PageName``

This module extracts the **target page name** (stripping aliases and
heading anchors) so it can be matched against the file-name → item-id
mapping built during sync.
"""

from __future__ import annotations

import re

# Matches [[Target]] or [[Target|Alias]] or [[Target#Heading]] or
# [[Target#Heading|Alias]] — captures the target name in group 1.
WIKILINK_RE = re.compile(
    r"\[\["
    r"([^\]|#]+)"           # target page name (no ], |, or #)
    r"(?:#[^\]|]*)?"        # optional #heading anchor
    r"(?:\|[^\]]*)?"        # optional |alias
    r"\]\]"
)


def extract_wikilinks(content: str) -> list[str]:
    """Return a list of target page names from all ``[[wikilinks]]`` in *content*.

    Aliases and heading anchors are stripped — only the page name is kept.
    Duplicates are removed while preserving first-seen order.
    """
    seen: set[str] = set()
    results: list[str] = []
    for match in WIKILINK_RE.finditer(content):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            results.append(target)
    return results


def resolve_wikilink_targets(
    content: str,
    name_to_item_id: dict[str, str],
) -> list[str]:
    """Resolve ``[[wikilinks]]`` in *content* to target ContextItem IDs.

    Only wikilinks whose target page name exists in *name_to_item_id*
    are returned.  The returned list contains **item IDs** (not page
    names), with duplicates removed.
    """
    targets = extract_wikilinks(content)
    seen: set[str] = set()
    item_ids: list[str] = []
    for target in targets:
        item_id = name_to_item_id.get(target)
        if item_id and item_id not in seen:
            seen.add(item_id)
            item_ids.append(item_id)
    return item_ids
"""Incremental Obsidian vault DataPlug.

Syncs an Obsidian vault (Markdown + wikilinks) into ContextSeek with
three key capabilities:

1. **Incremental sync** — tracks mtime + content hash per file so
   unchanged files are skipped on re-sync.
2. **Wikilink → Link** — resolves ``[[wikilinks]]`` to typed
   ``Link(relation=LinkType.related_to)`` edges between ContextItems.
3. **Delete handling** — soft-deletes ContextItems whose source files
   have vanished from the vault.

Usage::

    from contextseek.plugs import ObsidianVaultPlug

    plug = ObsidianVaultPlug(vault_path=Path("/path/to/vault"), scope="obsidian/vault")
    ctx.plug(plug, scope="obsidian/vault")
    links = plug.resolve_links(ctx)
    deletes = plug.handle_deletes(ctx)
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator

from contextseek.domain.links import Link, LinkType
from contextseek.plugs.core.protocols import PlugMeta, RawEvent
from contextseek.plugs.obsidian.sync_state import FileSyncState, SyncStateStore
from contextseek.plugs.obsidian.wikilinks import resolve_wikilink_targets

if TYPE_CHECKING:
    from contextseek.client.contextseek import ContextSeek

# Directories to skip when scanning the vault.
_IGNORED_DIR_NAMES: set[str] = {
    ".obsidian",
    ".contextseek",
    ".git",
    ".trash",
    "__pycache__",
    "node_modules",
}

# YAML front-matter pattern: --- \n key: value \n ---
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
# Tags line in front-matter: tags: [a, b] or tags:\n - a\n - b
_FRONTMATTER_TAGS_RE = re.compile(
    r"^tags:\s*(?:\[([^\]]*)\]|(.+))$", re.MULTILINE
)


def _file_hash(p: pathlib.Path) -> str:
    """Streamed SHA256 of a file's bytes."""
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_frontmatter_tags(text: str) -> list[str]:
    """Extract tags from YAML front-matter (if present)."""
    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        return []
    fm_text = fm_match.group(1)

    # List-style: tags:\n  - a\n  - b
    list_match = re.search(r"^tags:\s*\n((?:\s*-\s+.+\n?)+)", fm_text, re.MULTILINE)
    if list_match:
        items = re.findall(r"^\s*-\s+(.+)$", list_match.group(1), re.MULTILINE)
        return [item.strip().strip('"').strip("'") for item in items if item.strip()]

    # Bracket-style: tags: [a, b, c]
    bracket_match = _FRONTMATTER_TAGS_RE.search(fm_text)
    if bracket_match:
        bracket_content = bracket_match.group(1)
        if bracket_content is not None:
            return [
                t.strip().strip('"').strip("'")
                for t in bracket_content.split(",")
                if t.strip()
            ]
        # Inline-style: tags: a b c  (space-separated)
        inline = bracket_match.group(2)
        if inline and not inline.startswith("-"):
            return [t.strip() for t in inline.split() if t.strip()]
    return []


def _strip_frontmatter(text: str) -> str:
    """Remove YAML front-matter from markdown text."""
    return _FRONTMATTER_RE.sub("", text, count=1)


def _iter_markdown_files(vault_path: pathlib.Path) -> list[pathlib.Path]:
    """Return all .md files under vault, skipping config/cache directories."""
    import os

    files: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(vault_path):
        dirnames[:] = [
            name for name in dirnames if name not in _IGNORED_DIR_NAMES
        ]
        base = pathlib.Path(dirpath)
        for filename in filenames:
            fp = base / filename
            if fp.suffix.lower() == ".md":
                files.append(fp)
    return sorted(files)


@dataclass
class ObsidianVaultPlug:
    """DataPlug for incremental Obsidian vault sync.

    Attributes:
        vault_path: Path to the Obsidian vault root.
        scope: Destination scope for imported items.
        source_name: Plug name for PlugMeta.
    """

    vault_path: pathlib.Path
    scope: str = "obsidian/vault"
    source_name: str = "obsidian_vault"

    # Runtime state populated during stream()
    _added: int = field(default=0, init=False, repr=False)
    _skipped: int = field(default=0, init=False, repr=False)
    _vanished_files: list[str] = field(default_factory=list, init=False, repr=False)
    _state_store: SyncStateStore = field(init=False, repr=False)
    _new_states: dict[str, FileSyncState] = field(
        default_factory=dict, init=False, repr=False
    )
    _stream_meta: dict[str, tuple[str, float]] = field(
        default_factory=dict, init=False, repr=False
    )
    _old_states: dict[str, FileSyncState] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.vault_path = pathlib.Path(self.vault_path)
        state_path = self.vault_path / ".contextseek" / "sync_state.json"
        self._state_store = SyncStateStore(state_path)

    # ------------------------------------------------------------------
    # DataPlug protocol
    # ------------------------------------------------------------------

    def stream(self) -> Iterator[RawEvent]:
        """Yield RawEvents for new/modified .md files (incremental)."""
        old_states = self._state_store.load()
        self._old_states = dict(old_states)
        current_files = _iter_markdown_files(self.vault_path)
        current_paths = {f.as_posix() for f in current_files}

        self._vanished_files = sorted(set(old_states.keys()) - current_paths)
        self._added = 0
        self._skipped = 0

        for fp in current_files:
            key = fp.as_posix()
            try:
                mtime = fp.stat().st_mtime
            except OSError:
                continue

            old = old_states.get(key)

            # mtime fast-path
            if old is not None and abs(old.mtime - mtime) < 1e-6:
                self._skipped += 1
                self._new_states[key] = old
                continue

            # SHA256 authoritative check
            fhash = _file_hash(fp)
            if old is not None and old.content_hash == fhash:
                # mtime changed but content didn't — refresh mtime, skip
                updated = FileSyncState(
                    mtime=mtime, content_hash=fhash, context_item_id=old.context_item_id
                )
                self._new_states[key] = updated
                self._skipped += 1
                continue

            # Read and parse the file
            try:
                raw_text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            tags = _extract_frontmatter_tags(raw_text)
            tags = list(tags) + ["obsidian"]

            yield RawEvent(
                content=raw_text,
                source=key,
                tags=tags,
                metadata={"_file_stem": fp.stem, "_content_hash": fhash, "_mtime": mtime},
            )
            self._stream_meta[key] = (fhash, mtime)
            self._added += 1

    def metadata(self) -> PlugMeta:
        """Return plug metadata."""
        return PlugMeta(
            name=self.source_name,
            source_type="document",
            description="Incremental Obsidian vault sync (Markdown + wikilinks)",
        )

    # ------------------------------------------------------------------
    # Post-sync operations (called after ctx.plug())
    # ------------------------------------------------------------------

    def record_item_id(self, file_path: str, item_id: str) -> None:
        """Record the ContextItem ID assigned to a synced file.

        Called by the CLI handler after each ``ctx.add()`` succeeds so
        that ``resolve_links()`` and ``handle_deletes()`` can use it.
        """
        # Check if we already have stream metadata for this file
        # (from the RawEvent we yielded in stream())
        cached = self._stream_meta.get(file_path)
        if cached is not None:
            self._new_states[file_path] = FileSyncState(
                mtime=cached[1], content_hash=cached[0], context_item_id=item_id
            )
            return

        # Fallback: compute from disk
        p = pathlib.Path(file_path)
        try:
            mtime = p.stat().st_mtime
            fhash = _file_hash(p)
        except OSError:
            mtime = 0.0
            fhash = ""
        self._new_states[file_path] = FileSyncState(
            mtime=mtime, content_hash=fhash, context_item_id=item_id
        )

    def persist_state(self) -> None:
        """Save the sync cursor to disk.  Call after sync completes."""
        self._state_store.save(self._new_states)

    def resolve_links(self, ctx: "ContextSeek") -> int:
        """Post-sync pass: resolve ``[[wikilinks]]`` to typed Link edges.

        Builds a ``{file_stem: item_id}`` mapping from the sync state,
        then walks every imported item, extracts wikilinks from its
        content, and appends ``Link(relation=LinkType.related_to)``
        edges to matching targets.

        Returns the number of links created.
        """
        states = self._new_states or self._state_store.load()
        if not states:
            return 0

        # Build stem → item_id mapping
        stem_to_id: dict[str, str] = {}
        for file_path, state in states.items():
            stem = pathlib.Path(file_path).stem
            stem_to_id[stem] = state.context_item_id

        links_created = 0
        for file_path, state in states.items():
            item_id = state.context_item_id
            ref = ctx.resolver.ref_for(self.scope, item_id)
            payload = ctx.adapter.read(ref)
            if payload is None:
                continue

            from contextseek.domain.serialization import deserialize_context_item

            item = deserialize_context_item(payload)
            content = item.content_text

            target_ids = resolve_wikilink_targets(content, stem_to_id)
            existing_targets = {link.target_id for link in item.links}

            changed = False
            for target_id in target_ids:
                if target_id == item_id or target_id in existing_targets:
                    continue
                item.links.append(
                    Link(
                        target_id=target_id,
                        relation=LinkType.related_to,
                        strength=0.8,
                    )
                )
                links_created += 1
                changed = True

            if changed:
                ctx._write_item(item)

        return links_created

    def handle_deletes(self, ctx: "ContextSeek") -> int:
        """Soft-delete ContextItems whose source files vanished from the vault.

        Returns the number of items soft-deleted.
        """
        if not self._vanished_files:
            return 0

        # Use old_states captured during stream() — not the freshly
        # saved state (which no longer contains the vanished files).
        old_states = self._old_states or self._state_store.load()
        deleted = 0
        for file_path in self._vanished_files:
            old = old_states.get(file_path)
            if not old or not old.context_item_id:
                continue
            ref = ctx.resolver.ref_for(self.scope, old.context_item_id)
            try:
                ctx.forget(
                    ref,
                    scope=self.scope,
                    reason=f"source file deleted: {file_path}",
                    propagate=False,
                )
                deleted += 1
            except ValueError:
                # Already deleted or not found — skip
                pass
        return deleted

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def added(self) -> int:
        return self._added

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def vanished_files(self) -> list[str]:
        return self._vanished_files
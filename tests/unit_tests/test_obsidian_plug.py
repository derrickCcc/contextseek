"""Unit tests for Obsidian vault DataPlug components."""

from __future__ import annotations

import pathlib

import pytest

from contextseek.plugs.obsidian.sync_state import FileSyncState, SyncStateStore
from contextseek.plugs.obsidian.wikilinks import (
    WIKILINK_RE,
    extract_wikilinks,
    resolve_wikilink_targets,
)
from contextseek.plugs.obsidian.plug import (
    ObsidianVaultPlug,
    _extract_frontmatter_tags,
    _iter_markdown_files,
)


# ─── SyncStateStore ─────────────────────────────────────────────────


class TestSyncStateStore:
    def test_load_missing_file_returns_empty(self, tmp_path: pathlib.Path) -> None:
        store = SyncStateStore(tmp_path / "sync_state.json")
        assert store.load() == {}

    def test_save_then_load_roundtrip(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / ".contextseek" / "sync_state.json"
        store = SyncStateStore(path)
        states = {
            "vault/PageA.md": FileSyncState(mtime=1000.0, content_hash="abc", context_item_id="id-a"),
            "vault/PageB.md": FileSyncState(mtime=2000.0, content_hash="def", context_item_id="id-b"),
        }
        store.save(states)
        loaded = store.load()
        assert loaded == states

    def test_all_paths(self, tmp_path: pathlib.Path) -> None:
        store = SyncStateStore(tmp_path / "sync_state.json")
        states = {
            "a.md": FileSyncState(mtime=1.0, content_hash="x", context_item_id="1"),
            "b.md": FileSyncState(mtime=2.0, content_hash="y", context_item_id="2"),
        }
        store.save(states)
        assert store.all_paths() == {"a.md", "b.md"}


# ─── Wikilink parsing ───────────────────────────────────────────────


class TestExtractWikilinks:
    def test_simple_wikilink(self) -> None:
        assert extract_wikilinks("See [[PageA]] for details") == ["PageA"]

    def test_aliased_wikilink(self) -> None:
        assert extract_wikilinks("See [[PageA|Custom Alias]]") == ["PageA"]

    def test_heading_anchor(self) -> None:
        assert extract_wikilinks("[[PageA#Section]]") == ["PageA"]

    def test_heading_with_alias(self) -> None:
        assert extract_wikilinks("[[PageA#Section|Alias]]") == ["PageA"]

    def test_multiple_wikilinks(self) -> None:
        content = "[[PageA]] and [[PageB|B]] and [[PageC#Head]]"
        assert extract_wikilinks(content) == ["PageA", "PageB", "PageC"]

    def test_dedup_preserves_order(self) -> None:
        content = "[[PageA]] [[PageB]] [[PageA]]"
        assert extract_wikilinks(content) == ["PageA", "PageB"]

    def test_no_wikilinks(self) -> None:
        assert extract_wikilinks("plain text without links") == []

    def test_regex_pattern(self) -> None:
        assert WIKILINK_RE.search("text [[Target]] more") is not None


class TestResolveWikilinkTargets:
    def test_resolves_known_targets(self) -> None:
        mapping = {"PageA": "id-a", "PageB": "id-b"}
        content = "[[PageA]] references [[PageB]]"
        assert resolve_wikilink_targets(content, mapping) == ["id-a", "id-b"]

    def test_skips_unknown_targets(self) -> None:
        mapping = {"PageA": "id-a"}
        content = "[[PageA]] and [[UnknownPage]]"
        assert resolve_wikilink_targets(content, mapping) == ["id-a"]

    def test_dedup_item_ids(self) -> None:
        mapping = {"PageA": "id-a"}
        content = "[[PageA]] [[PageA]] [[PageA]]"
        assert resolve_wikilink_targets(content, mapping) == ["id-a"]


# ─── Front-matter tag extraction ────────────────────────────────────


class TestExtractFrontmatterTags:
    def test_bracket_style(self) -> None:
        text = "---\ntitle: Test\ntags: [python, testing, ci]\n---\n\nContent"
        assert _extract_frontmatter_tags(text) == ["python", "testing", "ci"]

    def test_list_style(self) -> None:
        text = "---\ntags:\n  - python\n  - testing\n---\n\nContent"
        assert _extract_frontmatter_tags(text) == ["python", "testing"]

    def test_inline_style(self) -> None:
        text = "---\ntags: python testing\n---\n\nContent"
        assert _extract_frontmatter_tags(text) == ["python", "testing"]

    def test_no_frontmatter(self) -> None:
        assert _extract_frontmatter_tags("plain text") == []

    def test_empty_tags(self) -> None:
        text = "---\ntitle: Test\n---\n\nContent"
        assert _extract_frontmatter_tags(text) == []


# ─── File iteration ─────────────────────────────────────────────────


class TestIterMarkdownFiles:
    def test_finds_md_files(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "PageA.md").write_text("A")
        (tmp_path / "PageB.md").write_text("B")
        (tmp_path / "readme.txt").write_text("not md")
        files = _iter_markdown_files(tmp_path)
        assert len(files) == 2
        assert all(f.suffix == ".md" for f in files)

    def test_skips_ignored_dirs(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "PageA.md").write_text("A")
        (tmp_path / ".obsidian").mkdir()
        (tmp_path / ".obsidian" / "config.md").write_text("config")
        (tmp_path / ".contextseek").mkdir()
        (tmp_path / ".contextseek" / "sync_state.json").write_text("{}")
        files = _iter_markdown_files(tmp_path)
        assert len(files) == 1
        assert files[0].name == "PageA.md"


# ─── ObsidianVaultPlug ──────────────────────────────────────────────


class TestObsidianVaultPlug:
    def test_metadata(self, tmp_path: pathlib.Path) -> None:
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        meta = plug.metadata()
        assert meta.name == "obsidian_vault"
        assert meta.source_type == "document"
        assert "Obsidian" in meta.description

    def test_stream_yields_new_files(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "PageA.md").write_text("# Page A\n\nContent of A")
        (tmp_path / "PageB.md").write_text("# Page B\n\nContent of B")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        assert len(events) == 2
        assert plug.added == 2
        assert all("obsidian" in (e.tags or []) for e in events)

    def test_stream_skips_unchanged(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "PageA.md").write_text("# Page A\n\nContent")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        # First sync
        events = list(plug.stream())
        assert len(events) == 1
        plug.record_item_id(events[0].source, "fake-id")
        plug.persist_state()

        # Second sync — no changes
        plug2 = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events2 = list(plug2.stream())
        assert len(events2) == 0
        assert plug2.skipped == 1
        assert plug2.added == 0

    def test_stream_detects_vanished(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "PageA.md").write_text("A")
        (tmp_path / "PageB.md").write_text("B")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        plug.record_item_id(events[0].source, "id-a")
        plug.record_item_id(events[1].source, "id-b")
        plug.persist_state()

        # Delete PageB
        (tmp_path / "PageB.md").unlink()
        plug2 = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        list(plug2.stream())
        assert any("PageB" in v for v in plug2.vanished_files)

    def test_frontmatter_tags_in_event(self, tmp_path: pathlib.Path) -> None:
        (tmp_path / "PageA.md").write_text(
            "---\ntags: [python, web]\n---\n\n# Page A\n\nContent"
        )
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        assert len(events) == 1
        tags = events[0].tags or []
        assert "python" in tags
        assert "web" in tags
        assert "obsidian" in tags

    def test_stream_empty_vault(self, tmp_path: pathlib.Path) -> None:
        """An empty vault yields no events."""
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        assert len(events) == 0
        assert plug.added == 0
        assert plug.skipped == 0

    def test_mtime_changed_content_same_skips(self, tmp_path: pathlib.Path) -> None:
        """mtime changed but content hash is the same → file is skipped."""
        import time as _time

        (tmp_path / "PageA.md").write_text("unchanged content")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        list(plug.stream())
        plug.record_item_id((tmp_path / "PageA.md").as_posix(), "id-a")
        plug.persist_state()

        # Touch the file to change mtime without changing content
        _time.sleep(0.02)
        p = tmp_path / "PageA.md"
        with p.open("a") as f:
            f.flush()
        import os
        os.utime(p, None)

        plug2 = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug2.stream())
        assert len(events) == 0
        assert plug2.skipped == 1
        assert plug2.added == 0

    def test_nested_directories_scanned(self, tmp_path: pathlib.Path) -> None:
        """Markdown files in nested directories are found."""
        (tmp_path / "subdir").mkdir()
        (tmp_path / "PageA.md").write_text("A")
        (tmp_path / "subdir" / "PageB.md").write_text("B")
        (tmp_path / "subdir" / "nested").mkdir()
        (tmp_path / "subdir" / "nested" / "PageC.md").write_text("C")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        assert len(events) == 3

    def test_new_file_on_second_sync(self, tmp_path: pathlib.Path) -> None:
        """Adding a file between syncs produces exactly one new event."""
        (tmp_path / "PageA.md").write_text("A")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        list(plug.stream())
        plug.record_item_id((tmp_path / "PageA.md").as_posix(), "id-a")
        plug.persist_state()

        (tmp_path / "PageB.md").write_text("B")
        plug2 = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug2.stream())
        assert len(events) == 1
        assert plug2.added == 1
        assert plug2.skipped == 1
        assert "PageB" in events[0].source

    def test_wikilink_to_nonexistent_page(self, tmp_path: pathlib.Path) -> None:
        """A [[wikilink]] to a page that doesn't exist in the vault
        should not cause an error — it's simply not resolved."""
        (tmp_path / "PageA.md").write_text("[[NonExistent]] and [[PageB]]")
        (tmp_path / "PageB.md").write_text("B")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        assert len(events) == 2

    def test_obsidian_config_dir_skipped(self, tmp_path: pathlib.Path) -> None:
        """The .obsidian config directory should not be scanned."""
        (tmp_path / "PageA.md").write_text("A")
        (tmp_path / ".obsidian").mkdir()
        (tmp_path / ".obsidian" / "workspace.json").write_text("{}")
        (tmp_path / ".obsidian" / "graph.json").write_text("{}")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        assert len(events) == 1

    def test_non_md_files_ignored(self, tmp_path: pathlib.Path) -> None:
        """Only .md files are picked up — .txt, .json, images etc. are ignored."""
        (tmp_path / "PageA.md").write_text("A")
        (tmp_path / "notes.txt").write_text("text")
        (tmp_path / "config.json").write_text("{}")
        (tmp_path / "image.png").write_bytes(b"\x89PNG")
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        assert len(events) == 1
        assert "PageA" in events[0].source

    def test_resolve_links_idempotent(self, tmp_path: pathlib.Path) -> None:
        """Calling resolve_links twice should not create duplicate links."""
        from contextseek import ContextSeek

        (tmp_path / "PageA.md").write_text("[[PageB]]")
        (tmp_path / "PageB.md").write_text("[[PageA]]")
        ctx = ContextSeek()
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        for event in plug.stream():
            item = ctx.add(event.content, scope="test/vault", source=event.source,
                          source_type="document", tags=event.tags, check_conflicts=False)
            plug.record_item_id(event.source, item.id)
        plug.persist_state()

        links1 = plug.resolve_links(ctx)
        links2 = plug.resolve_links(ctx)
        assert links1 > 0
        assert links2 == 0  # No new links on second call

    def test_handle_deletes_no_vanished(self, tmp_path: pathlib.Path) -> None:
        """handle_deletes returns 0 when no files have vanished."""
        from contextseek import ContextSeek

        (tmp_path / "PageA.md").write_text("A")
        ctx = ContextSeek()
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        list(plug.stream())
        assert plug.handle_deletes(ctx) == 0

    def test_handle_deletes_already_deleted(self, tmp_path: pathlib.Path) -> None:
        """handle_deletes silently skips items that are already deleted."""
        from contextseek import ContextSeek

        (tmp_path / "PageA.md").write_text("A")
        (tmp_path / "PageB.md").write_text("B")
        ctx = ContextSeek()
        plug = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        events = list(plug.stream())
        for e in events:
            item = ctx.add(e.content, scope="test/vault", source=e.source,
                          source_type="document", tags=e.tags, check_conflicts=False)
            plug.record_item_id(e.source, item.id)
        plug.persist_state()

        # Delete both files
        (tmp_path / "PageA.md").unlink()
        (tmp_path / "PageB.md").unlink()

        plug2 = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        list(plug2.stream())
        first_deletes = plug2.handle_deletes(ctx)
        assert first_deletes == 2

        # Second call should return 0 (already deleted)
        plug3 = ObsidianVaultPlug(vault_path=tmp_path, scope="test/vault")
        list(plug3.stream())
        second_deletes = plug3.handle_deletes(ctx)
        assert second_deletes == 0
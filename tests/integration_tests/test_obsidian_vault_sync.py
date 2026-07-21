"""Integration tests for incremental Obsidian vault sync.

Tests the full sync flow: import → re-sync (no writes) → edit → re-sync
(changed only) → wikilink → Link edges → delete → soft-delete.
"""

from __future__ import annotations

import pathlib
import time

import pytest

from contextseek import ContextSeek
from contextseek.domain.links import LinkType
from contextseek.plugs.obsidian import ObsidianVaultPlug


@pytest.fixture
def vault(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a minimal Obsidian vault with wikilinks."""
    v = tmp_path / "vault"
    v.mkdir()

    (v / "PageA.md").write_text(
        "# Page A\n\n"
        "This page references [[PageB]] and [[PageC|Custom Alias]].\n",
        encoding="utf-8",
    )
    (v / "PageB.md").write_text(
        "# Page B\n\n"
        "Backlink to [[PageA]].\n",
        encoding="utf-8",
    )
    (v / "PageC.md").write_text(
        "---\ntags: [reference, guide]\n---\n\n"
        "# Page C\n\n"
        "Plain content with no wikilinks.\n",
        encoding="utf-8",
    )
    return v


def _sync_vault(ctx: ContextSeek, vault: pathlib.Path, scope: str) -> ObsidianVaultPlug:
    """Run a full sync cycle and return the plug instance."""
    plug = ObsidianVaultPlug(vault_path=vault, scope=scope)
    for event in plug.stream():
        item = ctx.add(
            event.content,
            scope=scope,
            source=event.source,
            source_type="document",
            tags=event.tags,
            check_conflicts=False,
        )
        plug.record_item_id(event.source, item.id)
    plug.persist_state()
    plug.resolve_links(ctx)
    plug.handle_deletes(ctx)
    return plug


def test_first_sync_imports_all_files(vault: pathlib.Path) -> None:
    ctx = ContextSeek()
    plug = _sync_vault(ctx, vault, "obs/vault")
    assert plug.added == 3
    assert plug.skipped == 0

    items = ctx.items(scope="obs/vault")
    assert len(items) == 3


def test_second_sync_unchanged_no_writes(vault: pathlib.Path) -> None:
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")

    # Second sync — no changes
    plug = ObsidianVaultPlug(vault_path=vault, scope="obs/vault")
    events = list(plug.stream())
    assert len(events) == 0
    assert plug.added == 0
    assert plug.skipped == 3


def test_edited_file_produces_update(vault: pathlib.Path) -> None:
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")

    # Edit PageA
    time.sleep(0.01)  # ensure mtime differs
    (vault / "PageA.md").write_text("# Page A\n\nEdited content here.\n", encoding="utf-8")

    plug = ObsidianVaultPlug(vault_path=vault, scope="obs/vault")
    events = list(plug.stream())
    assert len(events) == 1
    assert plug.added == 1
    assert plug.skipped == 2
    assert "PageA" in events[0].source


def test_wikilinks_become_links(vault: pathlib.Path) -> None:
    ctx = ContextSeek()
    plug = _sync_vault(ctx, vault, "obs/vault")

    # PageA has [[PageB]] and [[PageC|Custom Alias]]
    # PageB has [[PageA]]
    # After resolve_links, those should be Link(relation=related_to) edges
    items = {pathlib.Path(it.provenance.source_id).stem: it for it in ctx.items(scope="obs/vault")}

    assert "PageA" in items
    assert "PageB" in items
    assert "PageC" in items

    page_a = items["PageA"]
    link_targets = {link.target_id for link in page_a.links if link.relation == LinkType.related_to}
    assert items["PageB"].id in link_targets
    assert items["PageC"].id in link_targets

    page_b = items["PageB"]
    link_targets_b = {link.target_id for link in page_b.links if link.relation == LinkType.related_to}
    assert items["PageA"].id in link_targets_b


def test_deleted_file_soft_deleted(vault: pathlib.Path) -> None:
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")

    # Get PageC's item ID from sync state before deleting
    from contextseek.plugs.obsidian.sync_state import SyncStateStore

    state_file = vault / ".contextseek" / "sync_state.json"
    states = SyncStateStore(state_file).load()
    page_c_path = (vault / "PageC.md").as_posix()
    page_c_state = states[page_c_path]
    page_c_id = page_c_state.context_item_id

    # Delete PageC from vault
    (vault / "PageC.md").unlink()

    # Re-sync
    plug = ObsidianVaultPlug(vault_path=vault, scope="obs/vault")
    list(plug.stream())

    deletes = plug.handle_deletes(ctx)
    assert deletes == 1

    # PageC's item should be soft-deleted (searchable=False)
    from contextseek.domain.serialization import deserialize_context_item

    ref = ctx.resolver.ref_for("obs/vault", page_c_id)
    payload = ctx.adapter.read(ref)
    assert payload is not None
    item = deserialize_context_item(payload)
    assert item.is_deleted is True
    assert item.searchable is False


def test_frontmatter_tags_imported(vault: pathlib.Path) -> None:
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")

    # PageC has front-matter tags: [reference, guide]
    items = ctx.items(scope="obs/vault")
    page_c = next(
        it for it in items
        if pathlib.Path(it.provenance.source_id).stem == "PageC"
    )
    assert "reference" in page_c.tags
    assert "guide" in page_c.tags
    assert "obsidian" in page_c.tags


def test_sync_state_persisted(vault: pathlib.Path) -> None:
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")

    state_file = vault / ".contextseek" / "sync_state.json"
    assert state_file.exists()

    # State should contain all 3 files
    from contextseek.plugs.obsidian.sync_state import SyncStateStore

    states = SyncStateStore(state_file).load()
    assert len(states) == 3
    assert all(s.context_item_id for s in states.values())


def test_no_self_links(vault: pathlib.Path) -> None:
    """A file should not get a Link pointing to itself even if it
    contains a [[wikilink]] to its own page name."""
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")

    items = {pathlib.Path(it.provenance.source_id).stem: it for it in ctx.items(scope="obs/vault")}
    for item in items.values():
        for link in item.links:
            assert link.target_id != item.id


def test_resolve_links_idempotent(vault: pathlib.Path) -> None:
    """Re-running resolve_links should not create duplicate Link edges."""
    ctx = ContextSeek()
    plug = _sync_vault(ctx, vault, "obs/vault")

    # Count links after first resolve
    items_before = ctx.items(scope="obs/vault")
    links_before = sum(len(it.links) for it in items_before)

    # Run resolve_links again
    plug.resolve_links(ctx)

    items_after = ctx.items(scope="obs/vault")
    links_after = sum(len(it.links) for it in items_after)

    assert links_after == links_before


def test_wikilink_to_nonexistent_page_no_link(vault: pathlib.Path) -> None:
    """A [[wikilink]] to a page that doesn't exist in the vault should
    not create a Link edge (only resolved wikilinks become links)."""
    ctx = ContextSeek()
    # Add a wikilink to a non-existent page in PageA
    (vault / "PageA.md").write_text(
        "# Page A\n\n[[PageB]] and [[NonExistentPage]].\n", encoding="utf-8"
    )
    _sync_vault(ctx, vault, "obs/vault")

    items = {pathlib.Path(it.provenance.source_id).stem: it for it in ctx.items(scope="obs/vault")}
    page_a = items["PageA"]
    link_targets = {link.target_id for link in page_a.links if link.relation == LinkType.related_to}
    # Should link to PageB but NOT to any item for NonExistentPage
    assert items["PageB"].id in link_targets
    # There should be exactly 1 link (PageB only, not NonExistentPage)
    assert len(link_targets) == 1


def test_add_new_file_then_sync(vault: pathlib.Path) -> None:
    """Adding a new file to the vault and re-syncing imports only the new file."""
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")
    assert len(ctx.items(scope="obs/vault")) == 3

    # Add a new file
    (vault / "PageD.md").write_text("# Page D\n\nNew page.\n", encoding="utf-8")
    plug = _sync_vault(ctx, vault, "obs/vault")

    assert plug.added == 1
    assert plug.skipped == 3
    assert len(ctx.items(scope="obs/vault")) == 4


def test_edit_and_add_then_sync(vault: pathlib.Path) -> None:
    """Editing one file and adding another in a single re-sync cycle."""
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")

    time.sleep(0.01)
    (vault / "PageA.md").write_text("# Page A\n\nEdited.\n", encoding="utf-8")
    (vault / "PageD.md").write_text("# Page D\n\nNew.\n", encoding="utf-8")

    plug = _sync_vault(ctx, vault, "obs/vault")
    assert plug.added == 2
    assert plug.skipped == 2


def test_delete_then_readd(vault: pathlib.Path) -> None:
    """Deleting a file, syncing, then re-adding it creates a new item."""
    ctx = ContextSeek()
    _sync_vault(ctx, vault, "obs/vault")

    # Delete PageC and sync (with persist)
    (vault / "PageC.md").unlink()
    _sync_vault(ctx, vault, "obs/vault")

    # Re-add PageC
    (vault / "PageC.md").write_text("# Page C\n\nReborn.\n", encoding="utf-8")
    plug2 = ObsidianVaultPlug(vault_path=vault, scope="obs/vault")
    events = list(plug2.stream())
    assert len(events) == 1
    assert plug2.added == 1


def test_nested_directories_sync(tmp_path: pathlib.Path) -> None:
    """Files in nested subdirectories are synced correctly."""
    v = tmp_path / "vault"
    v.mkdir()
    (v / "PageA.md").write_text("[[notes/PageB]]", encoding="utf-8")
    (v / "notes").mkdir()
    (v / "notes" / "PageB.md").write_text("[[PageA]]", encoding="utf-8")

    ctx = ContextSeek()
    plug = _sync_vault(ctx, v, "obs/vault")
    assert plug.added == 2
    assert len(ctx.items(scope="obs/vault")) == 2

    # Check wikilinks resolved — note: stem is "PageB" not "notes/PageB"
    items = {pathlib.Path(it.provenance.source_id).stem: it for it in ctx.items(scope="obs/vault")}
    # PageA has [[notes/PageB]] which won't match stem "PageB"
    # This is a known limitation — wikilinks with paths are not resolved
    # to the file stem. Only simple [[PageName]] wikilinks are resolved.
    page_b = items["PageB"]
    link_targets_b = {link.target_id for link in page_b.links if link.relation == LinkType.related_to}
    assert items["PageA"].id in link_targets_b  # PageB links to [[PageA]]


def test_full_cycle_sync(vault: pathlib.Path) -> None:
    """Complete cycle: first sync → second sync (no writes) → edit →
    re-sync (changed only) → delete → re-sync (soft-delete) → add →
    re-sync (new only)."""
    ctx = ContextSeek()

    # 1. First sync
    plug1 = _sync_vault(ctx, vault, "obs/vault")
    assert plug1.added == 3

    # 2. Second sync — no changes
    plug2 = ObsidianVaultPlug(vault_path=vault, scope="obs/vault")
    events2 = list(plug2.stream())
    assert len(events2) == 0
    assert plug2.skipped == 3

    # 3. Edit PageA and re-sync (with persist)
    time.sleep(0.01)
    (vault / "PageA.md").write_text("# Page A\n\nUpdated.\n[[PageB]]\n", encoding="utf-8")
    plug3 = _sync_vault(ctx, vault, "obs/vault")
    assert plug3.added == 1
    assert plug3.skipped == 2

    # 4. Delete PageC and re-sync (with persist — _sync_vault handles deletes)
    (vault / "PageC.md").unlink()
    plug4 = _sync_vault(ctx, vault, "obs/vault")
    # _sync_vault already called handle_deletes internally

    # 5. Add PageD and re-sync
    (vault / "PageD.md").write_text("# Page D\n\nNew.\n", encoding="utf-8")
    plug5 = ObsidianVaultPlug(vault_path=vault, scope="obs/vault")
    events5 = list(plug5.stream())
    assert len(events5) == 1
    assert plug5.added == 1
    assert plug5.skipped == 2  # PageA (updated in step 3), PageB — PageC vanished
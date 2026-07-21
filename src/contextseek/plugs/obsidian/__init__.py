"""Incremental Obsidian vault DataPlug."""

from contextseek.plugs.obsidian.plug import ObsidianVaultPlug
from contextseek.plugs.obsidian.sync_state import FileSyncState, SyncStateStore
from contextseek.plugs.obsidian.wikilinks import extract_wikilinks, resolve_wikilink_targets

__all__ = [
    "ObsidianVaultPlug",
    "FileSyncState",
    "SyncStateStore",
    "extract_wikilinks",
    "resolve_wikilink_targets",
]
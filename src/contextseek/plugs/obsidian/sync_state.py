"""JSON-backed per-file sync cursor for incremental Obsidian vault sync.

Records ``mtime``, ``content_hash`` and ``context_item_id`` for every
``.md`` file so the next sync can short-circuit via mtime and detect
vanished files for soft-delete.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass


@dataclass
class FileSyncState:
    """Per-file sync state — the sync cursor's atomic unit."""

    mtime: float
    content_hash: str
    context_item_id: str


class SyncStateStore:
    """JSON file-backed store for ``{file_path: FileSyncState}`` mappings.

    The state file lives at ``{vault}/.contextseek/sync_state.json`` and is
    created lazily on first ``save()``.
    """

    def __init__(self, state_path: pathlib.Path) -> None:
        self._path = state_path

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def load(self) -> dict[str, FileSyncState]:
        """Load all file states from disk.  Returns ``{}`` if missing."""
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return {
            k: FileSyncState(**v)
            for k, v in raw.items()
            if isinstance(v, dict) and "mtime" in v and "content_hash" in v
        }

    def save(self, states: dict[str, FileSyncState]) -> None:
        """Persist all file states to disk (creates parent dirs)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in states.items()}
        self._path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def get(self, file_path: str) -> FileSyncState | None:
        return self.load().get(file_path)

    def all_paths(self) -> set[str]:
        return set(self.load().keys())
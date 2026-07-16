"""Materialize distilled prompt skills as SKILL.md files on disk.

The mirror operation of :mod:`contextseek.daemon.sync_cmd`: instead of
importing notes/documents *into* ContextSeek, this writes ``stage=skill``
items *out* to a directory of Hermes-style ``SKILL.md`` files. That directory
becomes a portable source other agent tools (Claude Code, Qoder, ...) can pick
up — directly or via a hub/symlink tool such as SkillForge.

Only ``skill_type="prompt"`` skills are materialized; tool/mcp skills are JSON
tool definitions, not SKILL.md documents, and are exported via
``ctx.skill_tools()`` instead.

IMPORTANT: the export directory must NOT also be a daemon ``WATCH_PATHS``
target. Watching it would re-ingest the emitted SKILL.md files as raw
documents, forming a feedback loop.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from contextseek.domain.skill_executor import SkillExporter
from contextseek.domain.serialization import serialize_context_item
from contextseek.domain.skill_ir import SkillIR, can_transition

if TYPE_CHECKING:
    from contextseek.client.contextseek import ContextSeek

_MANIFEST_NAME = ".contextseek-export.json"


@dataclass
class ExportReport:
    written: int = 0
    unchanged: int = 0
    pruned: int = 0
    skipped_low_confidence: int = 0
    skipped_unpublishable: int = 0
    skipped_unconfirmed: int = 0
    skipped_conflict: int = 0
    out_dir: str = ""


def _slugify(name: str, fallback: str) -> str:
    """Filesystem-safe slug: lowercase, non-alphanumeric → single dash."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or fallback


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_manifest(path: pathlib.Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def export_skills(
    ctx: "ContextSeek",
    *,
    scope: str,
    out_dir: str | pathlib.Path,
    min_confidence: float = 0.8,
    dry_run: bool = False,
    prune: bool = True,
    spec: str = "hermes",
    item_ids: list[str] | None = None,
    conflict_strategy: str = "rename",
    require_confirmed: bool = False,
) -> ExportReport:
    """Write prompt skills in *scope* to ``out_dir/<slug>/SKILL.md``.

    ``spec`` selects the SKILL.md flavor: ``"hermes"`` (default, legacy
    frontmatter with version/tags) or ``"agent"`` (strict Anthropic Agent
    Skills frontmatter; point ``out_dir`` at ``.claude/skills/`` to land
    directly into a Claude Code / claude.ai skills directory).

    ``item_ids`` — when provided, only skills whose item id is in this list
    are exported.  Selective export disables pruning automatically so that
    other previously-exported skills are not removed.

    ``conflict_strategy`` controls how an *external* (non-manifest) SKILL.md
    in the target directory is handled: ``"overwrite"`` replaces it,
    ``"skip"`` leaves it untouched, ``"rename"`` writes to a disambiguated
    slug directory instead.

    ``require_confirmed`` — when True, skills that have not been
    human-confirmed (``content["status"] != "confirmed"`` and
    ``provenance.verified is not True``) are skipped and counted in
    ``skipped_unconfirmed``.

    Idempotent: keyed by the skill's stable ``skill_id`` (falling back to the
    item id for legacy skills), a SKILL.md whose content is unchanged is left
    untouched. A manifest (``out_dir/.contextseek-export.json``) records which
    directories this exporter owns, so pruning only removes our own stale
    exports and never touches hand-authored skills or symlinks in the same dir.
    """
    base = pathlib.Path(out_dir).expanduser()
    report = ExportReport(out_dir=str(base))
    exporter = SkillExporter()

    # Validate conflict_strategy.
    if conflict_strategy not in ("overwrite", "skip", "rename"):
        raise ValueError(
            f"conflict_strategy must be 'overwrite', 'skip', or 'rename', got '{conflict_strategy}'"
        )

    # Selective export disables prune so other skills are preserved.
    if item_ids is not None:
        prune = False

    def persist_item(item: Any) -> None:
        """Best-effort persistence for clients that expose adapter+resolver."""
        if dry_run:
            return
        adapter = getattr(ctx, "adapter", None)
        resolver = getattr(ctx, "resolver", None)
        if adapter is None or resolver is None:
            return
        try:
            ref = resolver.ref_for(scope, item.id)
            adapter.write(ref, serialize_context_item(item))
        except Exception:
            # Export should remain best-effort even if state persistence fails.
            return

    manifest_path = base / _MANIFEST_NAME
    old_manifest = _load_manifest(manifest_path)

    # Filter to confident, publishable prompt skills. Deprecated or quality-gated
    # (needs_review) skills must not reach the runtime surface.
    skills = ctx.skills(scope, skill_type="prompt")
    selected = []
    for item in skills:
        # Selective export: only items in the provided id set.
        if item_ids is not None and item.id not in item_ids:
            continue

        if item.provenance.confidence < min_confidence:
            report.skipped_low_confidence += 1
            continue
        ir = SkillIR.from_content(item.content)
        if ir.publish_status == "deprecated" or "needs_review" in item.tags:
            report.skipped_unpublishable += 1
            continue
        # Require human confirmation if requested.
        if require_confirmed:
            content_status = ""
            if isinstance(item.content, dict):
                content_status = item.content.get("status", "")
            is_confirmed = content_status == "confirmed" or item.provenance.verified
            if not is_confirmed:
                report.skipped_unconfirmed += 1
                continue
        # Quality-gate/render-check passed by this point -> at least validated.
        if can_transition(ir.publish_status, "validated"):
            ir.publish_status = "validated"
            item.content = ir.to_content()
            persist_item(item)
        selected.append(item)

    render = (
        exporter.to_agent_skill_md if spec == "agent" else exporter.to_hermes_skill_md
    )

    # Assign collision-free slugs (append id8 when two skills share a slug).
    new_manifest: dict[str, dict[str, str]] = {}
    used_slugs: set[str] = set()
    for item in selected:
        name = item.content.get("name", "") if isinstance(item.content, dict) else ""
        slug = _slugify(name, fallback=f"skill_{item.id[:8]}")
        if slug in used_slugs:
            slug = f"{slug}-{item.id[:8]}"
        used_slugs.add(slug)

        key = SkillIR.from_content(item.content).skill_id or item.id
        md = render(item)
        digest = _content_hash(md)

        skill_file = base / slug / "SKILL.md"
        prev = old_manifest.get(key)
        if (
            prev is not None
            and prev.get("slug") == slug
            and prev.get("hash") == digest
            and skill_file.exists()
        ):
            ir = SkillIR.from_content(item.content)
            if can_transition(ir.publish_status, "published"):
                ir.publish_status = "published"
                item.content = ir.to_content()
                persist_item(item)
            new_manifest[key] = {"slug": slug, "hash": digest}
            report.unchanged += 1
            continue

        # Conflict detection: file exists but is NOT in our manifest (external).
        external_conflict = skill_file.exists() and prev is None
        if external_conflict:
            if conflict_strategy == "skip":
                report.skipped_conflict += 1
                continue
            elif conflict_strategy == "rename":
                # Find a disambiguated slug that doesn't collide.
                base_slug = slug
                suffix = item.id[8:16] if len(item.id) >= 16 else item.id[:8]
                candidate = f"{base_slug}-{suffix}"
                idx = 0
                while candidate in used_slugs or (base / candidate / "SKILL.md").exists():
                    candidate = f"{base_slug}-{suffix}-{idx}"
                    idx += 1
                    if idx > 999:
                        # Safety valve: extremely unlikely to reach here.
                        candidate = f"{base_slug}-{suffix}-{item.id}"
                        break
                slug = candidate
                used_slugs.add(slug)
                skill_file = base / slug / "SKILL.md"
            # else: overwrite -- fall through to normal write

        new_manifest[key] = {"slug": slug, "hash": digest}

        if not dry_run:
            skill_file.parent.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(md, encoding="utf-8")
            ir = SkillIR.from_content(item.content)
            if can_transition(ir.publish_status, "published"):
                ir.publish_status = "published"
                item.content = ir.to_content()
                persist_item(item)
        report.written += 1

    # Prune directories we previously owned but no longer export.
    if prune:
        live_ids = set(new_manifest)
        for old_id, rec in old_manifest.items():
            if old_id in live_ids:
                continue
            stale_dir = base / rec.get("slug", "")
            if not rec.get("slug"):
                continue
            if not dry_run and stale_dir.is_dir():
                skill_md = stale_dir / "SKILL.md"
                skill_md.unlink(missing_ok=True)
                try:
                    stale_dir.rmdir()
                except OSError:
                    pass
            report.pruned += 1

    if not dry_run:
        base.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return report


__all__ = ["ExportReport", "export_skills"]

"""Tests for daemon/skill_export.py — materialize prompt skills as SKILL.md."""

from __future__ import annotations

import pathlib

from contextseek.daemon.skill_export import _MANIFEST_NAME, export_skills
from contextseek.domain.serialization import (
    deserialize_context_item,
    serialize_context_item,
)
from contextseek.domain.context_item import ContextItem, _generate_id
from contextseek.domain.provenance import Provenance, SourceType
from contextseek.domain.skill_ir import SkillIR
from contextseek.domain.stages import Stage
from contextseek.plugs.skills import _parse_skill_md


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skill(
    name: str,
    body: str = "do the thing",
    *,
    confidence: float = 0.8,
    content: dict | None = None,
    tags: list[str] | None = None,
) -> ContextItem:
    return ContextItem(
        id=_generate_id(),
        content=content
        or {
            "skill_type": "prompt",
            "name": name,
            "description": f"{name} description",
            "version": "1.0.0",
            "tags": ["alpha"],
            "body": body,
        },
        scope="me/work",
        provenance=Provenance(
            source_type=SourceType.distillation,
            source_id="src",
            confidence=confidence,
        ),
        stage=Stage.skill,
        tags=tags or [],
    )


class _StubClient:
    """Minimal client: export_skills only calls .skills(scope, skill_type=...)."""

    def __init__(self, items: list[ContextItem]) -> None:
        self._items = items

    def skills(self, scope, *, skill_type=None, query=None, k=50):
        out = [it for it in self._items if it.scope == scope]
        if skill_type is not None:
            out = [
                it
                for it in out
                if isinstance(it.content, dict)
                and it.content.get("skill_type") == skill_type
            ]
        return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_writes_skill_md_with_frontmatter(tmp_path: pathlib.Path) -> None:
    ctx = _StubClient([_skill("Deploy Service", "step 1\nstep 2")])
    report = export_skills(ctx, scope="me/work", out_dir=tmp_path)

    assert report.written == 1
    md = (tmp_path / "deploy-service" / "SKILL.md").read_text(encoding="utf-8")
    assert md.startswith("---")
    assert "name: Deploy Service" in md
    assert "step 1" in md
    assert (tmp_path / _MANIFEST_NAME).exists()


def test_low_confidence_skipped(tmp_path: pathlib.Path) -> None:
    ctx = _StubClient(
        [
            _skill("Good", confidence=0.9),
            _skill("Draft", confidence=0.75),  # heuristic, below default 0.8
        ]
    )
    report = export_skills(ctx, scope="me/work", out_dir=tmp_path)

    assert report.written == 1
    assert report.skipped_low_confidence == 1
    assert (tmp_path / "good" / "SKILL.md").exists()
    assert not (tmp_path / "draft").exists()


def test_idempotent_second_run_unchanged(tmp_path: pathlib.Path) -> None:
    ctx = _StubClient([_skill("Deploy")])
    first = export_skills(ctx, scope="me/work", out_dir=tmp_path)
    second = export_skills(ctx, scope="me/work", out_dir=tmp_path)

    assert first.written == 1
    assert second.written == 0
    assert second.unchanged == 1


def test_prune_removes_stale_export(tmp_path: pathlib.Path) -> None:
    skill_a = _skill("Alpha")
    skill_b = _skill("Beta")
    export_skills(_StubClient([skill_a, skill_b]), scope="me/work", out_dir=tmp_path)
    assert (tmp_path / "beta" / "SKILL.md").exists()

    # Beta no longer present → its dir is pruned, alpha untouched.
    report = export_skills(_StubClient([skill_a]), scope="me/work", out_dir=tmp_path)
    assert report.pruned == 1
    assert (tmp_path / "alpha" / "SKILL.md").exists()
    assert not (tmp_path / "beta").exists()


def test_prune_leaves_unmanaged_dirs_untouched(tmp_path: pathlib.Path) -> None:
    # A hand-authored skill the exporter never wrote (not in manifest).
    (tmp_path / "handwritten").mkdir()
    (tmp_path / "handwritten" / "SKILL.md").write_text("manual", encoding="utf-8")

    export_skills(_StubClient([_skill("Alpha")]), scope="me/work", out_dir=tmp_path)
    # Remove alpha → prune our export, but never touch handwritten/.
    export_skills(_StubClient([]), scope="me/work", out_dir=tmp_path)

    assert (tmp_path / "handwritten" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "manual"
    assert not (tmp_path / "alpha").exists()


def test_dry_run_writes_nothing(tmp_path: pathlib.Path) -> None:
    ctx = _StubClient([_skill("Deploy")])
    report = export_skills(ctx, scope="me/work", out_dir=tmp_path, dry_run=True)

    assert report.written == 1
    assert not (tmp_path / "deploy").exists()
    assert not (tmp_path / _MANIFEST_NAME).exists()


def test_slug_collision_disambiguated(tmp_path: pathlib.Path) -> None:
    a = _skill("Same Name")
    b = _skill("Same Name")
    report = export_skills(_StubClient([a, b]), scope="me/work", out_dir=tmp_path)

    assert report.written == 2
    assert (tmp_path / "same-name" / "SKILL.md").exists()
    assert (tmp_path / f"same-name-{b.id[:8]}" / "SKILL.md").exists()


def test_roundtrip_through_hermes_parser(tmp_path: pathlib.Path) -> None:
    ctx = _StubClient([_skill("Deploy Service", "line one\n\nline two")])
    export_skills(ctx, scope="me/work", out_dir=tmp_path)

    md = (tmp_path / "deploy-service" / "SKILL.md").read_text(encoding="utf-8")
    parsed = _parse_skill_md(md)
    assert parsed["name"] == "Deploy Service"
    assert parsed["description"] == "Deploy Service description"
    assert "line one" in parsed["body"]
    assert "line two" in parsed["body"]


def test_agent_spec_writes_strict_frontmatter(tmp_path: pathlib.Path) -> None:
    from contextseek.domain.skill_ir import SkillIR

    content = (
        SkillIR(
            name="Deploy Service",
            description="Deploy the service",
            body="## Overview\n\nstep",
            tags=["ops"],
        )
        .assign_identity("k1")
        .to_content()
    )
    ctx = _StubClient([_skill("Deploy Service", content=content)])

    report = export_skills(ctx, scope="me/work", out_dir=tmp_path, spec="agent")
    assert report.written == 1
    md = (tmp_path / "deploy-service" / "SKILL.md").read_text(encoding="utf-8")
    parsed = _parse_skill_md(md)
    assert parsed["name"] == "deploy-service"  # agent spec slugifies
    assert "metadata:" in md
    assert "\nversion:" not in md.split("---")[1]  # version nested, not top-level


def test_needs_review_skill_not_exported(tmp_path: pathlib.Path) -> None:
    ctx = _StubClient([_skill("Draft Skill", tags=["needs_review"], confidence=0.9)])
    report = export_skills(ctx, scope="me/work", out_dir=tmp_path)
    assert report.written == 0
    assert report.skipped_unpublishable == 1
    assert not (tmp_path / "draft-skill").exists()


def test_export_advances_publish_status_to_published(tmp_path: pathlib.Path) -> None:
    from contextseek.client.contextseek import ContextSeek

    ctx = ContextSeek()
    scope = "me/work"
    ir = SkillIR(
        name="Deploy Service",
        description="Deploy safely",
        body="## Overview\n\nStep 1",
        publish_status="drafted",
    ).assign_identity("k1")
    item = ContextItem(
        id=_generate_id(),
        content=ir.to_content(),
        scope=scope,
        provenance=Provenance(
            source_type=SourceType.distillation,
            source_id="k1",
            confidence=0.9,
        ),
        stage=Stage.skill,
    )
    ref = ctx.resolver.ref_for(scope, item.id)
    ctx.adapter.write(ref, serialize_context_item(item))

    report = export_skills(ctx, scope=scope, out_dir=tmp_path)
    assert report.written == 1

    stored = deserialize_context_item(ctx.adapter.read(ref))
    assert SkillIR.from_content(stored.content).publish_status == "published"


# ---------------------------------------------------------------------------
# Tests for selective export, conflict strategy, and confirmation gate
# ---------------------------------------------------------------------------


def test_export_selected_item_ids_only(tmp_path: pathlib.Path) -> None:
    a = _skill("Alpha Skill")
    b = _skill("Beta Skill")
    c = _skill("Gamma Skill")
    ctx = _StubClient([a, b, c])

    report = export_skills(
        ctx,
        scope="me/work",
        out_dir=tmp_path,
        item_ids=[a.id, c.id],
    )
    assert report.written == 2
    assert (tmp_path / "alpha-skill" / "SKILL.md").exists()
    assert not (tmp_path / "beta-skill").exists()
    assert (tmp_path / "gamma-skill" / "SKILL.md").exists()


def test_export_require_confirmed_skips_unconfirmed(tmp_path: pathlib.Path) -> None:
    unconfirmed = _skill("Unconfirmed", confidence=0.9)
    confirmed = _skill("Confirmed", confidence=0.9, content={
        "skill_type": "prompt",
        "name": "Confirmed",
        "description": "Confirmed description",
        "version": "1.0.0",
        "tags": ["alpha"],
        "body": "do stuff",
        "status": "confirmed",
    })

    ctx = _StubClient([unconfirmed, confirmed])
    report = export_skills(
        ctx,
        scope="me/work",
        out_dir=tmp_path,
        require_confirmed=True,
    )
    assert report.skipped_unconfirmed == 1
    assert report.written == 1
    assert (tmp_path / "confirmed" / "SKILL.md").exists()
    assert not (tmp_path / "unconfirmed").exists()


def test_conflict_strategy_skip(tmp_path: pathlib.Path) -> None:
    # Pre-create an external (non-manifest) SKILL.md in the target directory.
    ext_dir = tmp_path / "alpha-skill"
    ext_dir.mkdir(parents=True)
    (ext_dir / "SKILL.md").write_text("# hand-written\n\nmanual content", encoding="utf-8")

    ctx = _StubClient([_skill("Alpha Skill")])
    report = export_skills(
        ctx,
        scope="me/work",
        out_dir=tmp_path,
        conflict_strategy="skip",
    )
    assert report.skipped_conflict == 1
    assert report.written == 0
    # External file untouched.
    assert (ext_dir / "SKILL.md").read_text(encoding="utf-8").startswith("# hand-written")


def test_conflict_strategy_overwrite(tmp_path: pathlib.Path) -> None:
    ext_dir = tmp_path / "alpha-skill"
    ext_dir.mkdir(parents=True)
    original = "# hand-written\n\nmanual content"
    (ext_dir / "SKILL.md").write_text(original, encoding="utf-8")

    ctx = _StubClient([_skill("Alpha Skill")])
    report = export_skills(
        ctx,
        scope="me/work",
        out_dir=tmp_path,
        conflict_strategy="overwrite",
    )
    assert report.written == 1
    assert report.skipped_conflict == 0
    content = (ext_dir / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---")
    assert "Alpha Skill" in content


def test_conflict_strategy_rename(tmp_path: pathlib.Path) -> None:
    ext_dir = tmp_path / "alpha-skill"
    ext_dir.mkdir(parents=True)
    original = "# hand-written\n\nmanual content"
    (ext_dir / "SKILL.md").write_text(original, encoding="utf-8")

    skill = _skill("Alpha Skill")
    ctx = _StubClient([skill])
    report = export_skills(
        ctx,
        scope="me/work",
        out_dir=tmp_path,
        conflict_strategy="rename",
    )
    assert report.written == 1
    assert report.skipped_conflict == 0
    # External file untouched.
    assert (ext_dir / "SKILL.md").read_text(encoding="utf-8") == original
    # New file written to a renamed slug directory.
    suffix = skill.id[8:16] if len(skill.id) >= 16 else skill.id[:8]
    renamed_dir = tmp_path / f"alpha-skill-{suffix}"
    assert (renamed_dir / "SKILL.md").exists()
    assert (renamed_dir / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_export_with_item_ids_disables_prune(tmp_path: pathlib.Path) -> None:
    skill_a = _skill("Alpha")
    skill_b = _skill("Beta")
    # Initial full export writes both.
    export_skills(_StubClient([skill_a, skill_b]), scope="me/work", out_dir=tmp_path)
    assert (tmp_path / "alpha" / "SKILL.md").exists()
    assert (tmp_path / "beta" / "SKILL.md").exists()

    # Selective export of only Alpha — Beta must NOT be pruned.
    report = export_skills(
        _StubClient([skill_a, skill_b]),
        scope="me/work",
        out_dir=tmp_path,
        item_ids=[skill_a.id],
    )
    assert report.pruned == 0
    assert (tmp_path / "alpha" / "SKILL.md").exists()
    assert (tmp_path / "beta" / "SKILL.md").exists()


def test_export_invalid_conflict_strategy_raises(tmp_path: pathlib.Path) -> None:
    import pytest

    ctx = _StubClient([_skill("Alpha")])
    with pytest.raises(ValueError, match="conflict_strategy"):
        export_skills(
            ctx,
            scope="me/work",
            out_dir=tmp_path,
            conflict_strategy="bogus",
        )


def test_export_empty_item_ids_exports_nothing(tmp_path: pathlib.Path) -> None:
    ctx = _StubClient([_skill("Alpha"), _skill("Beta")])
    report = export_skills(
        ctx,
        scope="me/work",
        out_dir=tmp_path,
        item_ids=[],
    )
    assert report.written == 0
    assert not (tmp_path / "alpha").exists()
    assert not (tmp_path / "beta").exists()

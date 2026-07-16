"""Module-6 slice-B: MCP live skill mounting + runtime feedback writeback."""

from __future__ import annotations

import pytest

from contextseek.client.contextseek import ContextSeek
from contextseek.domain.context_item import ContextItem, _generate_id
from contextseek.domain.provenance import Provenance, SourceType
from contextseek.domain.serialization import serialize_context_item
from contextseek.domain.skill_ir import SkillIR
from contextseek.domain.stages import Stage
from contextseek.mcp.runtime import MCPRuntime
from contextseek.mcp.server import ContextSeekMCPServer
from contextseek.observability.audit import AuditLog

SCOPE = "me/work"


def _ctx() -> ContextSeek:
    return ContextSeek(audit_log=AuditLog())


def _seed(ctx: ContextSeek, ir: SkillIR, *, tags: list[str] | None = None) -> str:
    item = ContextItem(
        id=_generate_id(),
        content=ir.to_content(),
        scope=SCOPE,
        provenance=Provenance(
            source_type=SourceType.distillation, source_id="k", confidence=0.8
        ),
        stage=Stage.skill,
        tags=tags or [],
    )
    ctx.adapter.write(
        ctx.resolver.ref_for(SCOPE, item.id), serialize_context_item(item)
    )
    return ir.skill_id  # type: ignore[return-value]


def _prompt_skill(name="Deploy Guide", body="step 1", src="k1") -> SkillIR:
    return SkillIR(name=name, description="How to deploy", body=body).assign_identity(
        src
    )


def _tool_skill(src="k2") -> SkillIR:
    return SkillIR(
        name="Search Docs",
        description="Search docs",
        kind="tool",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    ).assign_identity(src)


class TestMCPPromptMounting:
    def test_prompts_list_exposes_prompt_skills(self):
        ctx = _ctx()
        pid = _seed(ctx, _prompt_skill())
        rt = MCPRuntime(server=ContextSeekMCPServer(client=ctx))
        res = rt.handle_request(
            {"method": "prompts/list", "id": 1, "params": {"scope": SCOPE}}
        )
        names = [p["name"] for p in res["result"]["prompts"]]
        assert pid in names

    def test_prompts_get_returns_body_and_records_selection(self):
        ctx = _ctx()
        pid = _seed(ctx, _prompt_skill(body="run the migration"))
        rt = MCPRuntime(server=ContextSeekMCPServer(client=ctx))
        res = rt.handle_request(
            {"method": "prompts/get", "id": 2, "params": {"scope": SCOPE, "name": pid}}
        )
        text = res["result"]["messages"][0]["content"]["text"]
        assert "run the migration" in text
        assert any(r.action == "skill_selected" for r in ctx.audit_log.records)

    def test_deprecated_prompt_withheld(self):
        ctx = _ctx()
        ir = _prompt_skill()
        ir.publish_status = "deprecated"
        _seed(ctx, ir)
        rt = MCPRuntime(server=ContextSeekMCPServer(client=ctx))
        res = rt.handle_request(
            {"method": "prompts/list", "id": 1, "params": {"scope": SCOPE}}
        )
        assert res["result"]["prompts"] == []

    def test_deprecated_prompt_blocked_even_when_called_directly(self):
        ctx = _ctx()
        ir = _prompt_skill()
        ir.publish_status = "deprecated"
        pid = _seed(ctx, ir)
        srv = ContextSeekMCPServer(client=ctx)
        out = srv.get_prompt(SCOPE, pid)
        assert "error" in out
        assert "not publishable" in out["error"]

    def test_initialize_advertises_prompts_capability(self):
        ctx = _ctx()
        rt = MCPRuntime(server=ContextSeekMCPServer(client=ctx))
        res = rt.handle_request({"method": "initialize", "id": 1, "params": {}})
        assert "prompts" in res["result"]["capabilities"]


class TestMCPLiveToolMounting:
    def test_tools_list_appends_live_tool_skill_with_scope(self):
        ctx = _ctx()
        tid = _seed(ctx, _tool_skill())
        rt = MCPRuntime(server=ContextSeekMCPServer(client=ctx))
        with_scope = rt.handle_request(
            {"method": "tools/list", "id": 1, "params": {"scope": SCOPE}}
        )["result"]["tools"]
        skill_tools = [t for t in with_scope if t["name"] == f"skill_{tid}"]
        assert len(skill_tools) == 1
        assert skill_tools[0]["inputSchema"]["properties"]["q"]["type"] == "string"

    def test_tools_list_without_scope_has_no_skill_tools(self):
        ctx = _ctx()
        _seed(ctx, _tool_skill())
        rt = MCPRuntime(server=ContextSeekMCPServer(client=ctx))
        tools = rt.handle_request({"method": "tools/list", "id": 1, "params": {}})[
            "result"
        ]["tools"]
        assert not any(t["name"].startswith("skill_") for t in tools)

    def test_calling_live_tool_returns_definition_and_records_selection(self):
        ctx = _ctx()
        tid = _seed(ctx, _tool_skill())
        srv = ContextSeekMCPServer(client=ctx)
        out = srv.call_tool(f"skill_{tid}", {"scope": SCOPE, "q": "x"})
        assert out["skill_id"] == tid
        assert out["parameters"]["properties"]["q"]["type"] == "string"
        assert any(r.action == "skill_selected" for r in ctx.audit_log.records)

    def test_needs_review_tool_blocked_even_when_called_directly(self):
        ctx = _ctx()
        tid = _seed(ctx, _tool_skill(), tags=["needs_review"])
        srv = ContextSeekMCPServer(client=ctx)
        out = srv.call_tool(f"skill_{tid}", {"scope": SCOPE, "q": "x"})
        assert "error" in out
        assert "not publishable" in out["error"]

    def test_live_tool_schema_requires_scope_so_call_contract_matches(self):
        # A standard MCP client fills only the advertised inputSchema; if scope
        # is needed by call_tool it must appear in the schema (list/call parity).
        ctx = _ctx()
        tid = _seed(ctx, _tool_skill())
        srv = ContextSeekMCPServer(client=ctx)
        tool = next(
            t for t in srv.list_tools(scope=SCOPE) if t["name"] == f"skill_{tid}"
        )
        schema = tool["inputSchema"]
        assert "scope" in schema["properties"]
        assert "scope" in schema["required"]
        # Original skill params are preserved alongside scope.
        assert "q" in schema["properties"]
        # scope advertises the right default, so a client filling the schema
        # (honoring defaults) can call successfully.
        assert schema["properties"]["scope"].get("default") == SCOPE
        args = {
            key: spec.get("default", "x") for key, spec in schema["properties"].items()
        }
        out = srv.call_tool(tool["name"], args)
        assert out.get("skill_id") == tid


class TestSkillFeedbackWriteback:
    def test_success_raises_boost_and_emits_events(self):
        ctx = _ctx()
        tid = _seed(ctx, _tool_skill())
        before = ctx._find_skill_item(SCOPE, tid).relevance_boost
        result = ctx.skill_feedback(scope=SCOPE, skill_id=tid, outcome="success")
        assert result["delta_boost"] > 0
        after = ctx._find_skill_item(SCOPE, tid).relevance_boost
        assert after > before
        actions = {r.action for r in ctx.audit_log.records}
        assert "skill_executed" in actions
        assert "skill_feedback_ingested" in actions

    def test_sustained_failure_deprecates_skill(self):
        ctx = _ctx()
        tid = _seed(ctx, _tool_skill())
        for _ in range(60):
            ctx.skill_feedback(
                scope=SCOPE, skill_id=tid, outcome="fail", error_type="timeout"
            )
        item = ctx._find_skill_item(SCOPE, tid)
        assert SkillIR.from_content(item.content).publish_status == "deprecated"
        assert "needs_review" in item.tags

    def test_unknown_skill_raises(self):
        ctx = _ctx()
        with pytest.raises(ValueError):
            ctx.skill_feedback(scope=SCOPE, skill_id="sk_missing", outcome="success")

    def test_acceptance_mcp_call_then_feedback_flows_to_signal(self):
        """Slice-B acceptance: an external MCP client calling skill_feedback after
        running a skill produces a skill_feedback_ingested event that flows back
        into the evolution signal (relevance_boost)."""
        ctx = _ctx()
        tid = _seed(ctx, _tool_skill())
        srv = ContextSeekMCPServer(client=ctx)

        srv.call_tool(f"skill_{tid}", {"scope": SCOPE, "q": "x"})  # select/execute
        before = ctx._find_skill_item(SCOPE, tid).relevance_boost
        srv.call_tool(
            "contextseek_skill_feedback",
            {"scope": SCOPE, "skill_id": tid, "outcome": "success"},
        )
        after = ctx._find_skill_item(SCOPE, tid).relevance_boost

        assert after > before
        ingested = [
            r for r in ctx.audit_log.records if r.action == "skill_feedback_ingested"
        ]
        assert ingested and ingested[-1].detail["skill_id"] == tid


# ─── Skill editing & human confirmation (Issue #40) ──────────────────────────


def _seed_by_item_id(ctx: ContextSeek, ir: SkillIR, *, tags=None) -> str:
    """Seed a skill and return its ContextItem.id (not skill_id)."""
    item = ContextItem(
        id=_generate_id(),
        content=ir.to_content(),
        scope=SCOPE,
        provenance=Provenance(
            source_type=SourceType.distillation, source_id="k", confidence=0.8
        ),
        stage=Stage.skill,
        tags=tags or [],
    )
    ctx.adapter.write(
        ctx.resolver.ref_for(SCOPE, item.id), serialize_context_item(item)
    )
    return item.id


def _seed_string_content(ctx: ContextSeek, body: str = "old body") -> str:
    """Seed a string-content skill and return its ContextItem.id."""
    item = ContextItem(
        id=_generate_id(),
        content=body,
        scope=SCOPE,
        provenance=Provenance(
            source_type=SourceType.distillation, source_id="k", confidence=0.7
        ),
        stage=Stage.skill,
        tags=[],
    )
    ctx.adapter.write(
        ctx.resolver.ref_for(SCOPE, item.id), serialize_context_item(item)
    )
    return item.id


class TestUpdateSkill:
    def test_update_dict_content_fields(self):
        ctx = _ctx()
        ir = _prompt_skill(name="Old", body="old body")
        item_id = _seed_by_item_id(ctx, ir)

        updated = ctx.update_skill(
            scope=SCOPE, item_id=item_id, name="New Name", body="new body"
        )
        assert updated.content["name"] == "New Name"
        assert updated.content["body"] == "new body"
        assert updated.content["status"] == "edited"
        assert updated.updated_at is not None
        # provenance should not change on edit
        assert updated.provenance.confidence == 0.8
        assert updated.provenance.verified is False

    def test_update_string_content_upgrades_to_dict(self):
        ctx = _ctx()
        item_id = _seed_string_content(ctx, "original body")

        updated = ctx.update_skill(
            scope=SCOPE, item_id=item_id, name="Upgraded", body="new body"
        )
        # Should now be dict content with status
        assert isinstance(updated.content, dict)
        assert updated.content["name"] == "Upgraded"
        assert updated.content["body"] == "new body"
        assert updated.content["status"] == "edited"

    def test_update_persists_to_storage(self):
        ctx = _ctx()
        ir = _prompt_skill(name="Persist", body="v1")
        item_id = _seed_by_item_id(ctx, ir)

        ctx.update_skill(scope=SCOPE, item_id=item_id, body="v2")
        # Reload from storage
        reloaded = ctx._find_skill_by_item_id(SCOPE, item_id)
        assert reloaded is not None
        assert reloaded.content["body"] == "v2"
        assert reloaded.content["status"] == "edited"

    def test_update_unknown_raises(self):
        ctx = _ctx()
        with pytest.raises(ValueError):
            ctx.update_skill(scope=SCOPE, item_id="nope", name="x")

    def test_update_emits_audit_event(self):
        ctx = _ctx()
        ir = _prompt_skill(name="Audit", body="b")
        item_id = _seed_by_item_id(ctx, ir)

        ctx.update_skill(scope=SCOPE, item_id=item_id, name="Audited")
        actions = {r.action for r in ctx.audit_log.records}
        assert "skill_updated" in actions


class TestConfirmSkill:
    def test_confirm_sets_confidence_and_verified(self):
        ctx = _ctx()
        ir = _prompt_skill(name="Confirm", body="b")
        item_id = _seed_by_item_id(ctx, ir)

        confirmed = ctx.confirm_skill(scope=SCOPE, item_id=item_id)
        assert confirmed.provenance.confidence == 1.0
        assert confirmed.provenance.verified is True
        assert confirmed.content["status"] == "confirmed"

    def test_confirm_string_content_upgrades(self):
        ctx = _ctx()
        item_id = _seed_string_content(ctx)

        confirmed = ctx.confirm_skill(scope=SCOPE, item_id=item_id)
        assert isinstance(confirmed.content, dict)
        assert confirmed.content["status"] == "confirmed"
        assert confirmed.provenance.verified is True

    def test_confirm_persists_to_storage(self):
        ctx = _ctx()
        ir = _prompt_skill(name="CPersist", body="b")
        item_id = _seed_by_item_id(ctx, ir)

        ctx.confirm_skill(scope=SCOPE, item_id=item_id)
        reloaded = ctx._find_skill_by_item_id(SCOPE, item_id)
        assert reloaded is not None
        assert reloaded.provenance.confidence == 1.0
        assert reloaded.provenance.verified is True

    def test_confirm_unknown_raises(self):
        ctx = _ctx()
        with pytest.raises(ValueError):
            ctx.confirm_skill(scope=SCOPE, item_id="nope")

    def test_confirm_emits_audit_event(self):
        ctx = _ctx()
        ir = _prompt_skill(name="CAudit", body="b")
        item_id = _seed_by_item_id(ctx, ir)

        ctx.confirm_skill(scope=SCOPE, item_id=item_id)
        actions = {r.action for r in ctx.audit_log.records}
        assert "skill_confirmed" in actions


class TestSkillStatus:
    def test_auto_generated_default(self):
        ctx = _ctx()
        ir = _prompt_skill(name="Auto", body="b")
        item_id = _seed_by_item_id(ctx, ir)
        item = ctx._find_skill_by_item_id(SCOPE, item_id)
        assert item is not None
        assert ctx.skill_status(item) == "auto-generated"

    def test_edited_after_update(self):
        ctx = _ctx()
        ir = _prompt_skill(name="Ed", body="b")
        item_id = _seed_by_item_id(ctx, ir)
        ctx.update_skill(scope=SCOPE, item_id=item_id, name="Edited")
        item = ctx._find_skill_by_item_id(SCOPE, item_id)
        assert item is not None
        assert ctx.skill_status(item) == "edited"

    def test_confirmed_after_confirm(self):
        ctx = _ctx()
        ir = _prompt_skill(name="Conf", body="b")
        item_id = _seed_by_item_id(ctx, ir)
        ctx.confirm_skill(scope=SCOPE, item_id=item_id)
        item = ctx._find_skill_by_item_id(SCOPE, item_id)
        assert item is not None
        assert ctx.skill_status(item) == "confirmed"

    def test_string_content_without_status_is_auto(self):
        ctx = _ctx()
        item_id = _seed_string_content(ctx)
        item = ctx._find_skill_by_item_id(SCOPE, item_id)
        assert item is not None
        assert ctx.skill_status(item) == "auto-generated"

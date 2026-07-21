"""Integration tests aligned with ``examples/full_pipeline_file.py``."""

from __future__ import annotations

from pathlib import Path

from full_pipeline_file import (
    DEMO_ITEMS,
    DEMO_SCOPE,
    file_backend_demo_stack,
    run_file_backend_demo,
)
from contextseek.domain.stages import Stage


def test_run_file_backend_demo_summary(tmp_path: Path) -> None:
    summary = run_file_backend_demo(tmp_path, clean_start=True, verbose=False)

    assert len(summary.item_ids) == len(DEMO_ITEMS)
    assert summary.disk_files == len(DEMO_ITEMS)
    assert summary.stats.returned_count >= 1

    ocean_id = summary.item_ids[0]
    assert summary.hits_by_query["分布式"] == (ocean_id,)
    assert summary.hits_by_query["向量"] == (summary.item_ids[1],)
    assert summary.hits_by_query["LangChain"] == (summary.item_ids[2],)
    assert summary.orchestrator_hit_ids == (ocean_id,)


def test_wrong_scope_returns_no_hits(tmp_path: Path) -> None:
    with file_backend_demo_stack(tmp_path) as s:
        response = s.ctx.retrieve(
            "分布式",
            scope="other_tenant/default/bob",
            k=5,
        )
    assert len(response) == 0


def test_full_retrieve_hits_scope(tmp_path: Path) -> None:
    with file_backend_demo_stack(tmp_path) as s:
        response = s.ctx.retrieve(
            "分布式",
            scope=s.scope,
            k=10,
            full=True,
        )
    assert len(response) > 0
    items = [h.item for h in response]
    assert all(it.scope == DEMO_SCOPE for it in items)
    assert any("分布式" in it.content_text for it in items)


def test_items_api_lists_written_rows(tmp_path: Path) -> None:
    with file_backend_demo_stack(tmp_path) as s:
        rows = s.ctx.items(scope=s.scope)
    assert len(rows) == len(DEMO_ITEMS)
    assert {it.stage for it in rows} == {Stage.knowledge}


def test_hits_tag_filter_requires_all_tags(tmp_path: Path) -> None:
    with file_backend_demo_stack(tmp_path) as s:
        vector_item_id = s.item_ids[1]
        response = s.ctx.retrieve(
            "向量",
            scope=s.scope,
            k=5,
            filters={"tags": ["vector", "search", "hybrid"]},
        )
    hits = list(response)
    assert len(hits) == 1
    assert hits[0].item.id == vector_item_id


def test_hits_tag_filter_any_semantics(tmp_path: Path) -> None:
    with file_backend_demo_stack(tmp_path) as s:
        ocean_item_id = s.item_ids[0]
        vector_item_id = s.item_ids[1]
        response = s.ctx.retrieve(
            "分布式 向量",
            scope=s.scope,
            k=5,
            filters={"tags": ["database", "vector"], "tag_match": "any"},
        )
    hits = list(response)
    hit_ids = {h.item.id for h in hits}
    assert ocean_item_id in hit_ids
    assert vector_item_id in hit_ids


def test_hits_tag_filter_any_single_tag(tmp_path: Path) -> None:
    with file_backend_demo_stack(tmp_path) as s:
        langchain_item_id = s.item_ids[2]
        response = s.ctx.retrieve(
            "embedding",
            scope=s.scope,
            k=5,
            filters={"tags": ["embedding"], "tag_match": "any"},
        )
    hits = list(response)
    assert len(hits) == 1
    assert hits[0].item.id == langchain_item_id


def test_response_meta_carries_layer(tmp_path: Path) -> None:
    with file_backend_demo_stack(tmp_path) as s:
        response = s.ctx.retrieve("LangChain", scope=s.scope, k=3)
    assert response.meta.layer in {"summary", "full"}
    assert response.meta.full_via == "expand"


def test_k_limits_hit_count(tmp_path: Path) -> None:
    # 「混合」同时出现在 OceanBase 句与混合检索句中，便于验证 k 截断
    with file_backend_demo_stack(tmp_path) as s:
        hits_all = list(s.ctx.retrieve("混合", scope=s.scope, k=20))
        hits_one = list(s.ctx.retrieve("混合", scope=s.scope, k=1))
    assert len(hits_all) >= 2
    assert len(hits_one) == 1

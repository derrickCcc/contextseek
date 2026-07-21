# API Reference

All public methods are on one `ContextSeek` object. Import and construct it once; all operations share the same adapter, audit log, and strategy.

```python
from contextseek import ContextSeek
ctx = ContextSeek.from_settings()
```

---

## Construction

### `ContextSeek.from_settings(settings=None, *, _version="default")`

Build a `ContextSeek` from environment variables, a `.env` file, or an explicit `ContextSeekSettings` object.

```python
# Auto-reads .env / environment variables
ctx = ContextSeek.from_settings()

# Explicit settings
from contextseek import ContextSeekSettings
from contextseek.config.settings import StorageSettings, EmbeddingSettings, LLMSettings

settings = ContextSeekSettings(
    storage=StorageSettings(backend="file", path="/data/ctx"),
    embedding=EmbeddingSettings(
        provider="langchain",
        class_path="langchain_openai.OpenAIEmbeddings",
        model="text-embedding-3-small",
        dims=1536,
    ),
    llm=LLMSettings(
        provider="langchain",
        class_path="langchain_openai.ChatOpenAI",
        model="gpt-4o-mini",
    ),
)
ctx = ContextSeek.from_settings(settings)
```

See [Configuration](../getting-started/configuration.md) for all available settings.

### `ContextSeek.from_runtime_config(path=None)`

Build from a JSON/YAML runtime config file. Intended for server deployments where storage, embedder, and evolution strategy are specified in a single file.

```python
ctx = ContextSeek.from_runtime_config("contextseek.runtime.json")
```

---

## Write

### `add(content, *, scope, source, source_type=..., tags=None, confidence=None, stage=None, stability=None, links=None, check_conflicts=True) → ContextItem`

Write a new `ContextItem`. This is the only write path.

On `add()`, ContextSeek:
1. Builds `Provenance` from `source` and `source_type`
2. Infers `stage` and `stability` (or uses overrides)
3. Detects exact duplicates (raises `ValueError`) and near-conflicts (tags item)
4. Generates L2 `abstract` and L1 `summary` if a `Summarizer` is configured
5. Computes embedding of L2 (or L0 fallback) if an `Embedder` is configured
6. Persists and emits an audit record

| Parameter | Default | Description |
|-----------|---------|-------------|
| `content` | required | Text or JSON-serializable dict payload (L0 body) |
| `scope` | required | Tenant/project/subject path, e.g. `"acme/bot/user_42"` |
| `source` | required | Source identifier: URL, user ID, trace ID, etc. |
| `source_type` | `SourceType.human_input` | How data entered the system — affects stage inference |
| `tags` | `None` | List of tag strings for retrieval filtering |
| `confidence` | `None` | Override provenance confidence (0.0–1.0); inferred if `None` |
| `stage` | `None` | Override `Stage`. Inferred from `source_type` if `None` |
| `stability` | `None` | Override `Stability`. Inferred from `stage` if `None` |
| `links` | `None` | List of `Link` objects to other item IDs |
| `check_conflicts` | `True` | Run deduplication and conflict detection on write |

**Returns:** The created `ContextItem` (id, ref, stage, provenance, etc. are populated).

**Raises:** `ValueError` if an exact duplicate already exists in the scope.

```python
from contextseek.domain.provenance import SourceType
from contextseek.domain.stages import Stage

item = ctx.add(
    "Always run integration tests before production deploy.",
    scope="acme/platform/team-sre",
    source="runbook/deploy-v4",
    source_type=SourceType.document,
    tags=["deploy", "prod"],
    stage=Stage.knowledge,
)
print(item.id, item.stage)
```

### `plug(source, *, scope=None) → None`

Attach a `DataPlug` and ingest all its events into the store.

```python
from contextseek.plugs import RAGPlug

rag_plug = RAGPlug(results=my_rag_results)
ctx.plug(rag_plug, scope="acme/kb/general")
```

See [DataPlugs](../guides/integrations/dataplugs.md) for available plug types.

---

## Read

### `retrieve(query, *, scope, k=10, full=False, stage=None, tags=None, tag_match='all', filters=None, include_deleted=False) → RetrieveResponse`

Ranked semantic search. Returns a `RetrieveResponse` iterable of `SearchHit` objects.

By default returns **L1 summaries** (token-efficient). Pass `full=True` to receive L0 bodies directly. To upgrade selected hits lazily, call `expand()`.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `query` | required | Natural-language query string |
| `scope` | required | Scope prefix — searches this prefix and all sub-scopes |
| `k` | `10` | Maximum hits to return |
| `full` | `False` | `True` → L0 bodies; `False` → L1 summaries (call `expand()` for L0) |
| `stage` | `None` | Filter by `Stage` enum value |
| `tags` | `None` | Tag filter list; matching controlled by `tag_match` |
| `tag_match` | `'all'` | `'all'` — item must have every tag (AND); `'any'` — at least one tag (OR) |
| `filters` | `None` | Dict bag: may include `stage`, `tags`, `tag_match`, `min_confidence` |
| `include_deleted` | `False` | Whether soft-deleted items appear in results |

**Returns:** `RetrieveResponse` — iterable as `for hit in response`. Each `hit` has:
- `hit.item` — `ContextItem` (with `summary` populated when `full=False`)
- `hit.score` — float relevance score
- `hit.layer` — `"summary"` or `"full"`

```python
response = ctx.retrieve("distributed database", scope="acme/db/engineer", k=5)
for hit in response:
    text = hit.item.summary or hit.item.content_text
    print(f"[{hit.item.stage.value}] score={hit.score:.2f} | {text[:80]}")
```

### `expand(hits) → list[ContextItem]`

Upgrade a list of `SearchHit` rows to L0 full text. Scope is derived from `hit.item.scope` — no extra argument required.

```python
response = ctx.retrieve("query", scope="acme/bot")
interesting = [h for h in response if h.score > 0.7]
full_items = ctx.expand(interesting)
for item in full_items:
    print(item.content)
```

### `expand_by_ids(ids, scope) → list[ContextItem]`

Same as `expand()` but accepts bare item ID strings. Useful when bridging from HTTP or MCP where `SearchHit` objects are not available.

```python
full_items = ctx.expand_by_ids(["abc123", "def456"], scope="acme/bot")
```

### `items(*, scope, stage=None) → list[ContextItem]`

Enumerate all items in a scope, sorted by `created_at` ascending. Not query-ranked — use `retrieve()` for ranked search.

```python
all_items = ctx.items(scope="acme/bot/user_42")
knowledge_items = ctx.items(scope="acme/bot", stage=Stage.knowledge)
```

### `tools() → list[ToolSpec]`

Return `retrieve` and `expand` tool specs for direct registration with LLM agents.

```python
for spec in ctx.tools():
    openai_tool = spec.to_openai()
    anthropic_tool = spec.to_anthropic()
```

---

## Scope analysis

### `scope_tree(root=None) → ScopeTree`

Return a hierarchical view of all scopes under `root`, with item/knowledge/skill counts per leaf scope. When `root=None`, the entire store is traversed.

```python
tree = ctx.scope_tree(root="acme")
tree.print()
# acme/
#   payment-service/
#     refund/              (142 items, 38 knowledge, 5 skills)
#     run/run_20260522_001/ (891 items, 12 knowledge)
#   shared/
#     knowledge/           (203 items, 87 knowledge, 14 skills)
```

`ScopeTree` fields:

| Field | Description |
|-------|-------------|
| `nodes` | Top-level `ScopeNode` dict, keyed by scope segment name |

`ScopeNode` fields:

| Field | Description |
|-------|-------------|
| `name` | Segment name at this level |
| `full_path` | Full scope path string |
| `item_count` | Total items in this scope |
| `knowledge_count` | Items with `stage=knowledge` |
| `skill_count` | Items with `stage=skill` |
| `children` | Nested child nodes |

> **Performance note:** `scope_tree()` enumerates all refs under the given prefix. For large scopes this may be slow — use it for debug sessions and dashboards, not hot paths.

### `scope_stats(scope) → ScopeStats`

Return aggregate statistics for a single scope (exact match, not prefix).

```python
stats = ctx.scope_stats("acme/payment-service/refund")
print(f"items: {stats.item_count}")
print(f"stage dist: {stats.stage_distribution}")  # {"raw": 5, "knowledge": 3, ...}
print(f"avg confidence: {stats.avg_confidence:.2f}")
print(f"last write: {stats.last_write}")
```

`ScopeStats` fields:

| Field | Type | Description |
|-------|------|-------------|
| `scope` | `str` | Scope path |
| `item_count` | `int` | Total non-deleted items |
| `stage_distribution` | `dict[str, int]` | Count per stage (string keys, e.g. `"raw"`, `"knowledge"`) |
| `avg_confidence` | `float` | Mean provenance confidence across all items |
| `last_write` | `datetime \| None` | `created_at` of the newest item; `None` if scope is empty |
| `gap_count` | `int` | Detected unfilled knowledge gaps (reserved; populated by GapDetector) |

---

## Provenance & Audit

### `upstream(ref, *, scope) → list[ContextItem]`

Walk `derived_from` and `supported_by` links to collect all upstream items that contributed to the given item.

```python
sources = ctx.upstream(item.ref, scope="acme/bot")
```

### `evidence_chain(ref, *, scope, max_depth=10) → EvidenceChain`

Build the full evidence chain DAG for an item. Returns an `EvidenceChain` with:
- `nodes` — all items in the chain
- `overall_confidence` — Noisy-OR propagated confidence
- `conflicts` — detected contradictions in the chain
- `critical_path` — highest-weight path to root

```python
chain = ctx.evidence_chain(item.ref, scope="acme/bot")
print(f"confidence={chain.overall_confidence:.2f}, nodes={len(chain.nodes)}")
```

**Raises:** `ValueError` if the item does not exist.

### `chain_confidence(ref, *, scope) → float`

Quick confidence lookup without the full DAG. Returns effective confidence (0.0–1.0).

```python
conf = ctx.chain_confidence(item.ref, scope="acme/bot")
```

### `tag(*, actor=None, request=None, source=None, reason=None)`

Context manager — attaches audit metadata to every audited call inside the `with` block.

```python
with ctx.tag(actor={"user": "alice", "role": "admin"}, reason="weekly_review"):
    ctx.retrieve("query", scope="acme/bot")
    ctx.add("new fact", scope="acme/bot", source="manual")
```

Fields are merged into each `AuditRecord` emitted during the block.

---

## Evolution & Maintenance

### `compact(*, scope, dry_run=False) → CompactReport`

Run the evolution pipeline on a scope. With `EVOLUTION_ENABLED=true`, drives the full pipeline: deduplication → extraction → semantic merge → distillation → archival. Without it, performs hash-based deduplication only.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `scope` | required | Scope to compact |
| `dry_run` | `False` | Compute the report without writing any changes |

**Returns:** `CompactReport` with `merged_count`, `archived_count`, `evolved_count`.

```python
report = ctx.compact(scope="acme/bot/user_42")
print(f"merged={report.merged_count}, archived={report.archived_count}")

# Dry run first
preview = ctx.compact(scope="acme/bot/user_42", dry_run=True)
```

### `dream(*, scope, dry_run=False) → DreamReport`

Trigger a dream cycle: consolidation (find recurring patterns across items, synthesize into new extracted items) and divergence (generate hypotheses that bridge clusters). Dream items are low-confidence `extracted` items that decay unless reinforced by `feedback()`.

```python
report = ctx.dream(scope="acme/bot/user_42")
print(f"generated {report.total_dream_items} dream items")
```

Requires `DREAM_LLM_ENABLED=true` for LLM-assisted consolidation. Without it, dream runs a keyword-overlap heuristic only.

### `overview(*, scope) → EvolutionReport`

Read-only scope summary: stage counts and pending evolution hints (items ready for extraction, convergence, or distillation).

```python
report = ctx.overview(scope="acme/bot")
print(report)
```

### `feedback(ref, *, scope, score, reason="") → None`

Apply relevance feedback to an item. Adjusts `relevance_boost` for future retrievals and sends evolution priority signals.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ref` | required | Full URI reference of the item |
| `scope` | required | Scope the item belongs to |
| `score` | required | Delta in range −1.0 to 1.0. Positive = more relevant |
| `reason` | `""` | Optional text reason (parsed by LLM if `EVOLUTION_LLM_FEEDBACK_ENABLED=true`) |

Effects of `score`:
- Positive: raises `relevance_boost` (clamped 0.1–5.0); increments `access_count`; tags item `"evolution_candidate"` when boost ≥ 2.0
- Negative (< 0): lowers `relevance_boost`; tags `raw`/`extracted` items `"needs_review"`; strong negative (≤ −0.5) decays `importance`

```python
ctx.feedback(hit.item.ref, scope="acme/bot", score=0.8, reason="exactly what I needed")
ctx.feedback(hit.item.ref, scope="acme/bot", score=-0.5, reason="outdated")
```

---

## Delete & Forget

### `forget(ref, *, scope, reason, propagate=True) → None`

Soft-delete an item. The item remains in storage but `searchable=False` and `is_deleted=True`. Items are hidden from `retrieve()` unless `include_deleted=True`.

With `propagate=True`, items that derived exclusively from the forgotten item are also soft-deleted.

```python
ctx.forget(item.ref, scope="acme/bot", reason="outdated after policy change")
```

### `delete(ref, *, scope, reason, propagate=True) → None`

Hard-remove an item from storage. Cannot be undone. Use `forget()` when auditability matters.

```python
ctx.delete(item.ref, scope="acme/bot", reason="GDPR erasure request")
```

---

## Skills

### `skills(scope, *, skill_type=None, query=None, k=50) → list[ContextItem]`

List or search items at `Stage.skill`. Optionally filter by `skill_type` (`"prompt"`, `"tool"`, `"mcp"`) or provide a semantic `query`.

```python
all_skills = ctx.skills("acme/bot")
tools_only = ctx.skills("acme/bot", skill_type="tool")
relevant = ctx.skills("acme/bot", query="database backup", k=10)
```

### `skill_tools(scope, *, fmt="openai", query=None, k=20) → list[dict]`

Export tool/MCP skills as LLM-compatible tool definitions. The returned list is ready to pass directly as the `tools` parameter in LLM API calls.

```python
tools = ctx.skill_tools("acme/bot", fmt="openai")
# Pass directly to OpenAI
openai_client.chat.completions.create(..., tools=tools)

tools = ctx.skill_tools("acme/bot", fmt="anthropic")
# Pass directly to Anthropic
anthropic_client.messages.create(..., tools=tools)
```

Supported `fmt` values: `"openai"`, `"anthropic"`, `"mcp"`.

### `skill_context(scope, *, query=None, k=5) → str`

Retrieve top prompt-type skills and return them as a formatted context string, ready to inject into a system prompt.

```python
system_prompt = ctx.skill_context("acme/bot", query="customer support")
```

---

## Versioning

### `pin(version) → ContextSeek`

Return a copy of the client with a different `policy_version` label. The copy shares the same adapter and strategy; only the audit `policy_version` field changes. Useful for canary/A-B deployment labeling.

```python
canary_ctx = ctx.pin("v2-canary")
with canary_ctx.tag(actor={"experiment": "canary"}):
    canary_ctx.retrieve("query", scope="acme/bot")
```

---

## Return types

| Type | Description |
|------|-------------|
| `ContextItem` | Core domain object — see [Core concepts](../concepts/context-model.md) |
| `RetrieveResponse` | Iterable of `SearchHit`; also has `.meta` (layer, hint) |
| `SearchHit` | `.item`, `.score`, `.layer` |
| `CompactReport` | `.merged_count`, `.archived_count`, `.evolved_count`, `.details` |
| `DreamReport` | `.consolidation`, `.divergence`, `.total_dream_items` |
| `EvolutionReport` | Stage counts + pending hints |
| `EvidenceChain` | `.nodes`, `.overall_confidence`, `.conflicts`, `.critical_path` |
| `ScopeTree` | `.nodes` (`ScopeNode` tree); `.print()` renders an annotated directory tree |
| `ScopeStats` | `.item_count`, `.stage_distribution`, `.avg_confidence`, `.last_write` |
| `ToolSpec` | `.to_openai()`, `.to_anthropic()` |

---

[← Guides](../guides/write-and-retrieve.md) · [Settings reference](settings.md) · [Examples](examples.md)

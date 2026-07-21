# API 参考

所有公共方法均在同一个 `ContextSeek` 对象上。构造一次实例后，所有操作共用同一个 adapter、audit log 与 strategy。

```python
from contextseek import ContextSeek
ctx = ContextSeek.from_settings()
```

---

## 构造

### `ContextSeek.from_settings(settings=None, *, _version="default")`

从环境变量、`.env` 文件或显式 `ContextSeekSettings` 对象构造实例。

```python
# 自动读取 .env / 环境变量
ctx = ContextSeek.from_settings()

# 显式传入 settings
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

详见[配置](../getting-started/configuration.md)。

### `ContextSeek.from_runtime_config(path=None)`

从 JSON/YAML 运行时配置文件构造，适用于服务端部署场景。

```python
ctx = ContextSeek.from_runtime_config("contextseek.runtime.json")
```

---

## 写入

### `add(content, *, scope, source, source_type=..., tags=None, confidence=None, stage=None, stability=None, links=None, check_conflicts=True) → ContextItem`

写入一条新 `ContextItem`，这是唯一的写入路径。

`add()` 依次执行：
1. 根据 `source` 和 `source_type` 构建 `Provenance`
2. 推断 `stage` 和 `stability`（或使用覆盖值）
3. 精确重复检测（抛出 `ValueError`）与近似冲突检测（给条目打标签）
4. 若配置了 `Summarizer`，生成 L2 `abstract` 和 L1 `summary`
5. 若配置了 `Embedder`，对 L2（或 L0 兜底）计算向量
6. 持久化并发出审计记录

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `content` | 必填 | 文本或可序列化 dict（L0 正文） |
| `scope` | 必填 | 租户/项目/主题路径，如 `"acme/bot/user_42"` |
| `source` | 必填 | 来源标识：URL、用户 ID、Trace ID 等 |
| `source_type` | `SourceType.human_input` | 数据入口类型，影响 stage 推断 |
| `tags` | `None` | 检索过滤用标签列表 |
| `confidence` | `None` | 覆盖 Provenance 置信度（0.0–1.0） |
| `stage` | `None` | 覆盖 Stage，默认由 `source_type` 推断 |
| `stability` | `None` | 覆盖 Stability，默认由 `stage` 推断 |
| `links` | `None` | 指向其他条目的 `Link` 列表 |
| `check_conflicts` | `True` | 写入时执行去重与冲突检测 |

**返回：** 已创建的 `ContextItem`（id、ref、stage、provenance 均已填充）。

**异常：** 若 scope 内已存在精确重复条目，抛出 `ValueError`。

```python
from contextseek.domain.provenance import SourceType
from contextseek.domain.stages import Stage

item = ctx.add(
    "生产部署前必须通过集成测试。",
    scope="acme/platform/team-sre",
    source="runbook/deploy-v4",
    source_type=SourceType.document,
    tags=["deploy", "prod"],
    stage=Stage.knowledge,
)
print(item.id, item.stage)
```

### `plug(source, *, scope=None) → None`

挂载 `DataPlug` 并将其所有事件批量写入存储。

```python
from contextseek.plugs import RAGPlug

ctx.plug(RAGPlug(results=my_rag_results), scope="acme/kb/general")
```

详见 [DataPlug 指南](../guides/integrations/dataplugs.md)。

---

## 读取

### `retrieve(query, *, scope, k=10, full=False, stage=None, tags=None, tag_match='all', filters=None, include_deleted=False) → RetrieveResponse`

排名语义检索，返回 `SearchHit` 的可迭代 `RetrieveResponse`。

默认返回 **L1 摘要**（省 token）。传入 `full=True` 直接获取 L0 正文；或先召回后选择性调用 `expand()` 升档。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `query` | 必填 | 自然语言查询 |
| `scope` | 必填 | Scope 前缀，检索该前缀及所有子 scope |
| `k` | `10` | 最多返回命中数 |
| `full` | `False` | `True` 返回 L0 正文；`False` 返回 L1 摘要 |
| `stage` | `None` | 按 Stage 枚举值过滤 |
| `tags` | `None` | 标签过滤列表，匹配方式由 `tag_match` 控制 |
| `tag_match` | `'all'` | `'all'`：条目必须包含所有标签（AND）；`'any'`：至少匹配一个标签（OR） |
| `filters` | `None` | 字典包：可含 `stage`、`tags`、`tag_match`、`min_confidence` |
| `include_deleted` | `False` | 是否包含软删除条目 |

**返回：** `RetrieveResponse` — 可用 `for hit in response` 遍历。每个 `hit`：
- `hit.item` — `ContextItem`（`full=False` 时 `summary` 已填充）
- `hit.score` — float 相关性分数
- `hit.layer` — `"summary"` 或 `"full"`

```python
response = ctx.retrieve("分布式数据库", scope="acme/db/engineer", k=5)
for hit in response:
    text = hit.item.summary or hit.item.content_text
    print(f"[{hit.item.stage.value}] score={hit.score:.2f} | {text[:80]}")
```

### `expand(hits) → list[ContextItem]`

将 `SearchHit` 列表升档为 L0 完整正文。Scope 由 `hit.item.scope` 自动推断，无需额外参数。

```python
response = ctx.retrieve("query", scope="acme/bot")
interesting = [h for h in response if h.score > 0.7]
full_items = ctx.expand(interesting)
```

### `expand_by_ids(ids, scope) → list[ContextItem]`

与 `expand()` 相同，但接受裸条目 ID 字符串。适用于 HTTP / MCP 桥接场景。

```python
full_items = ctx.expand_by_ids(["abc123", "def456"], scope="acme/bot")
```

### `items(*, scope, stage=None) → list[ContextItem]`

按 `created_at` 升序枚举 scope 内所有条目，不做排名。需要排名检索请用 `retrieve()`。

```python
all_items = ctx.items(scope="acme/bot/user_42")
knowledge_items = ctx.items(scope="acme/bot", stage=Stage.knowledge)
```

### `tools() → list[ToolSpec]`

返回 `retrieve` 和 `expand` 的工具描述，可直接注册到 LLM Agent。

```python
for spec in ctx.tools():
    openai_tool = spec.to_openai()
    anthropic_tool = spec.to_anthropic()
```

---

## Scope 分析

### `scope_tree(root=None) → ScopeTree`

返回 `root` 前缀下所有 scope 的层级视图，含各叶节点的 item/knowledge/skill 计数。`root=None` 时遍历全部 scope。

```python
from contextseek import ScopeBuilder

tree = ctx.scope_tree(root="acme")
tree.print()
# acme/
#   payment-service/
#     refund/   (142 items, 38 knowledge, 5 skills)
#     run/run_20260522_001/    (891 items, 12 knowledge)
#   shared/
#     knowledge/ (203 items, 87 knowledge, 14 skills)
```

`ScopeTree` 字段：

| 字段 | 说明 |
|------|------|
| `nodes` | 顶层 `ScopeNode` 字典（按 scope 段名索引） |

`ScopeNode` 字段：

| 字段 | 说明 |
|------|------|
| `name` | 当前段名称 |
| `full_path` | 完整 scope 路径 |
| `item_count` | 该 scope 下的总条目数 |
| `knowledge_count` | `stage=knowledge` 的条目数 |
| `skill_count` | `stage=skill` 的条目数 |
| `children` | 子节点字典 |

> **注意：** `scope_tree()` 会枚举指定前缀下的所有 ref，scope 内条目数量大时有一定延迟，建议低频调用（如调试、监控面板）。

### `scope_stats(scope) → ScopeStats`

返回单个 scope 的聚合统计。

| 参数 | 说明 |
|------|------|
| `scope` | 精确 scope 字符串（非前缀匹配） |

```python
stats = ctx.scope_stats("acme/payment-service/refund")
print(f"条目数: {stats.item_count}")
print(f"stage 分布: {stats.stage_distribution}")  # {"raw": 5, "knowledge": 3, ...}
print(f"平均置信度: {stats.avg_confidence:.2f}")
print(f"最后写入: {stats.last_write}")
```

`ScopeStats` 字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `scope` | `str` | scope 路径 |
| `item_count` | `int` | 总条目数 |
| `stage_distribution` | `dict[str, int]` | 各 stage（字符串键）的条目计数 |
| `avg_confidence` | `float` | 所有条目的平均置信度 |
| `last_write` | `datetime \| None` | 最新条目的 `created_at`；scope 为空时为 `None` |
| `gap_count` | `int` | 已检测的未填充盲点数（预留，GapDetector 实现后填充） |

---

## 溯源与审计

### `upstream(ref, *, scope) → list[ContextItem]`

沿 `derived_from` 和 `supported_by` 链接，向上收集所有上游条目。

```python
sources = ctx.upstream(item.ref, scope="acme/bot")
```

### `evidence_chain(ref, *, scope, max_depth=10) → EvidenceChain`

构建完整的证据链 DAG。返回 `EvidenceChain`，包含：
- `nodes` — 链中所有条目
- `overall_confidence` — Noisy-OR 传播置信度
- `conflicts` — 链中检测到的矛盾
- `critical_path` — 到根节点的最高权重路径

```python
chain = ctx.evidence_chain(item.ref, scope="acme/bot")
print(f"confidence={chain.overall_confidence:.2f}, nodes={len(chain.nodes)}")
```

**异常：** 条目不存在时抛出 `ValueError`。

### `chain_confidence(ref, *, scope) → float`

不构建完整 DAG，直接返回传播置信度（0.0–1.0）。

```python
conf = ctx.chain_confidence(item.ref, scope="acme/bot")
```

### `tag(*, actor=None, request=None, source=None, reason=None)`

上下文管理器 — 将审计元数据附加到 `with` 块内所有被审计操作的记录中。

```python
with ctx.tag(actor={"user": "alice", "role": "admin"}, reason="weekly_review"):
    ctx.retrieve("query", scope="acme/bot")
    ctx.add("新事实", scope="acme/bot", source="manual")
```

---

## 演化与维护

### `compact(*, scope, dry_run=False) → CompactReport`

对 scope 运行演化流水线。`EVOLUTION_ENABLED=true` 时执行完整流水线（去重→提取→语义合并→蒸馏→归档）；否则仅做哈希去重。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `scope` | 必填 | 要压缩的 scope |
| `dry_run` | `False` | 计算报告但不写入任何变更 |

**返回：** `CompactReport`，含 `merged_count`、`archived_count`、`evolved_count`。

```python
report = ctx.compact(scope="acme/bot/user_42")
print(f"merged={report.merged_count}, archived={report.archived_count}")

# 先预演
preview = ctx.compact(scope="acme/bot/user_42", dry_run=True)
```

### `dream(*, scope, dry_run=False) → DreamReport`

触发 dream 周期：整合（在 scope 内发现模式，合成新 `extracted` 条目）和发散（生成跨簇假设）。Dream 条目置信度低，会自然衰减，除非被 `feedback()` 强化。

```python
report = ctx.dream(scope="acme/bot/user_42")
print(f"生成 {report.total_dream_items} 条 dream 条目")
```

`DREAM_LLM_ENABLED=true` 启用 LLM 辅助合成；否则仅用关键词重叠启发式。

### `overview(*, scope) → EvolutionReport`

只读 scope 汇总：各 stage 计数 + 演化建议（待提取、待收敛、待蒸馏的条目数量）。

```python
report = ctx.overview(scope="acme/bot")
print(report)
```

### `feedback(ref, *, scope, score, reason="") → None`

向条目施加相关性反馈，调整检索排名权重并影响演化优先级。

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ref` | 必填 | 条目完整 URI 引用 |
| `scope` | 必填 | 条目所属 scope |
| `score` | 必填 | 反馈分值，范围 −1.0 到 1.0 |
| `reason` | `""` | 可选原因文本 |

| 分值范围 | 效果 |
|----------|------|
| `> 0` | 提升 `relevance_boost`（上限 5.0）；`access_count` +1；boost ≥ 2.0 时打 `"evolution_candidate"` |
| `< 0` | 降低 `relevance_boost`（下限 0.1）；`raw`/`extracted` 条目打 `"needs_review"`；≤ −0.5 衰减 `importance` |

```python
ctx.feedback(hit.item.ref, scope="acme/bot", score=0.8, reason="正好是我需要的")
ctx.feedback(hit.item.ref, scope="acme/bot", score=-0.5, reason="已过时")
```

---

## 删除与遗忘

### `forget(ref, *, scope, reason, propagate=True) → None`

软删除条目。条目保留在存储中，但 `searchable=False`、`is_deleted=True`，检索时不可见（除非 `include_deleted=True`）。`propagate=True` 时，仅依赖该条目的衍生条目也会被软删除。

```python
ctx.forget(item.ref, scope="acme/bot", reason="策略更新后已过时")
```

### `delete(ref, *, scope, reason, propagate=True) → None`

从存储中物理删除条目，不可恢复。需保留审计记录时请用 `forget()`。

```python
ctx.delete(item.ref, scope="acme/bot", reason="GDPR 数据删除请求")
```

---

## 技能

### `skills(scope, *, skill_type=None, query=None, k=50) → list[ContextItem]`

列举或检索 `Stage.skill` 条目。可按 `skill_type`（`"prompt"`、`"tool"`、`"mcp"`）过滤，也可提供语义查询。

```python
all_skills = ctx.skills("acme/bot")
tools_only = ctx.skills("acme/bot", skill_type="tool")
relevant = ctx.skills("acme/bot", query="数据库备份", k=10)
```

### `skill_tools(scope, *, fmt="openai", query=None, k=20) → list[dict]`

将 tool/MCP 类技能导出为 LLM 兼容的工具定义，可直接传入 LLM API 的 `tools` 参数。

```python
tools = ctx.skill_tools("acme/bot", fmt="openai")
openai_client.chat.completions.create(..., tools=tools)

tools = ctx.skill_tools("acme/bot", fmt="anthropic")
anthropic_client.messages.create(..., tools=tools)
```

支持的 `fmt`：`"openai"`、`"anthropic"`、`"mcp"`。

### `skill_context(scope, *, query=None, k=5) → str`

检索 top-k prompt 类技能，返回格式化上下文字符串，可直接注入 system prompt。

```python
system_prompt = ctx.skill_context("acme/bot", query="客户支持")
```

---

## 版本标签

### `pin(version) → ContextSeek`

返回带有不同 `policy_version` 标签的客户端副本。副本共享同一 adapter 和 strategy，仅审计记录的 `policy_version` 字段不同。适用于金丝雀/A-B 实验标注。

```python
canary_ctx = ctx.pin("v2-canary")
with canary_ctx.tag(actor={"experiment": "canary"}):
    canary_ctx.retrieve("query", scope="acme/bot")
```

---

## 返回类型

| 类型 | 说明 |
|------|------|
| `ContextItem` | 核心域对象——见[核心概念](../concepts/context-model.md) |
| `RetrieveResponse` | 可迭代 `SearchHit`；含 `.meta`（layer、hint） |
| `SearchHit` | `.item`、`.score`、`.layer` |
| `CompactReport` | `.merged_count`、`.archived_count`、`.evolved_count`、`.details` |
| `DreamReport` | `.consolidation`、`.divergence`、`.total_dream_items` |
| `EvolutionReport` | 各 stage 计数 + 演化建议 |
| `EvidenceChain` | `.nodes`、`.overall_confidence`、`.conflicts`、`.critical_path` |
| `ScopeTree` | `.nodes`（`ScopeNode` 树）；`.print()` 输出可视树 |
| `ScopeStats` | `.item_count`、`.stage_distribution`、`.avg_confidence`、`.last_write` |
| `ToolSpec` | `.to_openai()`、`.to_anthropic()` |

---

[← 使用指南](../guides/write-and-retrieve.md) · [配置项参考](settings.md) · [示例索引](examples.md)

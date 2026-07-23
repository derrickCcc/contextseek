# Examples

示例按依赖复杂度分为三组，建议按顺序探索。

## 零外部依赖示例

以下示例仅依赖项目核心依赖，使用 FileBackend 或内存后端，无需安装 OceanBase、配置 LLM API key 或任何外部服务。确保已 `uv sync` 后直接运行即可。

### 推荐首试

| 示例 | 一句话说明 | 命令 |
|---|---|---|
| `basic/pipeline_file.py` | 最简 FileBackend 写入 + 关键词检索 | `uv run python examples/basic/pipeline_file.py` |
| `advanced/research_agent.py` | 完整链路：写入→链接→检索→expand→Trace→compact→路由→skill | `uv run python examples/advanced/research_agent.py` |

### 全部零依赖示例

| 示例 | 功能亮点 | 运行命令 | 预期输出 |
|---|---|---|---|
| `basic/pipeline_file.py` | FileBackend 落盘 + 关键词子串检索 + Orchestrator 底层调用 | `uv run python examples/basic/pipeline_file.py` | 打印 3 条写入 item、3 组 query 的命中 ID、磁盘文件数 |
| `advanced/research_agent.py` | 多源写入、链接、检索/expand、Trace 导出、compact、策略路由、skill 导出 | `uv run python examples/advanced/research_agent.py` | 10 步逐步输出，最终打印 scope 总 item 数和磁盘文件数 |
| `advanced/evidence_chain.py` | 证据链 DAG：upstream / evidence_chain / chain_confidence | `uv run python examples/advanced/evidence_chain.py` | SRE 故障场景 5 步输出，含 DAG 节点数、置信度传播、冲突检测、关键路径 |
| `advanced/powermem_minimal.py` | PowerMem 最小集成（内置 mock，无需安装 powermem） | `uv run python examples/advanced/powermem_minimal.py` | 打印 plugged 记忆数 + 检索命中列表 |
| `advanced/self_evolution_demo.py` | 自演进四特性：冲突解决 + 双时效 + 效用反馈 + 失败反思 | `uv run python examples/advanced/self_evolution_demo.py` | 4 个分节输出，分别展示冲突退役、drift 隔离、boost 变化、pitfall 规则 |
| `advanced/powermem_plug.py` | PowerMem DataPlug（默认 mock 模式，可选真实 powermem） | `uv run python examples/advanced/powermem_plug.py` | 5 步输出：导入→挂载→追加→统一检索→溯源对比 |
| `demo/seed.py` | demo 种子脚本（FileBackend，写入合规 lesson + trace） | `uv run python examples/demo/seed.py` | 打印 seeded lesson 列表和完成提示 |
| `full_pipeline_file.py` | 兼容 shim，导出 `basic/pipeline_file.py` 的公开 API（不自动执行 `main()`） | `uv run python -c "from examples.full_pipeline_file import run_file_backend_demo; run_file_backend_demo(...)"` | 无直接输出（仅加载模块）；调用其导出函数可复现 `pipeline_file.py` 的行为 |

> **注意**：以下示例**不是**零依赖：
> - `basic/langchain_deepagents_example.py` — 需要 `OPENAI_API_KEY` + `contextseek[langchain]` extras
> - `basic/langchain.py` — 需要 `contextseek[langchain]` extras
> - `basic/pipeline_ob.py` — 需要 OceanBase + embeddings provider
> - `advanced/llm_full_pipeline_ob.py` — 需要 OceanBase + LLM API
> - `gis/` 下所有示例 — 需要 OceanBase >= 4.2.2 且 `GEO_ENABLED=true`

## [basic/](basic/) — 入门示例

依赖最小，适合初次了解 ContextSeek。

| 文件 | 后端 | 说明 |
|---|---|---|
| `pipeline_file.py` | FileBackend（无需外部服务） | 本地文件后端，关键词检索 |
| `pipeline_ob.py` | OceanBase | 向量 + 全文混合检索 |
| `langchain.py` | FileBackend（需 `langchain` extras） | LangChain Memory / Retriever 桥接 |
| `langchain_deepagents_example.py` | FileBackend + OpenAI API | LangChain + DeepAgents + ContextSeek 的真实集成示例（需 `OPENAI_API_KEY`） |

```bash
uv run python examples/basic/pipeline_file.py  # 零外部依赖，推荐首选
```

## [advanced/](advanced/) — 完整能力展示

涵盖 LLM 集成、演进流水线、DataPlug 扩展。

| 文件 | 依赖 | 说明 |
|---|---|---|
| `research_agent.py` | 仅项目本身 | 所有核心功能综合演示（推荐） |
| `evidence_chain.py` | 仅项目本身 | 证据链溯源：`upstream` / `evidence_chain` / `chain_confidence` |
| `llm_full_pipeline_ob.py` | OB + LLM API | Phase 1/2/3 完整 LLM 流水线 |
| `powermem_minimal.py` | 仅项目本身 | PowerMem 最小集成路径（~50 行） |
| `powermem_plug.py` | 可选 powermem | PowerMem DataPlug 完整演示 |

```bash
uv run python examples/advanced/research_agent.py  # 推荐：零外部依赖的完整演示
```

## [gis/](gis/) — 地理空间场景

需要 OceanBase >= 4.2.2（或 seekdb）且 `GEO_ENABLED=true`。

| 文件 | 场景 |
|---|---|
| `poi_search.py` | 地图 POI 关键词 + 地理混合搜索 |
| `ride_hailing.py` | 打车调度：司机 / 订单 / 热力区域 |
| `autonomous_driving.py` | 智能驾驶：HD 地图 / ODD / 道路事件 |

```bash
GEO_ENABLED=true uv run python examples/gis/poi_search.py
```

---

## HTTP API

启动 API 服务：

```bash
uvicorn contextseek.http.server:app --host 127.0.0.1 --port 8000 --reload
```

示例请求：

```bash
curl -X POST http://127.0.0.1:8000/add \
  -H "Content-Type: application/json" \
  -d '{"content": "hello", "scope": "t/p/u", "source": "curl"}'

curl -X POST http://127.0.0.1:8000/retrieve \
  -H "Content-Type: application/json" \
  -d '{"query": "hello", "scope": "t/p/u", "k": 5}'
```

端点：`/add`、`/retrieve`、`/expand`、`/compact`、`/forget`、`/delete`、`/health`

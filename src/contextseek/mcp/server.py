"""MCP-compatible server facade for ContextSeek tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contextseek.client.contextseek import ContextSeek
from contextseek.domain.serialization import serialize_context_item
from contextseek.domain.skill_ir import SkillIR

# Prefix marking a dynamically-mounted, evolution-produced skill tool, kept
# distinct from the static ``contextseek_*`` meta-tools.
_SKILL_TOOL_PREFIX = "skill_"


@dataclass
class ContextSeekMCPServer:
    """MCP tool server that exposes ContextSeek operations as tool calls."""

    client: ContextSeek

    @classmethod
    def with_default_client(cls) -> "ContextSeekMCPServer":
        """Create a server backed by the default ContextSeek settings."""
        return cls(client=ContextSeek.from_settings())

    @staticmethod
    def _is_publishable(item: Any) -> bool:
        """Whether a skill item may be surfaced on the live MCP runtime face."""
        ir = SkillIR.from_content(item.content)
        return ir.publish_status != "deprecated" and "needs_review" not in item.tags

    def list_prompts(self, scope: str) -> list[dict[str, Any]]:
        """Map prompt skills in *scope* to MCP ``prompts`` primitives.

        The natural mounting for prompt-type skills: an MCP client lists them as
        prompts and fetches one with ``prompts/get``. Deprecated / needs-review
        skills are withheld from the runtime face.
        """
        prompts: list[dict[str, Any]] = []
        for item in self.client.skills(scope, skill_type="prompt"):
            if not self._is_publishable(item):
                continue
            ir = SkillIR.from_content(item.content)
            prompts.append(
                {
                    "name": ir.skill_id or item.id,
                    "title": ir.name,
                    "description": ir.description,
                    "arguments": [],
                }
            )
        return prompts

    def get_prompt(self, scope: str, name: str) -> dict[str, Any]:
        """Return one prompt skill as an MCP ``prompts/get`` result.

        Serving a prompt is a consumption signal, so this records
        ``skill_selected`` before returning the rendered block.
        """
        from contextseek.domain.skill_executor import SkillExporter

        item = self.client._find_skill_item(scope, name)
        if item is None:
            ref = self.client.resolver.ref_for(scope, name)
            payload = self.client.adapter.read(ref)
            if payload is not None:
                from contextseek.domain.serialization import deserialize_context_item

                item = deserialize_context_item(payload)
        if item is None:
            return {"error": f"prompt skill not found: {name}"}
        if item.stage.value != "skill":
            return {"error": f"not a skill item: {name}"}
        if not self._is_publishable(item):
            return {"error": f"prompt skill not publishable: {name}"}

        ir = SkillIR.from_content(item.content)
        if ir.kind != "prompt":
            return {"error": f"not a prompt skill: {name}"}
        self.client.note_skill_selected(
            scope=scope,
            skill_id=ir.skill_id or item.id,
            target="mcp",
            reason="prompts/get",
        )
        text = SkillExporter().to_prompt_block(item)
        return {
            "description": ir.description,
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    def _live_skill_tools(self, scope: str) -> list[dict[str, Any]]:
        """tool/mcp skills in *scope* as live MCP tool definitions.

        The advertised ``inputSchema`` includes a required ``scope`` property so a
        standard MCP client that fills only the listed schema still sends what
        ``call_tool`` needs to locate the skill — keeping the list/call contract
        consistent (a client should never be able to see a tool it can't call).
        """
        tools: list[dict[str, Any]] = []
        for item in self.client.skills(scope):
            ir = SkillIR.from_content(item.content)
            if ir.kind not in ("tool", "mcp") or not self._is_publishable(item):
                continue
            tools.append(
                {
                    "name": f"{_SKILL_TOOL_PREFIX}{ir.skill_id or item.id}",
                    "description": ir.description,
                    "inputSchema": self._schema_with_scope(ir.parameters, scope),
                    "_skill_id": ir.skill_id or item.id,
                }
            )
        return tools

    @staticmethod
    def _schema_with_scope(parameters: dict[str, Any], scope: str) -> dict[str, Any]:
        """Return a copy of ``parameters`` with a required ``scope`` property."""
        schema = (
            dict(parameters) if isinstance(parameters, dict) else {"type": "object"}
        )
        schema.setdefault("type", "object")
        props = dict(schema.get("properties", {}))
        props["scope"] = {
            "type": "string",
            "description": "ContextSeek scope the skill lives in.",
            "default": scope,
        }
        schema["properties"] = props
        required = list(schema.get("required", []))
        if "scope" not in required:
            required.append("scope")
        schema["required"] = required
        return schema

    def list_tools(self, scope: str | None = None) -> list[dict[str, Any]]:
        """Return MCP tool definitions.

        When *scope* is given, evolution-produced tool/mcp skills in that scope
        are appended as live tools (discoverable + selectable by any MCP client)
        alongside the static ``contextseek_*`` meta-tools.
        """
        tools = self._meta_tools()
        if scope:
            tools.extend(self._live_skill_tools(scope))
        return tools

    def _meta_tools(self) -> list[dict[str, Any]]:
        """Return the static ContextSeek operation tools."""
        return [
            {
                "name": "contextseek_add",
                "description": "Add content to ContextSeek",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "content": {"type": "string", "required": True},
                    "source": {"type": "string", "default": "mcp"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
            },
            {
                "name": "contextseek_retrieve",
                "description": (
                    "Retrieve from ContextSeek: returns ranked SearchHits with L1 "
                    "summaries by default. Pass full=true for L0 complete content."
                ),
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "query": {"type": "string", "required": True},
                    "k": {"type": "integer", "default": 10},
                    "full": {"type": "boolean", "default": False},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "tag_match": {
                        "type": "string",
                        "enum": ["all", "any"],
                        "default": "all",
                    },
                    "include_expired": {"type": "boolean", "default": False},
                    "include_trace": {"type": "boolean", "default": False},
                },
            },
            {
                "name": "contextseek_expand",
                "description": "Upgrade SearchHits (by item id) to L0 full content",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "required": True,
                    },
                },
            },
            {
                "name": "contextseek_forget",
                "description": "Soft-delete a context item",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "item_id": {"type": "string", "required": True},
                    "reason": {"type": "string", "default": "mcp_forget"},
                },
            },
            {
                "name": "contextseek_delete",
                "description": "Permanently delete a context item from storage",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "item_id": {"type": "string", "required": True},
                    "reason": {"type": "string", "default": "mcp_delete"},
                    "propagate": {"type": "boolean", "default": True},
                },
            },
            {
                "name": "contextseek_compact",
                "description": "Run evolution/compaction on a scope",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                },
            },
            {
                "name": "contextseek_dream",
                "description": "Trigger a dream cycle (consolidation + divergence) on a scope",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "dry_run": {"type": "boolean", "default": False},
                },
            },
            {
                "name": "contextseek_overview",
                "description": "Read-only summary of items in a scope: stage distribution and evolution candidates",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                },
            },
            {
                "name": "contextseek_feedback",
                "description": "Apply relevance feedback to a ContextItem, adjusting its ranking weight",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "item_id": {"type": "string", "required": True},
                    "score": {"type": "number", "required": True},
                    "reason": {"type": "string", "default": ""},
                },
            },
            {
                "name": "contextseek_upstream",
                "description": "Walk derived_from and supported_by links to find upstream items",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "item_id": {"type": "string", "required": True},
                },
            },
            {
                "name": "contextseek_evidence_chain",
                "description": "Compute full evidence chain DAG with propagated confidence for an item",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "item_id": {"type": "string", "required": True},
                    "max_depth": {"type": "integer", "default": 10},
                },
            },
            {
                "name": "contextseek_chain_confidence",
                "description": "Quick propagated confidence lookup for a single item",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "item_id": {"type": "string", "required": True},
                },
            },
            {
                "name": "contextseek_skill_tools",
                "description": "Export tool/mcp skills as LLM tool definitions (OpenAI, Anthropic, or MCP format)",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "fmt": {"type": "string", "default": "openai"},
                    "query": {"type": "string", "default": None},
                    "k": {"type": "integer", "default": 20},
                },
            },
            {
                "name": "contextseek_skill_context",
                "description": "Render prompt skills as a Hermes-style system prompt block for injection",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "query": {"type": "string", "default": None},
                    "k": {"type": "integer", "default": 5},
                },
            },
            {
                "name": "contextseek_skill_feedback",
                "description": (
                    "Report a skill execution outcome back to ContextSeek, closing the "
                    "runtime loop: success raises the skill's ranking, repeated failure "
                    "deprecates it. Call after running a skill served via MCP."
                ),
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "skill_id": {"type": "string", "required": True},
                    "outcome": {"type": "string", "required": True},
                    "target": {"type": "string", "default": "mcp"},
                    "latency_ms": {"type": "number", "default": None},
                    "error_type": {"type": "string", "default": None},
                    "reason": {"type": "string", "default": ""},
                },
            },
            {
                "name": "contextseek_items",
                "description": "List all items in a scope, sorted by created_at",
                "parameters": {
                    "scope": {"type": "string", "required": True},
                    "stage": {"type": "string", "default": None},
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute an MCP tool call."""
        if name == "contextseek_add":
            item = self.client.add(
                arguments["content"],
                scope=arguments["scope"],
                source=arguments.get("source", "mcp"),
                tags=arguments.get("tags", []),
            )
            return {"id": item.id, "stage": item.stage.value}

        if name == "contextseek_retrieve":
            response = self.client.retrieve(
                arguments["query"],
                scope=arguments["scope"],
                k=arguments.get("k", 10),
                full=bool(arguments.get("full", False)),
                tags=arguments.get("tags") or None,
                tag_match=arguments.get("tag_match", "all"),
                include_expired=bool(arguments.get("include_expired", False)),
                with_trace=bool(arguments.get("include_trace", False)),
            )
            result = {
                "items": [
                    {
                        "id": h.item.id,
                        "scope": h.item.scope,
                        "score": h.score,
                        "layer": h.layer,
                        "summary": h.item.summary,
                        "content": h.item.content_text if h.layer == "full" else None,
                    }
                    for h in response
                ],
                "_meta": {
                    "layer": response.meta.layer,
                    "full_via": response.meta.full_via,
                    "hint": response.meta.hint,
                },
            }
            if response.trace is not None:
                result["_trace"] = response.trace.to_dict()
            return result

        if name == "contextseek_expand":
            scope = arguments["scope"]
            ids = arguments.get("ids", [])
            items = self.client.expand_by_ids(ids, scope)
            return {"items": [serialize_context_item(it) for it in items]}

        if name == "contextseek_forget":
            item_id = arguments["item_id"]
            ref = (
                item_id
                if str(item_id).startswith(self.client.resolver.scheme)
                else self.client.resolver.ref_for(arguments["scope"], item_id)
            )
            self.client.forget(
                ref,
                scope=arguments["scope"],
                reason=arguments.get("reason", "mcp_forget"),
            )
            return {"status": "ok"}

        if name == "contextseek_delete":
            item_id = arguments["item_id"]
            ref = (
                item_id
                if str(item_id).startswith(self.client.resolver.scheme)
                else self.client.resolver.ref_for(arguments["scope"], item_id)
            )
            self.client.delete(
                ref,
                scope=arguments["scope"],
                reason=arguments.get("reason", "mcp_delete"),
                propagate=bool(arguments.get("propagate", True)),
            )
            return {"status": "ok"}

        if name == "contextseek_compact":
            report = self.client.compact(scope=arguments["scope"])
            return {
                "merged": report.merged_count,
                "archived": report.archived_count,
                "evolved": report.evolved_count,
                "conflict_updated": report.conflict_updated_count,
                "conflict_drift": report.conflict_drift_count,
            }

        if name == "contextseek_dream":
            report = self.client.dream(
                scope=arguments["scope"],
                dry_run=bool(arguments.get("dry_run", False)),
            )
            return {
                "total_dream_items": report.total_dream_items,
                "consolidation_patterns": report.consolidation.patterns_found,
                "consolidation_items": len(report.consolidation.items),
                "divergence_items": len(report.divergence.items)
                if report.divergence
                else 0,
                "pitfall_items": len(report.pitfall.items) if report.pitfall else 0,
            }

        if name == "contextseek_overview":
            report = self.client.overview(scope=arguments["scope"])
            return {
                "total_items": report.total_items,
                "stage_distribution": report.stage_distribution,
                "pending_extraction": report.pending_extraction,
                "pending_convergence": report.pending_convergence,
                "distill_candidates": report.distill_candidates,
            }

        if name == "contextseek_feedback":
            scope = arguments["scope"]
            item_id = arguments["item_id"]
            ref = (
                item_id
                if str(item_id).startswith(self.client.resolver.scheme)
                else self.client.resolver.ref_for(scope, item_id)
            )
            self.client.feedback(
                ref,
                scope=scope,
                score=float(arguments["score"]),
                reason=arguments.get("reason", ""),
            )
            return {"status": "ok"}

        if name == "contextseek_upstream":
            scope = arguments["scope"]
            item_id = arguments["item_id"]
            ref = (
                item_id
                if str(item_id).startswith(self.client.resolver.scheme)
                else self.client.resolver.ref_for(scope, item_id)
            )
            chain = self.client.upstream(ref, scope=scope)
            return {"items": [serialize_context_item(it) for it in chain]}

        if name == "contextseek_evidence_chain":
            scope = arguments["scope"]
            item_id = arguments["item_id"]
            ref = (
                item_id
                if str(item_id).startswith(self.client.resolver.scheme)
                else self.client.resolver.ref_for(scope, item_id)
            )
            chain = self.client.evidence_chain(
                ref,
                scope=scope,
                max_depth=int(arguments.get("max_depth", 10)),
            )
            return chain.to_dict()

        if name == "contextseek_chain_confidence":
            scope = arguments["scope"]
            item_id = arguments["item_id"]
            ref = (
                item_id
                if str(item_id).startswith(self.client.resolver.scheme)
                else self.client.resolver.ref_for(scope, item_id)
            )
            confidence = self.client.chain_confidence(ref, scope=scope)
            return {"confidence": confidence}

        if name == "contextseek_skill_tools":
            scope = arguments["scope"]
            fmt = arguments.get("fmt", "openai")
            query = arguments.get("query") or None
            k = int(arguments.get("k", 20))
            tools = self.client.skill_tools(scope, fmt=fmt, query=query, k=k)
            return {"tools": tools}

        if name == "contextseek_skill_context":
            scope = arguments["scope"]
            query = arguments.get("query") or None
            k = int(arguments.get("k", 5))
            context = self.client.skill_context(scope, query=query, k=k)
            return {"context": context}

        if name == "contextseek_items":
            scope = arguments["scope"]
            stage_str = arguments.get("stage")
            from contextseek.domain.stages import Stage

            stage = Stage(stage_str) if stage_str else None
            result_items = self.client.items(scope=scope, stage=stage)
            return {"items": [serialize_context_item(it) for it in result_items]}

        if name == "contextseek_skill_feedback":
            return self.client.skill_feedback(
                scope=arguments["scope"],
                skill_id=arguments["skill_id"],
                outcome=str(arguments["outcome"]),
                target=arguments.get("target", "mcp"),
                latency_ms=arguments.get("latency_ms"),
                error_type=arguments.get("error_type"),
                reason=arguments.get("reason", ""),
            )

        # Live skill tool: ContextSeek stores definitions, not executors, so a
        # call records selection and returns the definition for the client's
        # runtime to execute (then report back via contextseek_skill_feedback).
        if name.startswith(_SKILL_TOOL_PREFIX):
            scope = arguments.get("scope", "")
            skill_id = name[len(_SKILL_TOOL_PREFIX) :]
            item = self.client._find_skill_item(scope, skill_id) if scope else None
            if item is None:
                return {"error": f"unknown skill tool: {name}"}
            if not self._is_publishable(item):
                return {"error": f"skill tool not publishable: {name}"}
            self.client.note_skill_selected(
                scope=scope, skill_id=skill_id, target="mcp", reason="tools/call"
            )
            ir = SkillIR.from_content(item.content)
            if ir.kind not in ("tool", "mcp"):
                return {"error": f"not a tool skill: {name}"}
            return {
                "skill_id": skill_id,
                "kind": ir.kind,
                "name": ir.name,
                "description": ir.description,
                "parameters": ir.parameters,
                "body": ir.body,
                "_note": "definition only — execute in your runtime, then call "
                "contextseek_skill_feedback with the outcome",
            }

        return {"error": f"unknown tool: {name}"}

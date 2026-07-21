"""Capability descriptors for LLM tool/function calling.

`ToolSpec` serializes to both OpenAI tools format and Anthropic (Claude)
tools format. The list returned by `ContextSeek.tools()` shares the same
capability copy as `ResponseMeta.hint`, keeping the tool protocol channel
and the response envelope channel aligned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """Description of an LLM tool/function.

    Serialize with :meth:`to_openai` / :meth:`to_anthropic` to each
    vendor's tool registration format.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        """OpenAI tools API shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic(self) -> dict[str, Any]:
        """Anthropic (Claude) tools API shape."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


# ---------------------------------------------------------------------------
# Built-in capability definitions — copy shared with ResponseMeta.hint
# ---------------------------------------------------------------------------

EXPAND_HINT = (
    "These items contain summaries only. "
    "Call expand(ids=[...]) to retrieve complete content for any item "
    "whose summary is insufficient to answer."
)
"""Natural-language hint text — used by ResponseMeta.hint and _EXPAND_SPEC.description."""


_RETRIEVE_SPEC = ToolSpec(
    name="retrieve",
    description=(
        "Search the ContextSeek store and return ranked hits as summaries (L1). "
        "Pass full=true to receive full content (L0) directly instead of summaries."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language query."},
            "scope": {"type": "string", "description": "Scope to search within."},
            "k": {
                "type": "integer",
                "description": "Maximum number of hits to return.",
                "default": 10,
            },
            "full": {
                "type": "boolean",
                "description": "If true, return L0 full content instead of L1 summaries.",
                "default": False,
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tag filter list.",
                "default": [],
            },
            "tag_match": {
                "type": "string",
                "enum": ["all", "any"],
                "description": "Tag match mode: 'all' requires every tag, 'any' requires at least one.",
                "default": "all",
            },
        },
        "required": ["query", "scope"],
    },
)


_EXPAND_SPEC = ToolSpec(
    name="expand",
    description=EXPAND_HINT,
    parameters={
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of item ids to expand to L0 full content.",
            },
            "scope": {
                "type": "string",
                "description": "Scope the items belong to.",
            },
        },
        "required": ["ids", "scope"],
    },
)


def default_tool_specs() -> list[ToolSpec]:
    """Return ContextSeek's built-in tool specs (retrieve + expand)."""
    return [_RETRIEVE_SPEC, _EXPAND_SPEC]

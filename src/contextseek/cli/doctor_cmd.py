"""``contextseek doctor`` — config & connectivity self-check.

Loads ``ContextSeekSettings``, reports which backend/embedder/LLM are resolved,
runs a lightweight liveness check on each (e.g. a tiny embedding call, a storage
ping), and prints clear PASS/FAIL/SKIP/WARN lines with actionable hints pointing
at ``.env.example``.

Never prints secrets.  Exits non-zero if any required component fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from contextseek.config.factory import (
    build_embedder,
    build_llm,
    resolve_embedding_dims,
)
from contextseek.config.settings import ContextSeekSettings


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
WARN = "WARN"


@dataclass
class CheckResult:
    """Outcome of a single doctor check."""

    status: str
    component: str
    message: str
    hint: str = ""
    env_section: str = ""


# ---------------------------------------------------------------------------
# Error message sanitisation — never leak secrets
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9\-_]{10,}"), "sk-***"),
    (re.compile(r"(?i)(password\s*=\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(api[_-]?key\s*=\s*)\S+"), r"\1***"),
    (re.compile(r"(?i)(token\s*=\s*)\S+"), r"\1***"),
]


def _sanitize_error_message(msg: str, *, max_len: int = 200) -> str:
    """Remove secrets from an error message and cap its length."""
    sanitized = msg
    for pattern, replacement in _SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    if len(sanitized) > max_len:
        sanitized = sanitized[:max_len] + "…"
    return sanitized


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def _classify_exception(exc: Exception, component: str) -> tuple[str, str, str]:
    """Return (message, hint, env_section) for a caught exception.

    The message is sanitised before being returned.
    """
    raw = str(exc)
    exc_type = type(exc).__name__
    msg = _sanitize_error_message(raw)

    # Build a short type prefix for the message
    short = f"{exc_type}: {msg}" if msg else exc_type

    # --- Import / missing dependency ---
    if isinstance(exc, ImportError):
        if "pyseekdb" in raw.lower():
            return (
                short,
                "Install pyseekdb: pip install pyseekdb",
                "section 1.0",
            )
        if "pyobvector" in raw.lower() or "sqlalchemy" in raw.lower():
            return (
                short,
                "Install OceanBase deps: pip install 'contextseek[oceanbase]'",
                "section 1.1",
            )
        if "langchain" in raw.lower():
            pkg_hint = "pip install 'contextseek[langchain]'"
            if "openai" in raw.lower():
                pkg_hint += " && pip install 'contextseek[openai]'"
            elif "dashscope" in raw.lower():
                pkg_hint += " && pip install 'contextseek[dashscope]'"
            elif "ollama" in raw.lower():
                pkg_hint += " && pip install 'contextseek[ollama]'"
            elif "huggingface" in raw.lower():
                pkg_hint += " && pip install 'contextseek[huggingface]'"
            return (
                short,
                f"Missing LangChain package: {pkg_hint}",
                f"section {'2' if component == 'embedding' else '3'}",
            )
        return (short, "Install the missing dependency", f"section {'2' if component == 'embedding' else '3'}")

    # --- Authentication ---
    raw_lower = raw.lower()
    if (
        "auth" in raw_lower
        or "api key" in raw_lower
        or "api_key" in raw_lower
        or "unauthorized" in raw_lower
        or "401" in raw_lower
        or "forbidden" in raw_lower
        or "403" in raw_lower
    ):
        if component == "embedding":
            return (short, "Set the correct API key (e.g. OPENAI_API_KEY) in .env", "section 2")
        return (short, "Set the correct API key (e.g. OPENAI_API_KEY) in .env", "section 3")

    # --- Connection / network ---
    if (
        isinstance(exc, (ConnectionError, TimeoutError, OSError))
        or "connection refused" in raw_lower
        or "timed out" in raw_lower
        or "timeout" in raw_lower
        or "unreachable" in raw_lower
        or "network" in raw_lower
    ):
        if component == "storage":
            return (short, "Check that the storage service is running and host/port are correct", "section 1 / 1.0 / 1.1")
        if component == "embedding":
            return (short, "Check EMBEDDING_BASE_URL and network connectivity", "section 2")
        return (short, "Check LLM_BASE_URL and network connectivity", "section 3")

    # --- Configuration / value errors ---
    if isinstance(exc, ValueError):
        if "dims" in raw_lower or "embedding_dims" in raw_lower:
            return (short, "Set EMBEDDING_DIMS when using oceanbase backend", "section 1.1 / 2")
        if "unknown" in raw_lower and "provider" in raw_lower:
            if component == "embedding":
                return (short, "Check EMBEDDING_PROVIDER spelling; see supported providers", "section 2")
            return (short, "Check LLM_PROVIDER spelling; see supported providers", "section 3")
        return (short, "Check configuration values in .env", "section 1 / 2 / 3")

    # --- Generic fallback ---
    return (short, "See .env.example for configuration reference", "section 1 / 2 / 3")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_storage(settings: ContextSeekSettings) -> CheckResult:
    """Build and initialise the storage backend to verify connectivity."""
    storage = settings.storage
    backend_name = storage.backend

    # --- oceanbase requires EMBEDDING_DIMS ---
    if backend_name == "oceanbase":
        vector_dims = settings.embedding.dims
        if not vector_dims:
            return CheckResult(
                FAIL,
                "storage",
                "EMBEDDING_DIMS must be set when STORAGE_BACKEND=oceanbase",
                "Set EMBEDDING_DIMS (e.g. 1536 for OpenAI text-embedding-3-small)",
                "section 1.1 / 2",
            )

    # --- Construct the backend (mirrors client/contextseek.py:261-318) ---
    backend: Any
    try:
        if backend_name == "oceanbase":
            from contextseek.storage.ob_backend import OceanBaseBackend

            ob = settings.ob
            geo = getattr(settings, "geo", None)
            if geo is not None and getattr(geo, "enabled", False):
                from contextseek.storage.ob_geo_backend import OceanBaseGeoBackend

                backend = OceanBaseGeoBackend(
                    table_name=ob.table_name,
                    vector_dims=vector_dims,
                    host=ob.host,
                    port=ob.port,
                    user=ob.user,
                    password=ob.password,
                    db_name=ob.db_name,
                    geo_table_name=geo.geo_table_name,
                    distance_decay_km=geo.distance_decay_km,
                    route_sample_interval_km=geo.route_sample_interval_km,
                )
            else:
                backend = OceanBaseBackend(
                    table_name=ob.table_name,
                    vector_dims=vector_dims,
                    host=ob.host,
                    port=ob.port,
                    user=ob.user,
                    password=ob.password,
                    db_name=ob.db_name,
                )
        elif backend_name == "sqlite":
            from contextseek.storage.sqlite_backend import SQLiteBackend

            backend = SQLiteBackend(path=settings.sqlite.path)
        elif backend_name == "seekdb":
            from contextseek.storage.seekdb_backend import SeekDBBackend

            seekdb = settings.seekdb
            backend = SeekDBBackend(
                path=seekdb.path,
                database=seekdb.database,
                host=seekdb.host,
                port=seekdb.port,
            )
        elif backend_name == "file":
            from contextseek.storage.file_backend import FileBackend

            backend = FileBackend(root_dir=storage.path)
        elif backend_name == "memory":
            from contextseek.storage.in_memory_backend import InMemoryBackend

            backend = InMemoryBackend()
        else:
            return CheckResult(
                FAIL,
                "storage",
                f"Unknown storage backend: {backend_name!r}",
                "Set STORAGE_BACKEND to one of: memory, file, sqlite, seekdb, oceanbase",
                "section 1",
            )
    except Exception as exc:
        msg, hint, section = _classify_exception(exc, "storage")
        return CheckResult(FAIL, "storage", msg, hint, section)

    # --- Initialise (this is the actual connectivity check) ---
    try:
        backend.initialize()
    except Exception as exc:
        msg, hint, section = _classify_exception(exc, "storage")
        return CheckResult(FAIL, "storage", msg, hint, section)

    # --- Clean up ---
    close = getattr(backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass

    return CheckResult(PASS, "storage", f"{backend_name} backend initialized")


def _check_embedding(settings: ContextSeekSettings) -> CheckResult:
    """Build embedder and do a tiny probe call."""
    emb_settings = settings.embedding
    provider = emb_settings.provider.strip().lower() if emb_settings.provider else "none"

    if provider in {"", "none"}:
        return CheckResult(
            SKIP,
            "embedding",
            "provider=none — vector search disabled",
            "Set EMBEDDING_PROVIDER to enable vector retrieval",
            "section 2",
        )

    # langchain provider requires class_path
    if provider == "langchain" and not emb_settings.class_path:
        return CheckResult(
            SKIP,
            "embedding",
            "provider=langchain but EMBEDDING_CLASS_PATH not set",
            "Set EMBEDDING_CLASS_PATH to your custom LangChain embeddings class",
            "section 2",
        )

    # Build the embedder
    try:
        embedder = build_embedder(emb_settings)
    except Exception as exc:
        msg, hint, section = _classify_exception(exc, "embedding")
        return CheckResult(FAIL, "embedding", msg, hint, section)

    if embedder is None:
        return CheckResult(
            SKIP,
            "embedding",
            f"provider={provider} resolved to None — vector search disabled",
            "Check EMBEDDING_PROVIDER / EMBEDDING_CLASS_PATH configuration",
            "section 2",
        )

    # Probe call
    try:
        vec = embedder("test")
    except Exception as exc:
        msg, hint, section = _classify_exception(exc, "embedding")
        return CheckResult(FAIL, "embedding", msg, hint, section)

    if not vec or not isinstance(vec, list):
        return CheckResult(
            FAIL,
            "embedding",
            f"embedder returned empty/invalid result for probe: {type(vec).__name__}",
            "Check EMBEDDING_MODEL and provider configuration",
            "section 2",
        )

    actual_dims = len(vec)
    configured_dims = resolve_embedding_dims(emb_settings)
    if configured_dims and configured_dims != actual_dims:
        return CheckResult(
            WARN,
            "embedding",
            f"embedder returned {actual_dims}-dim vector (configured dims={configured_dims})",
            f"Update EMBEDDING_DIMS to {actual_dims} or verify EMBEDDING_MODEL",
            "section 2",
        )

    return CheckResult(
        PASS,
        "embedding",
        f"embedder returned {actual_dims}-dim vector for probe \"test\"",
    )


def _check_llm(settings: ContextSeekSettings) -> CheckResult:
    """Build LLM and do a tiny invoke call."""
    llm_settings = settings.llm
    provider = llm_settings.provider.strip().lower() if llm_settings.provider else "none"

    if provider in {"", "none"}:
        return CheckResult(
            SKIP,
            "llm",
            "provider=none — rerank/summarize/evolution LLM disabled",
            "Set LLM_PROVIDER to enable LLM-powered features",
            "section 3",
        )

    # langchain provider requires class_path
    if provider == "langchain" and not llm_settings.class_path:
        return CheckResult(
            SKIP,
            "llm",
            "provider=langchain but LLM_CLASS_PATH not set",
            "Set LLM_CLASS_PATH to your custom LangChain chat model class",
            "section 3",
        )

    # Build the LLM
    try:
        llm = build_llm(llm_settings)
    except Exception as exc:
        msg, hint, section = _classify_exception(exc, "llm")
        return CheckResult(FAIL, "llm", msg, hint, section)

    if llm is None:
        return CheckResult(
            SKIP,
            "llm",
            f"provider={provider} resolved to None — LLM disabled",
            "Check LLM_PROVIDER / LLM_CLASS_PATH configuration",
            "section 3",
        )

    # Probe call — directly invoke to get precise error reporting
    try:
        from langchain_core.messages import HumanMessage

        resp = llm.invoke([HumanMessage(content="hello")])
    except Exception as exc:
        msg, hint, section = _classify_exception(exc, "llm")
        return CheckResult(FAIL, "llm", msg, hint, section)

    if resp is None:
        return CheckResult(
            FAIL,
            "llm",
            "LLM invoke returned None",
            "Check LLM_MODEL and provider configuration",
            "section 3",
        )

    return CheckResult(PASS, "llm", f"LLM responded to probe (provider={provider})")


def _check_cross(
    settings: ContextSeekSettings,
    results: list[CheckResult],
) -> list[CheckResult]:
    """Cross-dependency checks that produce WARN (never FAIL)."""
    warnings: list[CheckResult] = []

    backend_name = settings.storage.backend
    emb_provider = settings.embedding.provider.strip().lower() if settings.embedding.provider else "none"
    llm_provider = settings.llm.provider.strip().lower() if settings.llm.provider else "none"

    # seekdb + no external embedder
    if backend_name == "seekdb" and emb_provider in {"", "none"}:
        warnings.append(
            CheckResult(
                WARN,
                "cross",
                "seekdb backend with no external embedder — using built-in 384-dim embedding",
                "Set EMBEDDING_PROVIDER for higher-quality vectors",
                "section 2",
            )
        )

    # LLM configured but embedding not — retrieval will lack vector route
    if llm_provider not in {"", "none"} and emb_provider in {"", "none"}:
        warnings.append(
            CheckResult(
                WARN,
                "cross",
                "LLM configured but embedding is not — retrieval recall routes will not include vector search",
                "Set EMBEDDING_PROVIDER to enable vector retrieval",
                "section 2",
            )
        )

    return warnings


# ---------------------------------------------------------------------------
# Configuration report (no side effects)
# ---------------------------------------------------------------------------

def _describe_storage(settings: ContextSeekSettings) -> str:
    """Human-readable description of the resolved storage config (no secrets)."""
    s = settings.storage
    backend = s.backend
    if backend == "sqlite":
        return f"sqlite (path={settings.sqlite.path})"
    if backend == "seekdb":
        seekdb = settings.seekdb
        if seekdb.host:
            return f"seekdb (host={seekdb.host}:{seekdb.port}, database={seekdb.database})"
        return f"seekdb embedded (path={seekdb.path}, database={seekdb.database})"
    if backend == "oceanbase":
        ob = settings.ob
        return f"oceanbase (host={ob.host}:{ob.port}, user={ob.user}, db={ob.db_name}, table={ob.table_name})"
    if backend == "file":
        return f"file (path={s.path})"
    if backend == "memory":
        return "memory"
    return f"{backend} (unknown)"


def _describe_embedding(settings: ContextSeekSettings) -> str:
    """Human-readable description of the resolved embedding config (no secrets)."""
    emb = settings.embedding
    provider = emb.provider or "none"
    if provider in {"", "none"}:
        return "none"
    parts = [f"provider={provider}"]
    if emb.model:
        parts.append(f"model={emb.model}")
    dims = resolve_embedding_dims(emb)
    if dims:
        parts.append(f"dims={dims}")
    if emb.base_url:
        parts.append(f"base_url={emb.base_url}")
    return ", ".join(parts)


def _describe_llm(settings: ContextSeekSettings) -> str:
    """Human-readable description of the resolved LLM config (no secrets)."""
    llm = settings.llm
    provider = llm.provider or "none"
    if provider in {"", "none"}:
        return "none"
    parts = [f"provider={provider}"]
    if llm.model:
        parts.append(f"model={llm.model}")
    if llm.base_url:
        parts.append(f"base_url={llm.base_url}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    PASS: "[PASS]",
    FAIL: "[FAIL]",
    SKIP: "[SKIP]",
    WARN: "[WARN]",
}


def _print(text: str = "") -> None:
    print(text)


def _render_results(results: list[CheckResult]) -> None:
    """Print check results in human-readable format."""
    _print()
    _print("Checks:")
    for r in results:
        label = _STATUS_LABELS.get(r.status, f"[{r.status}]")
        _print(f"  {label} {r.component:<10} {r.message}")
        if r.hint:
            section = f"; see .env.example ({r.env_section})" if r.env_section else ""
            indent = "         ↳ fix: " if r.status == FAIL else "         ↳ hint: "
            _print(f"{indent}{r.hint}{section}")
    _print()


def _render_summary(results: list[CheckResult]) -> int:
    """Print summary line and return exit code."""
    counts = {PASS: 0, FAIL: 0, SKIP: 0, WARN: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    parts = []
    if counts[PASS]:
        parts.append(f"{counts[PASS]} PASS")
    if counts[FAIL]:
        parts.append(f"{counts[FAIL]} FAIL")
    if counts[SKIP]:
        parts.append(f"{counts[SKIP]} SKIP")
    if counts[WARN]:
        parts.append(f"{counts[WARN]} WARN")

    exit_code = 1 if counts[FAIL] > 0 else 0
    suffix = f" — exit {exit_code}" if exit_code else ""
    _print(f"Result: {', '.join(parts) if parts else 'no checks'}{suffix}")
    return exit_code


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_doctor(settings: ContextSeekSettings) -> int:
    """Run the doctor diagnostics and return an exit code.

    Exit code is 1 if any check FAILs, 0 otherwise.
    """
    # --- Configuration report (no side effects) ---
    _print()
    _print("ContextSeek doctor — configuration diagnostics")
    _print()
    _print("Configuration resolved:")
    _print(f"  storage   : {_describe_storage(settings)}")
    _print(f"  embedding : {_describe_embedding(settings)}")
    _print(f"  llm       : {_describe_llm(settings)}")

    # --- Run checks ---
    results: list[CheckResult] = []

    # Storage check — wrap in suppress_backend_noise to avoid pyseekdb etc.
    from contextseek.cli.ui import suppress_backend_noise

    with suppress_backend_noise():
        results.append(_check_storage(settings))

    results.append(_check_embedding(settings))
    results.append(_check_llm(settings))

    # Cross-dependency warnings
    results.extend(_check_cross(settings, results))

    # --- Render ---
    _render_results(results)
    return _render_summary(results)
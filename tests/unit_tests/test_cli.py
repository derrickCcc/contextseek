"""Tests for CLI argument parsing and validation."""

import argparse
import json
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from io import StringIO

import pytest

from contextseek.cli.main import build_parser, run_cli, _positive_int
from contextseek.client.contextseek import ContextSeek
from contextseek.domain.tools import default_tool_specs


class _ToolSpecClient:
    def tools(self):
        return default_tool_specs()


class TestPositiveInt:
    def test_positive_value(self):
        assert _positive_int("5") == 5
        assert _positive_int("1") == 1

    def test_zero_raises(self):
        with pytest.raises(argparse.ArgumentTypeError, match="must be > 0"):
            _positive_int("0")

    def test_negative_raises(self):
        with pytest.raises(argparse.ArgumentTypeError, match="must be > 0"):
            _positive_int("-1")

    def test_non_integer_raises(self):
        with pytest.raises(ValueError):
            _positive_int("abc")


class TestRetrieveKValidation:
    def test_k_zero_rejected(self) -> None:
        parser = build_parser()
        err = StringIO()
        with redirect_stderr(err), pytest.raises(SystemExit):
            parser.parse_args(["retrieve", "--scope", "t", "--query", "q", "--k", "0"])
        assert "must be > 0" in err.getvalue()

    def test_k_negative_rejected(self) -> None:
        parser = build_parser()
        err = StringIO()
        with redirect_stderr(err), pytest.raises(SystemExit):
            parser.parse_args(["retrieve", "--scope", "t", "--query", "q", "--k", "-1"])
        assert "must be > 0" in err.getvalue()

    def test_k_positive_accepted(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["retrieve", "--scope", "t", "--query", "q", "--k", "5"]
        )
        assert args.k == 5


class TestToolsOutput:
    def test_openai_format_outputs_valid_tool_definitions(self, capsys):
        assert run_cli(["tools", "--format", "openai"], client=_ToolSpecClient()) == 0

        payload = json.loads(capsys.readouterr().out)
        tool_names = {tool["function"]["name"] for tool in payload}

        assert tool_names == {"retrieve", "expand"}
        retrieve_tool = next(
            tool["function"]
            for tool in payload
            if tool["function"]["name"] == "retrieve"
        )
        expand_tool = next(
            tool["function"] for tool in payload if tool["function"]["name"] == "expand"
        )

        assert all(tool["type"] == "function" for tool in payload)
        assert retrieve_tool["parameters"]["type"] == "object"
        assert retrieve_tool["parameters"]["required"] == ["query", "scope"]
        assert set(retrieve_tool["parameters"]["properties"]) == {
            "query",
            "scope",
            "k",
            "full",
        }
        assert expand_tool["parameters"]["required"] == ["ids", "scope"]
        assert expand_tool["parameters"]["properties"]["ids"]["type"] == "array"

    def test_anthropic_format_outputs_valid_tool_definitions(self, capsys):
        assert (
            run_cli(["tools", "--format", "anthropic"], client=_ToolSpecClient()) == 0
        )

        payload = json.loads(capsys.readouterr().out)
        tool_names = {tool["name"] for tool in payload}

        assert tool_names == {"retrieve", "expand"}
        retrieve_tool = next(tool for tool in payload if tool["name"] == "retrieve")
        expand_tool = next(tool for tool in payload if tool["name"] == "expand")

        assert all("function" not in tool for tool in payload)
        assert retrieve_tool["input_schema"]["type"] == "object"
        assert retrieve_tool["input_schema"]["required"] == ["query", "scope"]
        assert set(retrieve_tool["input_schema"]["properties"]) == {
            "query",
            "scope",
            "k",
            "full",
        }
        assert expand_tool["input_schema"]["required"] == ["ids", "scope"]
        assert expand_tool["input_schema"]["properties"]["ids"]["items"] == {
            "type": "string"
        }


class TestRetrieveTagFiltering:
    def test_retrieve_accepts_tags_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["retrieve", "--scope", "t", "--query", "q", "--tags", "a,b"]
        )

        assert args.tags == "a,b"

    def test_retrieve_filters_results_by_all_tags(self) -> None:
        ctx = ContextSeek()
        kept = ctx.add(
            "database backup runbook",
            scope="t/p",
            source="test",
            tags=["ops", "database"],
        )
        ctx.add(
            "database onboarding guide",
            scope="t/p",
            source="test",
            tags=["docs", "database"],
        )
        out = StringIO()

        with redirect_stdout(out):
            code = run_cli(
                [
                    "retrieve",
                    "--scope",
                    "t/p",
                    "--query",
                    "database",
                    "--tags",
                    "ops,database",
                    "--json",
                ],
                client=ctx,
            )

        payload = json.loads(out.getvalue())
        assert code == 0
        assert [item["id"] for item in payload["items"]] == [kept.id]


class TestExpandOutput:
    def test_expand_reports_missing_ids(self) -> None:
        ctx = ContextSeek()
        item = ctx.add("expand target", scope="t/p", source="test")
        out = StringIO()

        with redirect_stdout(out):
            code = run_cli(
                [
                    "expand",
                    "--scope",
                    "t/p",
                    "--ids",
                    f"{item.id},missing-id",
                ],
                client=ctx,
            )

        payload = json.loads(out.getvalue())
        assert code == 0
        assert [it["id"] for it in payload["items"]] == [item.id]
        assert payload["missing_ids"] == ["missing-id"]


class TestDoctor:
    """Tests for the ``contextseek doctor`` subcommand."""

    # ------------------------------------------------------------------
    # Parser registration
    # ------------------------------------------------------------------

    def test_doctor_parser_registered(self) -> None:
        """``doctor`` subcommand is registered and parseable."""
        parser = build_parser()
        args = parser.parse_args(["doctor"])
        assert args.command == "doctor"

    def test_doctor_in_help(self) -> None:
        """``doctor`` appears in the main parser help text."""
        parser = build_parser()
        help_text = parser.format_help()
        assert "doctor" in help_text
        assert "diagnose" in help_text

    # ------------------------------------------------------------------
    # Storage checks
    # ------------------------------------------------------------------

    def test_doctor_all_none_passes(self, capsys) -> None:
        """Default isolated env (memory/none/none) → exit 0 with PASS + SKIPs."""
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[PASS] storage" in out
        assert "[SKIP] embedding" in out
        assert "[SKIP] llm" in out
        assert "[FAIL]" not in out
        assert "exit 0" not in out  # no failure, no exit suffix
        # Should mention .env.example in the SKIP hints
        assert ".env.example" in out

    def test_doctor_storage_memory_pass(self, monkeypatch, capsys) -> None:
        """Memory backend → PASS."""
        monkeypatch.setenv("STORAGE_BACKEND", "memory")
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[PASS] storage" in out
        assert "memory backend initialized" in out

    def test_doctor_storage_sqlite_pass(self, monkeypatch, tmp_path, capsys) -> None:
        """SQLite backend with a temp path → PASS."""
        db_path = str(tmp_path / "test.sqlite3")
        monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
        monkeypatch.setenv("SQLITE_PATH", db_path)
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[PASS] storage" in out
        assert "sqlite backend initialized" in out

    def test_doctor_storage_file_pass(self, monkeypatch, tmp_path, capsys) -> None:
        """File backend with a temp path → PASS."""
        file_path = str(tmp_path / "store")
        monkeypatch.setenv("STORAGE_BACKEND", "file")
        monkeypatch.setenv("STORAGE_PATH", file_path)
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[PASS] storage" in out
        assert "file backend initialized" in out

    def test_doctor_storage_oceanbase_missing_dims_fails(
        self, monkeypatch, capsys
    ) -> None:
        """OceanBase backend without EMBEDDING_DIMS → storage FAIL, exit 1."""
        monkeypatch.setenv("STORAGE_BACKEND", "oceanbase")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "none")
        monkeypatch.setenv("EMBEDDING_DIMS", "0")
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] storage" in out
        assert "EMBEDDING_DIMS" in out
        assert ".env.example" in out

    def test_doctor_storage_unknown_backend_fails(self, monkeypatch, capsys) -> None:
        """Unknown storage backend → FAIL."""
        monkeypatch.setenv("STORAGE_BACKEND", "unknown_backend")
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] storage" in out
        assert "Unknown storage backend" in out
        assert "memory, file, sqlite, seekdb, oceanbase" in out

    def test_doctor_storage_empty_backend_fails(self, monkeypatch, capsys) -> None:
        """Empty storage backend string → FAIL (unknown)."""
        monkeypatch.setenv("STORAGE_BACKEND", "")
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] storage" in out

    # ------------------------------------------------------------------
    # Embedding checks
    # ------------------------------------------------------------------

    def test_doctor_embedding_none_skips(self, capsys) -> None:
        """Embedding provider=none → SKIP."""
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[SKIP] embedding" in out
        assert "vector search disabled" in out

    def test_doctor_embedding_langchain_no_class_path_skips(
        self, monkeypatch, capsys
    ) -> None:
        """Embedding provider=langchain without class_path → SKIP."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "langchain")
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[SKIP] embedding" in out
        assert "EMBEDDING_CLASS_PATH" in out

    def test_doctor_embedding_build_failure(self, monkeypatch, capsys) -> None:
        """Embedding provider configured but build_embedder raises → FAIL."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")

        def _fake_build_embedder(settings):
            raise PermissionError("AuthenticationError: Incorrect API key provided")

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", _fake_build_embedder
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] embedding" in out
        assert "API key" in out
        assert ".env.example" in out

    def test_doctor_embedding_probe_success(self, monkeypatch, capsys) -> None:
        """Embedder successfully returns a vector → PASS."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
        monkeypatch.setenv("EMBEDDING_DIMS", "4")

        def _fake_embedder(text):
            return [0.1, 0.2, 0.3, 0.4]

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", lambda s: _fake_embedder
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[PASS] embedding" in out
        assert "4-dim" in out

    def test_doctor_embedding_probe_returns_empty_fails(
        self, monkeypatch, capsys
    ) -> None:
        """Embedder returns empty list → FAIL."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")

        def _fake_embedder(text):
            return []

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", lambda s: _fake_embedder
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] embedding" in out

    def test_doctor_embedding_probe_raises_fails(self, monkeypatch, capsys) -> None:
        """Embedder probe call raises → FAIL."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")

        def _fake_embedder(text):
            raise ConnectionError("Connection refused to embedding server")

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", lambda s: _fake_embedder
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] embedding" in out
        assert "Connection" in out or "connection" in out

    def test_doctor_embedding_dims_mismatch_warns(self, monkeypatch, capsys) -> None:
        """Embedder returns dims different from configured → WARN."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("EMBEDDING_DIMS", "1536")

        def _fake_embedder(text):
            return [0.1] * 768  # returns 768 but configured 1536

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", lambda s: _fake_embedder
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        # WARN does not cause failure
        assert code == 0
        assert "[WARN] embedding" in out
        assert "768-dim" in out
        assert "1536" in out

    def test_doctor_embedding_probe_returns_non_list_fails(
        self, monkeypatch, capsys
    ) -> None:
        """Embedder returns non-list (e.g. None) → FAIL."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", lambda s: (lambda t: None)
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] embedding" in out

    # ------------------------------------------------------------------
    # LLM checks
    # ------------------------------------------------------------------

    def test_doctor_llm_none_skips(self, capsys) -> None:
        """LLM provider=none → SKIP."""
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[SKIP] llm" in out
        assert "rerank/summarize/evolution" in out

    def test_doctor_llm_langchain_no_class_path_skips(
        self, monkeypatch, capsys
    ) -> None:
        """LLM provider=langchain without class_path → SKIP."""
        monkeypatch.setenv("LLM_PROVIDER", "langchain")
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[SKIP] llm" in out
        assert "LLM_CLASS_PATH" in out

    def test_doctor_llm_build_failure(self, monkeypatch, capsys) -> None:
        """LLM build_llm raises → FAIL."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")

        def _fake_build_llm(settings):
            raise ImportError("No module named 'langchain_openai'")

        monkeypatch.setattr("contextseek.cli.doctor_cmd.build_llm", _fake_build_llm)
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] llm" in out
        assert "langchain" in out.lower()

    def test_doctor_llm_invoke_failure(self, monkeypatch, capsys) -> None:
        """LLM provider configured but invoke raises → FAIL."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")

        class _FakeLLM:
            def invoke(self, messages):
                raise ConnectionError("Connection refused to api.openai.com")

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_llm", lambda s: _FakeLLM()
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] llm" in out
        assert ".env.example" in out

    def test_doctor_llm_invoke_success(self, monkeypatch, capsys) -> None:
        """LLM responds to probe → PASS."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")

        class _FakeLLM:
            def invoke(self, messages):
                return "hello back"

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_llm", lambda s: _FakeLLM()
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[PASS] llm" in out

    def test_doctor_llm_invoke_returns_none_fails(self, monkeypatch, capsys) -> None:
        """LLM invoke returns None → FAIL."""
        monkeypatch.setenv("LLM_PROVIDER", "openai")

        class _FakeLLM:
            def invoke(self, messages):
                return None

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_llm", lambda s: _FakeLLM()
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert "[FAIL] llm" in out
        assert "None" in out

    # ------------------------------------------------------------------
    # Cross-dependency checks
    # ------------------------------------------------------------------

    def test_doctor_cross_dependency_seekdb_warn(self, monkeypatch, capsys) -> None:
        """seekdb + embedding=none → WARN about built-in embedding."""
        monkeypatch.setenv("STORAGE_BACKEND", "seekdb")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "none")

        from contextseek.cli.doctor_cmd import CheckResult, PASS

        def _fake_check_storage(settings):
            return CheckResult(PASS, "storage", "seekdb backend initialized")

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd._check_storage", _fake_check_storage
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[WARN] cross" in out
        assert "seekdb" in out.lower()

    def test_doctor_cross_dependency_llm_without_embedding_warn(
        self, monkeypatch, capsys
    ) -> None:
        """LLM configured but embedding=none → WARN about missing vector search."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "none")
        monkeypatch.setenv("LLM_PROVIDER", "openai")

        from contextseek.cli.doctor_cmd import CheckResult, PASS

        def _fake_check_llm(settings):
            return CheckResult(PASS, "llm", "LLM responded")

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd._check_llm", _fake_check_llm
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[WARN] cross" in out
        assert "vector search" in out.lower()

    def test_doctor_no_cross_warning_when_both_configured(
        self, monkeypatch, capsys
    ) -> None:
        """Both embedding and LLM configured → no cross WARN."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("LLM_PROVIDER", "openai")

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder",
            lambda s: (lambda t: [0.1, 0.2]),
        )

        class _FakeLLM:
            def invoke(self, messages):
                return "ok"

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_llm", lambda s: _FakeLLM()
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 0
        assert "[WARN] cross" not in out

    # ------------------------------------------------------------------
    # Secret sanitisation
    # ------------------------------------------------------------------

    def test_doctor_no_secrets_leaked_sk_key(self, monkeypatch, capsys) -> None:
        """Doctor output must never contain raw sk- keys from error messages."""
        fake_secret = "sk-FAKESECRET1234567890abcdef"

        def _fake_build_embedder(settings):
            raise PermissionError(
                f"AuthenticationError: Incorrect API key provided: {fake_secret}"
            )

        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", _fake_build_embedder
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert fake_secret not in out
        assert "sk-***" in out

    def test_doctor_no_secrets_leaked_password(self, monkeypatch, capsys) -> None:
        """Doctor output must not leak password= values from error messages."""
        fake_password = "SuperSecret123!"

        def _fake_build_embedder(settings):
            raise ValueError(f"Connection failed: password={fake_password} rejected")

        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", _fake_build_embedder
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert fake_password not in out
        assert "password=***" in out

    def test_doctor_no_secrets_leaked_token(self, monkeypatch, capsys) -> None:
        """Doctor output must not leak token= values from error messages."""
        fake_token = "tok_abcDEF123456"

        def _fake_build_embedder(settings):
            raise RuntimeError(f"Auth failed: token={fake_token} is invalid")

        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd.build_embedder", _fake_build_embedder
        )
        code = run_cli(["doctor"])
        out = capsys.readouterr().out

        assert code == 1
        assert fake_token not in out
        assert "token=***" in out

    def test_doctor_config_report_no_password(self, monkeypatch, capsys) -> None:
        """OceanBase config report must not include the password field."""
        monkeypatch.setenv("STORAGE_BACKEND", "oceanbase")
        monkeypatch.setenv("OB_PASSWORD", "should_not_appear_in_output")
        monkeypatch.setenv("EMBEDDING_DIMS", "1536")

        # Patch storage check to avoid real OB connection
        from contextseek.cli.doctor_cmd import CheckResult, PASS

        monkeypatch.setattr(
            "contextseek.cli.doctor_cmd._check_storage",
            lambda s: CheckResult(PASS, "storage", "ok"),
        )
        run_cli(["doctor"])
        out = capsys.readouterr().out

        assert "should_not_appear_in_output" not in out

    # ------------------------------------------------------------------
    # Sanitisation unit tests
    # ------------------------------------------------------------------

    def test_sanitize_truncates_long_messages(self) -> None:
        """_sanitize_error_message truncates messages over 200 chars."""
        from contextseek.cli.doctor_cmd import _sanitize_error_message

        long_msg = "x" * 300
        result = _sanitize_error_message(long_msg)
        assert len(result) <= 201  # 200 + ellipsis
        assert result.endswith("…")

    def test_sanitize_preserves_short_messages(self) -> None:
        """_sanitize_error_message preserves short messages unchanged."""
        from contextseek.cli.doctor_cmd import _sanitize_error_message

        assert _sanitize_error_message("short error") == "short error"

    def test_sanitize_sk_key_pattern(self) -> None:
        """_sanitize_error_message masks sk- prefixed keys."""
        from contextseek.cli.doctor_cmd import _sanitize_error_message

        result = _sanitize_error_message("error: sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
        assert "sk-***" in result
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234" not in result

    # ------------------------------------------------------------------
    # Output format structure
    # ------------------------------------------------------------------

    def test_doctor_output_has_header(self, capsys) -> None:
        """Doctor output starts with the diagnostic header."""
        run_cli(["doctor"])
        out = capsys.readouterr().out

        assert "ContextSeek doctor" in out
        assert "configuration diagnostics" in out.lower()

    def test_doctor_output_has_config_resolved_section(self, capsys) -> None:
        """Doctor output includes a 'Configuration resolved:' section."""
        run_cli(["doctor"])
        out = capsys.readouterr().out

        assert "Configuration resolved:" in out
        assert "storage" in out
        assert "embedding" in out
        assert "llm" in out

    def test_doctor_output_has_checks_section(self, capsys) -> None:
        """Doctor output includes a 'Checks:' section."""
        run_cli(["doctor"])
        out = capsys.readouterr().out

        assert "Checks:" in out

    def test_doctor_output_has_result_summary(self, capsys) -> None:
        """Doctor output ends with a 'Result:' summary line."""
        run_cli(["doctor"])
        out = capsys.readouterr().out

        assert "Result:" in out
        assert "PASS" in out
        assert "SKIP" in out

    def test_doctor_exit_code_nonzero_on_fail(self, monkeypatch, capsys) -> None:
        """Exit code is 1 when any check FAILs."""
        monkeypatch.setenv("STORAGE_BACKEND", "bad_backend")
        code = run_cli(["doctor"])
        assert code == 1

    def test_doctor_exit_code_zero_on_all_pass_skip(self, capsys) -> None:
        """Exit code is 0 when no checks FAIL."""
        code = run_cli(["doctor"])
        assert code == 0

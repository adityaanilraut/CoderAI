"""Tests for Phase 2 capabilities:
- MCP sampling/createMessage handling
- Structured lint issue parsing (ruff, eslint, golangci-lint, shellcheck)
- Multi-language symbol search (Go, Rust, C++, Java, Kotlin, Ruby, PHP)
- Multi-language AST / pattern chunking (Go, Rust, C++, Java, Ruby)
"""

import json
from pathlib import Path
import pytest

from coderAI.tools.mcp import MCPClient
from coderAI.tools.lint import _parse_lint_issues
from coderAI.tools.search import SymbolSearchTool
from coderAI.context.code_chunker import chunk_file


@pytest.mark.asyncio
async def test_mcp_sampling_custom_handler():
    client = MCPClient()

    async def custom_sampling(server_name: str, params: dict):
        return {
            "role": "assistant",
            "content": {"type": "text", "text": f"Echo from custom sampling for {server_name}"},
            "model": "test-model",
            "stopReason": "endTurn",
        }

    client.sampling_handler = custom_sampling
    res = await client._handle_sampling(
        "test-server",
        {
            "messages": [{"role": "user", "content": "Hello"}],
            "maxTokens": 100,
        },
    )

    assert res["role"] == "assistant"
    assert res["content"]["text"] == "Echo from custom sampling for test-server"
    assert res["model"] == "test-model"


@pytest.mark.asyncio
async def test_mcp_sampling_validation_error():
    client = MCPClient()
    with pytest.raises(ValueError, match="requires non-empty messages"):
        await client._handle_sampling("test-server", {"messages": []})


def test_parse_lint_issues_ruff():
    sample_ruff = json.dumps(
        [
            {
                "code": "F401",
                "message": "os imported but unused",
                "location": {"row": 5, "column": 8},
                "filename": "src/main.py",
                "fix": {"applicability": "safe"},
            }
        ]
    )
    count, issues, files = _parse_lint_issues("ruff", sample_ruff)
    assert count == 1
    assert issues[0]["code"] == "F401"
    assert issues[0]["line"] == 5
    assert issues[0]["fixable"] is True
    assert files == ["src/main.py"]


def test_parse_lint_issues_eslint():
    sample_eslint = json.dumps(
        [
            {
                "filePath": "src/app.ts",
                "messages": [
                    {
                        "ruleId": "no-unused-vars",
                        "message": "'x' is defined but never used",
                        "line": 12,
                        "column": 7,
                        "severity": 2,
                    }
                ],
            }
        ]
    )
    count, issues, files = _parse_lint_issues("eslint", sample_eslint)
    assert count == 1
    assert issues[0]["code"] == "no-unused-vars"
    assert issues[0]["line"] == 12
    assert files == ["src/app.ts"]


@pytest.mark.asyncio
async def test_multi_language_symbol_search(tmp_path: Path):
    # Setup files in multiple languages
    go_file = tmp_path / "server.go"
    go_file.write_text("package main\n\ntype HttpServer struct {}\n\nfunc HandleRequest() {}\n")

    rust_file = tmp_path / "lib.rs"
    rust_file.write_text("pub struct DatabasePool;\n\npub async fn connect_db() {}\n")

    cpp_file = tmp_path / "engine.cpp"
    cpp_file.write_text("class RenderEngine {\npublic:\n    void renderFrame() {}\n};\n")

    java_file = tmp_path / "App.java"
    java_file.write_text(
        "public class ApplicationConfig {\n    public void configureService() {}\n}\n"
    )

    ruby_file = tmp_path / "user.rb"
    ruby_file.write_text("class UserModel\n  def authenticate\n  end\nend\n")

    tool = SymbolSearchTool()

    # Test Go
    res_go = await tool.execute(symbol="HttpServer", path=str(tmp_path))
    assert res_go["success"] is True
    assert any(r["name"] == "HttpServer" and r["kind"] == "class" for r in res_go["results"])

    # Test Rust
    res_rs = await tool.execute(symbol="connect_db", path=str(tmp_path))
    assert res_rs["success"] is True
    assert any(r["name"] == "connect_db" and r["kind"] == "function" for r in res_rs["results"])

    # Test C++
    res_cpp = await tool.execute(symbol="RenderEngine", path=str(tmp_path))
    assert res_cpp["success"] is True
    assert any(r["name"] == "RenderEngine" and r["kind"] == "class" for r in res_cpp["results"])

    # Test Java
    res_java = await tool.execute(symbol="ApplicationConfig", path=str(tmp_path))
    assert res_java["success"] is True
    assert any(r["name"] == "ApplicationConfig" for r in res_java["results"])

    # Test Ruby
    res_rb = await tool.execute(symbol="UserModel", path=str(tmp_path))
    assert res_rb["success"] is True
    assert any(r["name"] == "UserModel" for r in res_rb["results"])


def test_multi_language_code_chunking(tmp_path: Path):
    go_file = tmp_path / "worker.go"
    go_file.write_text("""package main

// TaskWorker manages concurrent background executions and schedules tasks across threads.
type TaskWorker struct {
    ID          int
    Name        string
    Queue       chan string
    IsActive    bool
    MaxRounds   int
}

// ProcessJob performs high-priority processing for incoming batch jobs and reports metrics.
func ProcessJob(taskID int, payload string, timeoutSeconds int) (bool, error) {
    println("Processing incoming payload...")
    return true, nil
}
""")
    res_go = chunk_file(go_file, tmp_path)
    assert len(res_go.chunks) >= 2
    types = {c.chunk_type for c in res_go.chunks}
    assert "class" in types or "function" in types

    rust_file = tmp_path / "service.rs"
    rust_file.write_text("""// WorkerService coordinates multi-threaded worker pipelines with memory safety.
pub struct WorkerService {
    pub active: bool,
    pub worker_id: u64,
    pub task_name: String,
    pub max_retries: usize,
}

// run_worker launches the primary worker daemon loop and captures telemetry metrics.
pub fn run_worker(service: &WorkerService, timeout_ms: u64) -> Result<(), String> {
    println!("Launching background service worker...");
    Ok(())
}
""")
    res_rs = chunk_file(rust_file, tmp_path)
    assert len(res_rs.chunks) >= 2

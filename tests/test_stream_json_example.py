"""The stdio example sends EOF and reaps its child without a live provider."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.mark.asyncio
async def test_example_completes_with_a_child_that_reads_until_eof(tmp_path, monkeypatch, capsys):
    source = Path(__file__).resolve().parents[1] / "examples/coderai-cli-stream-json/main.py"
    spec = importlib.util.spec_from_file_location("stream_example", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    processes = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        from coderai.ui.shell.startup import _build_parser

        parsed = _build_parser().parse_args(args[1:])
        assert parsed.print_mode and parsed.prompt_flag and parsed.input_format == "stream-json"
        process = await real_spawn(
            sys.executable,
            "-c",
            "import sys,json; payload=json.load(sys.stdin); print(json.dumps({'echo':payload['prompt']}))",
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    try:
        await asyncio.wait_for(module.main(), 2)
        assert "How many lines of code" in capsys.readouterr().out
        assert processes[0].returncode == 0
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                await process.wait()

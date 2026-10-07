"""A tool-free reviewer must not silently lose Luna reasoning via API fallback."""

import asyncio


def test_empty_role_allowlist_is_advertised_as_no_tools(tmp_path, monkeypatch):
    from coderai.cli.session_factory import build_session_manager, close_session_manager

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODERAI_THINKING_ENABLED", "1")
    monkeypatch.setenv("CODERAI_REASONING_EFFORT", "high")
    monkeypatch.delenv("CODERAI_CONFIG_FILE", raising=False)
    monkeypatch.delenv("CODERAI_CONFIG_JSON", raising=False)
    (tmp_path / "review.md").write_text("Review the supplied source for concrete defects.")
    role = tmp_path / "review.yaml"
    role.write_text(
        "version: 1\nagent:\n  name: review\n  system_prompt_path: ./review.md\n  tools: []\n  allowed_tools: []\n"
    )
    captured = []

    async def check():
        manager = build_session_manager(
            str(tmp_path),
            model="gpt-6-luna",
            agent=str(role),
            non_interactive=True,
            client_factory=lambda *args, **kwargs: {
                "client": object(),
                "model": "gpt-6-luna",
                "thinkingEnabled": True,
                "baseURL": "https://api.openai.com/v1",
            },
        )

        async def completion(client, request, **kwargs):
            captured.append(request)
            return {"choices": [{"message": {"content": "No actionable issues found."}}]}

        manager._create_completion = completion
        try:
            assert manager.get_resolved_settings()["allowedTools"] == []
            await manager.create_session("Review this supplied diff.")
            assert captured and not captured[0].get("tools")
            assert captured[0]["reasoning_effort"] == "high"
        finally:
            await close_session_manager(manager)

    asyncio.run(check())

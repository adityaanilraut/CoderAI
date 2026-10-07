"""Deterministic, credential-free provider fixture for real terminal journeys."""

import asyncio
import json
from pathlib import Path
import sys

from coderai.background.store import JobStore
from coderai.soul.session.models import SessionEntry, SessionMessage
from coderai.wire.emitter import WireEmitter


class OfflineManager:
    def __init__(self, root):
        self.project_root = root
        self.entries = {}
        self.messages = {}
        self.steers = []
        self.job_store = JobStore()
        self._assistant_callback = lambda *a: None
        self.on_stream_chunk = lambda text: None
        self.on_thinking_chunk = lambda text: None
        self.emitters = {}

    def get_event_emitter(self, sid):
        if sid not in self.emitters:
            self.emitters[sid] = WireEmitter()
        return self.emitters[sid]

    def get_reasoning_effort(self):
        return "max"

    def resolve_session_id(self, raw):
        return next((sid for sid in self.entries if sid.startswith(raw)), None)

    def fork_session(self, sid):
        from dataclasses import replace

        forked = "fork-" + sid
        self.entries[forked] = replace(self.entries[sid], id=forked, fork_of=sid)
        self.messages[forked] = [replace(m, session_id=forked) for m in self.messages[sid]]
        return forked

    def delete_session(self, sid):
        return self.entries.pop(sid, None) is not None

    def get_active_model(self):
        return "gpt-6-sol"

    def get_active_agent_role(self):
        return "default"

    def get_resolved_settings(self):
        return {"contextWindow": 32000, "multimodal": "on"}

    def is_auto_approve(self):
        return False

    def is_afk(self):
        return False

    def get_session(self, sid):
        return self.entries.get(sid)

    def list_sessions(self):
        return list(self.entries.values())

    def list_session_messages(self, sid):
        return self.messages.get(sid, [])

    def interrupt_session(self, sid):
        self.entries[sid].status = "interrupted"

    def set_plan_mode(self, sid, value):
        self.entries[sid].plan_mode = value

    def steer_session(self, sid, text):
        self.steers.append(text)
        print("FIXTURE_STEER " + text, flush=True)

    async def create_session(
        self, text, *, session_id, plan_mode=False, skills=None, content_params=None
    ):
        self.entries[session_id] = SessionEntry(
            session_id, summary=text, status="running", plan_mode=plan_mode
        )
        self.messages[session_id] = []
        await self.turn(session_id, text, content_params)
        return session_id

    async def reply_session(self, sid, text=None, *, permission_replies=None, **kwargs):
        if permission_replies is not None:
            print("FIXTURE_PERMISSION " + json.dumps(permission_replies), flush=True)
            text = "permission handled"
        await self.turn(sid, text or "continue", kwargs.get("content_params"))

    async def turn(self, sid, text, images):
        print("FIXTURE_BEGIN " + text, flush=True)
        self.messages[sid].append(SessionMessage(str(len(self.messages[sid])), sid, "user", text))
        entry = self.entries[sid]
        entry.status = "running"
        try:
            for i in range(30 if text.startswith("delay") else 3):
                await asyncio.sleep(0.04)
                self.on_stream_chunk(f"{text} chunk{i} ")
                if i == 10:
                    print("FIXTURE_HEARTBEAT", flush=True)
            if text == "tool output":
                output = "\n".join(f"complete-output-{i}" for i in range(100))
                message = SessionMessage(
                    "full-tool",
                    sid,
                    "tool",
                    json.dumps(
                        {
                            "name": "bash",
                            "ok": True,
                            "output": output,
                            "metadata": {"command": "pytest tests/fixture.py"},
                        }
                    ),
                    tool_call_id="t-full",
                )
                self.messages[sid].append(message)
                self._assistant_callback(message, True)
            if text == "approval":
                entry.status = "ask_permission"
                entry.ask_permissions = [
                    {"toolCallId": "t1", "name": "write", "scopes": ["write-in-cwd"]}
                ]
                return
            if text == "question":
                entry.status = "ask_user_question"
                msg = SessionMessage(
                    "question-tool",
                    sid,
                    "tool",
                    json.dumps(
                        {
                            "metadata": {
                                "questions": [
                                    {
                                        "question": "Choose a color",
                                        "options": [{"label": "Red"}, {"label": "Blue"}],
                                    }
                                ]
                            }
                        }
                    ),
                )
                self.messages[sid].append(msg)
                return
            message = SessionMessage(
                f"a-{len(self.messages[sid])}", sid, "assistant", f"FIXTURE_FINISH {text}"
            )
            entry.assistant_reply = message.content
            entry.status = "ready"
            self.messages[sid].append(message)
            self._assistant_callback(message, True)
        except asyncio.CancelledError:
            print("FIXTURE_CANCEL " + text, flush=True)
            entry.status = "interrupted"
            raise


async def main():
    from coderai.ui.shell.app import _run_interactive

    await _run_interactive(OfflineManager(str(Path(sys.argv[1]))), False)


if __name__ == "__main__":
    asyncio.run(main())

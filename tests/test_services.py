"""Tests for the ToolServices container and ContextVar scoping."""

import asyncio

import coderAI.core.services as services_mod
from coderAI.core.services import ToolServices, get_services, services_scope


class TestDefaultServices:
    def test_default_is_process_wide_singleton(self):
        assert get_services() is get_services()

    def test_default_built_lazily(self, monkeypatch):
        monkeypatch.setattr(services_mod, "_process_default", None)
        assert services_mod._process_default is None
        svc = get_services()
        assert services_mod._process_default is svc

    def test_fields_built_lazily(self):
        svc = ToolServices()
        assert svc._notepad is None
        pad = svc.notepad
        assert svc._notepad is pad
        assert svc.notepad is pad

    def test_unbound_config_stays_dynamic(self):
        from coderAI.system.config import config_manager

        svc = ToolServices()
        assert svc.config is config_manager.load()
        # Not cached as a snapshot: a config reload is observed immediately.
        config_manager._config = None
        assert svc.config is config_manager.load()

    def test_default_events_is_process_emitter(self):
        from coderAI.system.events import event_emitter

        assert ToolServices().events is event_emitter


class TestServicesScope:
    def test_scope_isolates_stores(self):
        with services_scope() as outer:
            pad_outer = outer.notepad
            tracker_outer = outer.agent_tracker
            with services_scope() as inner:
                assert get_services() is inner
                assert inner.notepad is not pad_outer
                assert inner.agent_tracker is not tracker_outer
            assert get_services() is outer
            assert outer.notepad is pad_outer

    def test_scope_restores_previous_on_exit(self):
        before = get_services()
        with services_scope():
            assert get_services() is not before
        assert get_services() is before

    def test_injected_instances_are_used(self):
        sentinel = object()
        with services_scope(memory_store=sentinel) as svc:
            assert svc.memory_store is sentinel

    def test_inherit_shares_parent_stores_but_overrides_config(self):
        fake_config = object()
        with services_scope() as parent:
            pad = parent.notepad
            with services_scope(inherit=True, config=fake_config) as child:
                assert child.config is fake_config
                assert child.notepad is pad
            # Parent scope keeps its own (dynamic) config resolution.
            assert parent.config is not fake_config

    def test_scope_propagates_into_tasks(self):
        async def main():
            with services_scope() as svc:

                async def child():
                    return get_services()

                return svc, await asyncio.create_task(child())

        svc, seen_in_task = asyncio.run(main())
        assert seen_in_task is svc

"""Coverage for coderAI/tui/prompt_history.py recall logic."""

from coderAI.tui.prompt_history import PromptHistory


def test_add_records_and_skips_blank_and_dupes():
    h = PromptHistory()
    h.add("first")
    h.add("   ")  # blank -> ignored
    h.add("first")  # consecutive dupe -> ignored
    h.add("second")
    assert h.entries == ["first", "second"]


def test_non_consecutive_dupe_is_kept():
    h = PromptHistory()
    h.add("a")
    h.add("b")
    h.add("a")
    assert h.entries == ["a", "b", "a"]


def test_prev_walks_back_and_clamps_at_oldest():
    h = PromptHistory()
    h.add("first")
    h.add("second")
    assert h.prev("") == "second"
    assert h.prev("second") == "first"
    # Already at the oldest entry -> stays put.
    assert h.prev("first") == "first"


def test_prev_on_empty_history_returns_none():
    h = PromptHistory()
    assert h.prev("draft") is None
    assert not h.navigating


def test_next_returns_none_when_not_navigating():
    h = PromptHistory()
    h.add("first")
    assert h.next() is None


def test_next_restores_stashed_draft_at_end():
    h = PromptHistory()
    h.add("first")
    h.add("second")
    assert h.prev("my draft") == "second"  # stashes "my draft"
    assert h.prev("second") == "first"
    assert h.next() == "second"
    # Walking past the newest entry restores the live draft and ends nav.
    assert h.next() == "my draft"
    assert not h.navigating
    assert h.next() is None


def test_add_resets_navigation_state():
    h = PromptHistory()
    h.add("first")
    h.prev("")
    assert h.navigating
    h.add("second")
    assert not h.navigating


def test_max_entries_evicts_oldest():
    h = PromptHistory(max_entries=3)
    for token in ["a", "b", "c", "d", "e"]:
        h.add(token)
    assert h.entries == ["c", "d", "e"]

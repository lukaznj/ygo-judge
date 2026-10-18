import json

import pytest

from ygo_judge import server
from ygo_judge.scenario import Setup


def test_saved_duel_keeps_normal_first_turn_rules_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STATE", tmp_path)
    store = server.Duels()
    duel = store.create(Setup())
    duel.play([])
    store.save(duel)
    restored = server.Duels().get(duel.id)
    assert restored.board() == duel.board()
    assert restored.log == duel.log
    assert "Enter the Battle Phase" not in restored.describe_decision()


def test_old_saved_duel_requires_a_fresh_test(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "STATE", tmp_path)
    store = server.Duels()
    duel = store.create(Setup())
    store.save(duel)
    path = store._path(duel.id)
    saved = json.loads(path.read_text())
    saved.pop("rules_version")  # Original unversioned format, with first-turn Battle Phase enabled.
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="Start a new duel with start_duel"):
        server.Duels().get(duel.id)

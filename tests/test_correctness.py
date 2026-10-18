"""Regressions for ruling changes introduced by the scenario/decision harness."""

import pytest
from pydantic import ValidationError

from ygo_judge import engine as E
from ygo_judge.cards import CardDB
from ygo_judge.duel import Duel
from ygo_judge.scenario import Setup


db = CardDB()


def start(p1=None, p2=None, seed=1):
    d = Duel(db, Setup.model_validate({"p1": p1 or {}, "p2": p2 or {}, "seed": seed}))
    d.play([])
    return d


def test_normal_draws_start_on_turn_two_and_empty_decks_lose():
    d = start({"deck": ["Dark Magician"]}, {"deck": ["Blue-Eyes White Dragon"]})
    assert "hand 0" in d.board()
    assert not any("draws" in line for line in d.log)
    assert "P2 draws Blue-Eyes White Dragon" in d.play(["End the turn"])
    assert "P1 draws Dark Magician" in d.play(["End the turn"])
    d.play(["End the turn"])
    assert d.result and "P1 wins" in d.result


def test_search_shuffles_but_seeded_replay_is_identical():
    remaining = ["Dark Magician", "Mystical Elf", "Blue-Eyes White Dragon", "Dark Hole", "Monster Reborn", "Marauding Captain"]
    changed = []
    for seed in (1, 2, 3):
        d = start({"hand": ["Reinforcement of the Army"], "deck": ["Goblindbergh", *remaining]}, seed=seed)
        assert ", ".join(["Goblindbergh", *remaining]) in d.board()  # supplied starting order is preserved
        d.play(["Activate Reinforcement of the Army", "P1 Spell & Trap Zone 1", "Goblindbergh"])
        deck = next(line for line in d.board().splitlines() if "Deck (top first)" in line)
        changed.append(", ".join(remaining) not in deck)
        assert all(card in deck for card in remaining)
        rebuilt = Duel(db, d.setup, responses=d.responses)
        assert rebuilt.board() == d.board() and rebuilt.log == d.log
    assert any(changed)


@pytest.mark.parametrize("zone,negated", [(1, True), (2, False)])
def test_column_choice_is_offered_and_changes_resolution(zone, negated):
    d = start({"spells_traps": [{"card": "Infinite Impermanence", "zone": 1}], "hand": ["Dark Hole"]},
              {"monsters": [{"card": "Marauding Captain"}]})
    d.play(["Activate Infinite Impermanence", "Marauding Captain"])
    d.play(["Activate Dark Hole"])
    assert d.decision.kind == "place" and d.decision.auto is None
    out = d.play([f"P1 Spell & Trap Zone {zone}"])
    assert ("its effect is negated" in out) == negated
    assert ("destroyed by an effect" in out) != negated
    d.undo(1)
    assert d.decision.kind == "place"


def test_battle_position_is_a_real_choice():
    d = start({"hand": ["Monster Reborn"], "graveyard": ["Mystical Elf"]})
    d.play(["Activate Monster Reborn", "P1 Spell & Trap Zone 1", "Mystical Elf", "P1 Monster Zone 3"])
    assert d.decision.kind == "position" and d.decision.auto is None
    d.play(["face-up Defense"])
    assert "Mystical Elf, face-up Defense" in d.board()


def test_startup_exposes_legal_draw_phase_activation():
    d = start({"hand": ["Mystical Space Typhoon"]}, {"spells_traps": [{"card": "Call of the Haunted"}]})
    assert d.phase == 0x01 and d.decision.kind == "chain"
    assert "Mystical Space Typhoon" in d.describe_decision()
    out = d.play(["Mystical Space Typhoon", "P1 Spell & Trap Zone 1", "Call of the Haunted"])
    assert "destroyed by an effect" in out


@pytest.mark.parametrize("choice,level", [("7", 7), (7, 8)])
def test_reasoning_distinguishes_numeric_labels_from_indices(choice, level):
    d = start({"hand": ["Reasoning"], "deck": ["Dark Magician", "Mystical Elf"]})
    d.play(["Activate Reasoning", "P1 Spell & Trap Zone 1"])
    out = d.play([choice])
    assert f"P2 declares {level}" in out
    if level == 7:
        assert "GY: Dark Magician" in d.board()
    else:
        assert d.decision.kind == "place"


def test_transmission_gear_waits_for_both_players_and_resolves():
    d = start({"monsters": [{"card": "Marauding Captain"}], "spells_traps": [{"card": "Transmission Gear"}]},
              {"monsters": [{"card": "Mystical Elf"}], "deck_filler": 1})
    d.play(["End the turn", "Enter the Battle Phase", "Attack with Mystical Elf", "Marauding Captain", "Transmission Gear"])
    assert d.decision.kind == "rps" and d.decision.player == 0
    d.play(["Rock"])
    assert d.decision.kind == "rps" and d.decision.player == 1
    out = d.play(["Scissors"])
    assert "Stopped after" not in out and "Mystical Elf: P2 Monster Zone 1 → P2 banished" in out
    assert d.decision.kind != "rps"


@pytest.mark.parametrize("proper", [True, False])
def test_revival_uses_explicit_graveyard_history(proper):
    d = start({"hand": ["Monster Reborn"], "graveyard": [{"card": "Stardust Dragon", "properly_summoned": proper}]})
    assert ("Activate Monster Reborn" in d.describe_decision()) == proper


@pytest.mark.parametrize("proper", [True, False])
def test_field_history_survives_a_move_to_graveyard(proper):
    d = start({"hand": ["Dark Hole", "Monster Reborn"],
               "monsters": [{"card": "Stardust Dragon", "summoned": "special", "properly_summoned": proper}]})
    d.play(["Activate Dark Hole", "P1 Spell & Trap Zone 1", "pass"])
    assert "GY: Stardust Dragon" in d.board()
    assert ("Activate Monster Reborn" in d.describe_decision()) == proper


@pytest.mark.parametrize("setup", [
    {"graveyard": ["Stardust Dragon"]},
    {"banished": ["Black Luster Soldier"]},
    {"monsters": [{"card": "Stardust Dragon", "summoned": "special"}]},
    {"monsters": [{"card": "Chaos Sorcerer"}]},
])
def test_ambiguous_revival_history_is_rejected(setup):
    with pytest.raises(ValueError, match="properly_summoned"):
        start(setup)


@pytest.mark.parametrize("setup", [
    {"turn": 2}, {"p1": {"draw_count": 0}},
    {"p1": {"monsters": [{"card": "Mystical Elf", "summoned_this_turn": True}]}},
    {"p1": {"graveyard": [{"card": "Stardust Dragon", "properly_sumoned": True}]}},
])
def test_unknown_setup_fields_are_rejected(setup):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Setup.model_validate(setup)


def test_initialization_script_error_aborts_the_duel(monkeypatch):
    original = db.script
    broken = b'local s,id=GetID(); function s.initial_effect(c) error("audit_init_error") end'
    monkeypatch.setattr(db, "script", lambda name: broken if name == "c53129443.lua" else original(name))
    with pytest.raises(E.EngineError, match="audit_init_error"):
        start({"hand": ["Dark Hole"]})


def test_runtime_script_error_invalidates_future_results(monkeypatch):
    original = db.script
    script = original("c53129443.lua").replace(b"function s.activate(e,tp,eg,ep,ev,re,r,rp)",
                                               b'function s.activate(e,tp,eg,ep,ev,re,r,rp) error("audit_runtime_error")')
    monkeypatch.setattr(db, "script", lambda name: script if name == "c53129443.lua" else original(name))
    d = start({"hand": ["Dark Hole"]}, {"monsters": [{"card": "Mystical Elf"}]})
    with pytest.raises(E.EngineError, match="audit_runtime_error"):
        d.play(["Activate Dark Hole", "P1 Spell & Trap Zone 1"])
    with pytest.raises(E.EngineError, match="Simulation invalid"):
        d.play([])


def test_card_order_is_offered_and_applied():
    deck = ["Dark Magician", "Mystical Elf", "Blue-Eyes White Dragon", "Marauding Captain", "Goblindbergh"]
    d = start({"monsters": [{"card": "Big Eye", "position": "set"}], "deck": deck})
    d.play(["Change the battle position of Big Eye", "5"])
    assert d.decision.kind == "sort_card" and d.decision.auto is None
    d.play([list(reversed(deck))])
    assert "Deck (top first): " + ", ".join(reversed(deck)) in d.board()


@pytest.mark.parametrize("proper", [True, False])
def test_banished_monsters_use_explicit_history(proper):
    d = start({"hand": ["D.D.R. - Different Dimension Reincarnation", "Mystical Elf"],
               "banished": [{"card": "Stardust Dragon", "properly_summoned": proper}]})
    assert ("Activate D.D.R." in d.describe_decision()) == proper

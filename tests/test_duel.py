import pytest

from ygo_judge.cards import CardDB
from ygo_judge.duel import Duel
from ygo_judge.scenario import Setup

db = CardDB()


def duel(p1: dict, p2: dict | None = None) -> Duel:
    d = Duel(db, Setup.model_validate({"p1": p1, "p2": p2 or {}}), "test")
    d.play([])
    return d


def zone(d: Duel, text: str) -> str:
    return next(line for line in d.board().splitlines() if text in line)


def test_ash_blossom_negates_a_search():
    d = duel({"hand": ["Reinforcement of the Army"], "deck": ["Marauding Captain"]}, {"hand": ["Ash Blossom & Joyous Spring"]})
    out = d.play(["Activate Reinforcement of the Army"])
    assert "P2 may respond to Chain Link 1" in out
    out = d.play(["Ash Blossom"])
    assert "Chain Link 1 (Reinforcement of the Army): its effect is negated" in out
    assert "Marauding Captain" in zone(d, "Deck (top first)")


def test_no_decision_without_a_legal_response():
    d = duel({"hand": ["Dark Hole"]}, {"hand": ["Ash Blossom & Joyous Spring"], "monsters": [{"card": "Mystical Elf"}]})
    out = d.play(["Activate Dark Hole"])
    assert "P2 may respond" not in out
    assert "Mystical Elf: P2 Monster Zone 1 → P2 GY — destroyed by an effect" in out


def test_undo_and_replay_rebuild_the_same_duel():
    d = duel({"hand": ["Reinforcement of the Army"], "deck": ["Marauding Captain"]}, {"hand": ["Ash Blossom & Joyous Spring"]})
    d.play(["Activate Reinforcement of the Army", "Ash Blossom"])
    rebuilt = Duel(db, d.setup, "test", d.responses)
    assert rebuilt.log == d.log and rebuilt.board() == d.board()
    d.undo(1)
    assert d.decision.title == "P2 may respond to Chain Link 1 (Reinforcement of the Army)"
    d.play(["pass"])
    assert "Marauding Captain" in zone(d, "Hand:")


def test_link_summon_then_optional_trigger():
    d = duel({"hand": ["Goblindbergh", "Marauding Captain"], "monsters": [{"card": "Mystical Shine Ball"}], "extra_deck": ["Link Spider"]})
    d.play(["Special Summon Link Spider", "Mystical Shine Ball", "Normal Summon Goblindbergh", "yes", "Marauding Captain"])
    assert "Link Spider" in zone(d, "Extra Monster Zone left")
    assert "Marauding Captain, face-up Attack" in zone(d, "Monster Zone 2")
    assert "Goblindbergh, face-up Defense" in zone(d, "Monster Zone 1")


def test_unknown_cards_are_rejected_not_guessed():
    with pytest.raises(ValueError, match="Unknown card"):
        db.resolve("Nonexistent Card Blah")
    assert db.resolve("ash blossom").name == "Ash Blossom & Joyous Spring"


FLUTE = 93871599


def flute_board() -> dict:
    return {
        "hand": [FLUTE],
        "monsters": [{"card": "Dark Magician"}],
        "deck": ["Blue-Eyes White Dragon"],
        "deck_filler": 2,
    }


def activate_flute(d: Duel) -> None:
    d.play([f"Activate {db.get(FLUTE).name}", "Blue-Eyes White Dragon", "face-up Attack"])
    assert db.get(FLUTE).name in zone(d, "GY:")
    assert "Blue-Eyes White Dragon" in d.board()


def test_first_turn_has_no_battle_phase_or_main_phase_2_for_flute():
    d = duel(flute_board(), {"deck_filler": 2})
    activate_flute(d)
    assert (d.turn, d.turn_player, d.phase) == (1, 0, 0x04)
    assert "Enter the Battle Phase" not in d.describe_decision()
    assert "Go to Main Phase 2" not in d.describe_decision()
    assert f"Activate {db.get(FLUTE).name}" not in d.describe_decision()
    for choice in ("Enter the Battle Phase", "Go to Main Phase 2"):
        d.play([choice])
        assert (d.turn, d.turn_player, d.phase) == (1, 0, 0x04)
    d.play(["End the turn"])
    assert (d.turn, d.turn_player, d.phase) == (2, 1, 0x04)
    assert "P1 enters the Battle Phase" not in d.log
    assert "P1 enters the Main Phase 2" not in d.log
    assert not d.errors


@pytest.mark.parametrize("turn,player", [(2, 1), (3, 0)])
def test_flute_draw_is_legal_after_battle_phase_on_later_turns(turn, player):
    players = [{"deck_filler": 2}, {"deck_filler": 2}]
    players[player] = flute_board()
    d = duel(*players)
    d.play(["End the turn"] * (turn - 1))
    assert (d.turn, d.turn_player, d.phase) == (turn, player, 0x04)
    activate_flute(d)
    d.play(["Enter the Battle Phase", "Go to Main Phase 2"])
    assert d.phase == 0x100
    assert f"Activate {db.get(FLUTE).name}" in d.describe_decision()
    out = d.play([f"Activate {db.get(FLUTE).name}"])
    assert f"P{player + 1} draws Mystical Shine Ball" in out
    assert db.get(FLUTE).name in zone(d, "Banished:")
    rebuilt = Duel(db, d.setup, "test", d.responses)
    assert rebuilt.log == d.log and rebuilt.board() == d.board()
    d.undo(1)
    assert d.phase == 0x100
    assert f"Activate {db.get(FLUTE).name}" in d.describe_decision()
    assert not d.errors


def test_flute_sent_on_turn_1_cannot_draw_on_turn_3():
    d = duel(flute_board(), {"deck_filler": 2})
    activate_flute(d)
    d.play(["End the turn", "End the turn", "Enter the Battle Phase", "Go to Main Phase 2"])
    assert (d.turn, d.turn_player, d.phase) == (3, 0, 0x100)
    assert db.get(FLUTE).name in zone(d, "GY:")
    assert f"Activate {db.get(FLUTE).name}" not in d.describe_decision()
    assert not d.errors

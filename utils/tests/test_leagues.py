from types import SimpleNamespace

from utils.commands import Commands


def team(name, abbreviation, wins=0, losses=0, points_for=0):
    return SimpleNamespace(
        team_name=name,
        team_abbrev=abbreviation,
        wins=wins,
        losses=losses,
        points_for=points_for,
    )


class FakeLeague:
    current_week = 2

    def __init__(self):
        self.teams = [
            team("Alpha", "ALP", wins=1, losses=0, points_for=250),
            team("Beta", "BET", wins=0, losses=1, points_for=200),
        ]
        self.refresh_count = 0

    def refresh(self):
        self.refresh_count += 1

    def box_scores(self, week=None):
        return [
            SimpleNamespace(
                home_team=self.teams[0],
                away_team=self.teams[1],
                home_score=120.25,
                away_score=115.5,
                home_lineup=[],
                away_lineup=[],
            )
        ]


def test_scoreboard_uses_current_box_scores():
    result = Commands(FakeLeague()).get_scoreboard_short()

    assert result == "Score Update\nALP 120.25 - 115.50 BET"


def test_matchups_include_records():
    result = Commands(FakeLeague()).get_matchups()

    assert result == "Matchups:\nAlpha(1-0) vs Beta(0-1)"


def test_standings_add_top_half_wins():
    result = Commands(FakeLeague()).get_standings()

    assert result == "Current Standings:\n1: Alpha (2 - 0) (+1)\n2: Beta (0 - 1) (+0)"

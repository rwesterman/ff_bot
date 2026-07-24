import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from espn_api.football import League

from utils.commands import Commands


pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def league_settings():
    env_file = Path(__file__).parents[2] / ".env"
    if not env_file.is_file():
        pytest.skip("A repository-level .env file is required for live ESPN tests")

    load_dotenv(env_file, override=False)

    league_id = os.environ.get("LEAGUE_ID")
    league_year = os.environ.get("LEAGUE_YEAR")

    if not league_id or not league_year:
        pytest.skip("LEAGUE_ID and LEAGUE_YEAR must be set in .env")

    kwargs = {}

    if espn_s2 := os.environ.get("ESPN_S2"):
        kwargs["espn_s2"] = espn_s2

    if swid := os.environ.get("SWID"):
        kwargs["swid"] = swid if swid.startswith("{") else f"{{{swid}}}"

    return {
        "league_id": int(league_id),
        "league_year": int(league_year),
        "credentials": kwargs,
    }


@pytest.fixture(scope="module")
def commands(league_settings):
    league = League(
        league_id=league_settings["league_id"],
        year=league_settings["league_year"],
        **league_settings["credentials"],
    )
    return Commands(league)


@pytest.fixture(scope="module")
def previous_year_commands(league_settings):
    league = League(
        league_id=league_settings["league_id"],
        year=league_settings["league_year"] - 1,
        **league_settings["credentials"],
    )
    return Commands(league)


def test_live_league_is_accessible(commands):
    assert commands.league.teams
    assert all(team.team_name for team in commands.league.teams)


def test_live_scoreboard(commands):
    if commands.league.current_week < 1:
        pytest.skip("ESPN box-score data is unavailable before week 1")

    result = commands.get_scoreboard_short()

    assert result.startswith("Score Update")
    assert len(result.splitlines()) > 1


def test_live_matchups(commands):
    if commands.league.current_week < 1:
        pytest.skip("ESPN matchup data is unavailable before week 1")

    result = commands.get_matchups()

    assert result.startswith("Matchups:")
    assert len(result.splitlines()) > 1


def test_live_standings(commands):
    result = commands.get_standings()

    assert result.startswith("Current Standings:")
    assert len(result.splitlines()) > 1


def test_previous_year_scoreboard(previous_year_commands):
    result = previous_year_commands.get_scoreboard_short(week=1)

    assert result.startswith("Score Update")
    assert len(result.splitlines()) > 1


def test_previous_year_matchups(previous_year_commands):
    result = previous_year_commands.get_matchups(week=1)

    assert result.startswith("Matchups:")
    assert len(result.splitlines()) > 1

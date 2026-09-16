from dataclasses import replace
from contextlib import closing
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from utils.commands import Commands
from utils.penalties import (
    CORE_URL,
    SCOREBOARD_URL,
    EspnPenaltySource,
    Penalty,
    PenaltyMonitor,
    PenaltyStore,
    announcement,
    deliver_pending,
    record_starter_penalties,
)


@pytest.fixture
def store(tmp_path):
    return PenaltyStore(tmp_path / "bonuses.db", 123, 2026)


@pytest.fixture
def penalty():
    return Penalty("game1", "play1", 0, 42, "Taunting", "PENALTY on TEST-A.Player, Taunting, 15 yards.", 1)


def player(player_id=42, position="WR", slot="WR"):
    return SimpleNamespace(
        playerId=player_id,
        name="Avery Player",
        position=position,
        slot_position=slot,
        points=20,
        projected_points=20,
        game_played=100,
    )


def team(team_id, wins=0, losses=0, ties=0, points_for=0):
    return SimpleNamespace(
        team_id=team_id,
        team_name=f"Team {team_id}",
        team_abbrev=f"T{team_id}",
        wins=wins,
        losses=losses,
        ties=ties,
        points_for=points_for,
        owner="League Manager",
    )


@pytest.fixture
def league():
    home = team(1, wins=1, points_for=100)
    away = team(2, losses=1, points_for=95)
    box = SimpleNamespace(
        home_team=home,
        away_team=away,
        home_score=100,
        away_score=95,
        home_lineup=[],
        away_lineup=[player()],
    )
    return SimpleNamespace(
        current_week=2,
        teams=[home, away],
        refresh=Mock(),
        box_scores=Mock(return_value=[box]),
        standings=Mock(return_value=[home, away]),
        settings=SimpleNamespace(reg_season_count=14, matchup_periods={1: [1], 2: [2]}),
    )


def test_award_persists_and_is_scoped_and_idempotent(store, penalty):
    assert store.award(penalty, team(2), player())
    restarted = PenaltyStore(store.path, 123, 2026)
    assert not restarted.award(penalty, team(3), player())
    assert restarted.totals(1) == {2: 10}
    assert restarted.totals(2) == {}
    assert PenaltyStore(store.path, 999, 2026).totals(1) == {}
    assert PenaltyStore(store.path, 123, 2025).totals(1) == {}
    row = restarted.pending()[0]
    assert (row["team_id"], row["player_name"], row["penalty_name"], row["description"], row["week"]) == (
        2,
        "Avery Player",
        "Taunting",
        penalty.description,
        1,
    )
    restarted.mark_notified(row["id"], 987)
    assert restarted.pending() == []
    assert restarted.totals(1) == {2: 10}


@pytest.mark.parametrize(
    "position,slot,eligible",
    [
        ("WR", "WR", True),
        ("QB", "OP", True),
        ("RB", "RB/WR/TE", True),
        ("TE", "WR/TE", True),
        ("K", "K", True),
        ("WR", "BE", False),
        ("RB", "IR", False),
        ("WR", "FA", False),
        ("LB", "OP", False),
        ("D/ST", "D/ST", False),
    ],
)
def test_weekly_starter_eligibility(store, penalty, league, position, slot, eligible):
    box = league.box_scores()[0]
    box.away_lineup = [player(position=position, slot=slot)]
    assert record_starter_penalties(store, [penalty], [box]) == int(eligible)


def test_multiple_flags_and_bye_and_unknown_player(store, penalty, league):
    box = league.box_scores()[0]
    box.home_team = None
    events = [penalty, replace(penalty, occurrence=1), replace(penalty, player_id=999, play_id="other")]
    assert record_starter_penalties(store, events, [box]) == 2
    assert store.totals(1) == {2: 20}
    assert record_starter_penalties(store, events, [box]) == 0


def test_announcements(store, penalty):
    store.award(penalty, team(2), player())
    row = store.pending()[0]
    assert announcement(row, 1) == (
        "Avery Player has been flagged for Unsportsmanlike Conduct! "
        "Team 2 will be awarded ten additional points this week."
    )
    assert announcement(row, 2).endswith("in Week 1.")
    assert len(announcement(dict(row, team_name="x" * 3000), 1)) < 2000


def test_notification_failure_retries_without_reawarding(store, penalty):
    store.award(penalty, team(2), player())
    send = AsyncMock(side_effect=RuntimeError("Discord unavailable"))
    with pytest.raises(RuntimeError):
        asyncio.run(deliver_pending(store, 1, send))
    assert len(store.pending()) == 1
    restarted = PenaltyStore(store.path, 123, 2026)
    send = AsyncMock(return_value=99)
    asyncio.run(deliver_pending(restarted, 1, send))
    asyncio.run(deliver_pending(restarted, 1, send))
    assert send.await_count == 1
    assert restarted.totals(1) == {2: 10}
    assert restarted.pending() == []


def play(text="PENALTY on TEST-A.Player, Taunting, 15 yards.", ids=(42,)):
    return {
        "id": "play1",
        "text": text,
        "participants": [{"type": "penalized", "athlete": {"$ref": f"{CORE_URL}/athletes/{pid}"}} for pid in ids],
    }


@pytest.mark.parametrize("name", ["Taunting", "Unsportsmanlike Conduct"])
@pytest.mark.parametrize("ending", ["15 yards.", "declined.", "offsetting."])
def test_source_uses_penalized_id_not_scorer(name, ending):
    event = play(f"TOUCHDOWN. PENALTY on TEST-A.Player, {name}, {ending}")
    event["participants"].insert(0, {"type": "scorer", "athlete": {"$ref": f"{CORE_URL}/athletes/99"}})
    penalties = EspnPenaltySource().parse_play(2026, 1, "game1", event)
    assert len(penalties) == 1
    assert penalties[0].player_id == 42
    assert penalties[0].name == name


def test_source_multiple_penalties_resolves_names(requests_mock):
    requests_mock.get(f"{CORE_URL}/seasons/2026/athletes/42", json={"shortName": "A. Player"})
    requests_mock.get(f"{CORE_URL}/seasons/2026/athletes/43", json={"shortName": "B. Other"})
    event = play(
        "PENALTY on TEST-B.Other, Holding, 10 yards. "
        "PENALTY on TEST-A.Player, Taunting, 15 yards. "
        "PENALTY on TEST-B.Other, Unsportsmanlike Conduct, 15 yards.",
        ids=(43, 42),
    )
    penalties = EspnPenaltySource().parse_play(2026, 1, "game1", event)
    assert [(p.player_id, p.occurrence, p.name) for p in penalties] == [
        (42, 0, "Taunting"),
        (43, 0, "Unsportsmanlike Conduct"),
    ]


def test_unattributed_or_ambiguous_flags_do_not_award(requests_mock, caplog):
    source = EspnPenaltySource()
    assert source.parse_play(2026, 1, "game1", play(ids=())) == []
    assert "Unresolved penalty player" in caplog.text
    for pid in (42, 43):
        requests_mock.get(f"{CORE_URL}/seasons/2026/athletes/{pid}", json={"shortName": "A.Player"})
    assert source.parse_play(2026, 1, "game1", play(ids=(42, 43))) == []
    assert source.parse_play(2026, 1, "game1", play("PENALTY on TEST-A.Player, Holding, 10 yards.")) == []


def test_source_week_selection_and_pagination(requests_mock):
    requests_mock.get(
        SCOREBOARD_URL,
        json={
            "season": {"year": 2026, "type": 2},
            "week": {"number": 1},
            "events": [
                {"id": "game1", "status": {"type": {"state": "post"}}},
                {"id": "future", "status": {"type": {"state": "pre"}}},
            ],
        },
    )
    url = f"{CORE_URL}/events/game1/competitions/game1/plays"
    requests_mock.get(url + "?page=1&limit=400", json={"items": [play()], "pageCount": 2})
    requests_mock.get(url + "?page=2&limit=400", json={"items": [dict(play(), id="play2")], "pageCount": 2})
    assert len(EspnPenaltySource().fetch_week(2026, 1)) == 2
    assert requests_mock.request_history[0].qs == {
        "dates": ["2026"],
        "seasontype": ["2"],
        "week": ["1"],
        "limit": ["100"],
    }


def test_source_failure_does_not_mean_no_penalties(requests_mock):
    requests_mock.get(SCOREBOARD_URL, status_code=503)
    with pytest.raises(requests.HTTPError):
        EspnPenaltySource().fetch_week(2026, 1)
    requests_mock.get(SCOREBOARD_URL, json={"season": {"year": 2025}, "week": {"number": 1}})
    with pytest.raises(ValueError, match="different season/week"):
        EspnPenaltySource().fetch_week(2026, 1)


def test_poll_catches_up_and_uses_event_week_lineups(store, penalty, league):
    league.current_week = 4
    boxes = league.box_scores.return_value
    league.box_scores.side_effect = lambda week: boxes if week == 1 else []
    source = Mock()
    source.fetch_week.side_effect = lambda season, week: [replace(penalty, week=week)]
    monitor = PenaltyMonitor(Commands(league, store), store, source)
    monitor.poll()
    assert [call.args[1] for call in source.fetch_week.call_args_list] == [1, 2, 3, 4]
    assert store.totals(1) == {2: 10}
    assert store.totals(4) == {}
    source.reset_mock()
    monitor.poll()
    assert [call.args[1] for call in source.fetch_week.call_args_list] == [3, 4]
    assert len(store.pending()) == 1
    assert league.refresh.call_count == 2


def test_failed_catchup_retries_without_duplicate_points(store, penalty, league):
    source = Mock()
    source.fetch_week.side_effect = [[penalty], requests.Timeout(), [penalty], []]
    monitor = PenaltyMonitor(Commands(league, store), store, source)
    with pytest.raises(requests.Timeout):
        monitor.poll()
    assert not monitor.caught_up
    monitor.poll()
    assert store.totals(1) == {2: 10}
    assert monitor.caught_up


def test_scores_and_standings_flip_without_mutating_espn(store, penalty, league):
    store.award(penalty, team(2), player())
    commander = Commands(league, store)
    for _ in range(2):
        assert commander.get_scoreboard_short(1) == "Score Update\nT1 100.00 - 105.00 T2"
        assert commander.get_standings() == "Current Standings:\n1: Team 2 (2 - 0) (+1)\n2: Team 1 (0 - 1) (+0)"
    assert league.box_scores()[0].away_score == 95
    assert "T1 100.00 - 95.00 T2" in commander.get_scoreboard_short(2)
    assert "T1 100.00 - 105.00 T2" in commander.get_final()
    assert "Team 2 barely beat Team 1" in commander.get_final()


def test_current_week_bonus_does_not_change_standings(store, penalty, league):
    store.award(replace(penalty, week=2), team(2), player())
    commander = Commands(league, store)
    assert "T1 100.00 - 105.00 T2" in commander.get_scoreboard_short()
    assert "1: Team 1 (2 - 0) (+1)" in commander.get_standings()
    assert "T1 0.00 - 30.00 T2" in commander.get_projected_scoreboard()


def test_bonus_creates_head_to_head_tie(store, penalty, league):
    league.box_scores()[0].away_score = 90
    store.award(penalty, team(2), player())
    result = Commands(league, store).get_standings()
    assert "Team 1 (1 - 0 - 1)" in result
    assert "Team 2 (0 - 0 - 1)" in result


def test_playoff_score_combines_weeks_but_standings_exclude_playoffs(store, penalty, league):
    league.current_week = 17
    league.settings.matchup_periods = {15: [15, 16], 16: [17, 18]}
    store.award(replace(penalty, week=15), team(2), player())
    store.award(replace(penalty, week=16, play_id="play2"), team(2), player())
    commander = Commands(league, store)
    assert "T1 100.00 - 115.00 T2" in commander.get_scoreboard_short(16)
    assert "1: Team 1 (15 - 0) (+14)" in commander.get_standings()


def test_points_for_tiebreaker_includes_bonuses(store, penalty, league):
    first, second = league.teams
    first.points_for = 500
    second.points_for = 495
    second.wins, second.losses = 1, 0
    third, fourth = team(3, losses=1), team(4, losses=1)
    league.teams.extend([third, fourth])
    league.box_scores.return_value = [
        SimpleNamespace(home_team=first, away_team=third, home_score=90, away_score=80),
        SimpleNamespace(home_team=second, away_team=fourth, home_score=120, away_score=70),
    ]
    assert "1: Team 1" in Commands(league, store).get_standings()
    store.award(penalty, second, player())
    assert "1: Team 2 (2 - 0) (+1)" in Commands(league, store).get_standings()


def test_reclassified_flag_is_not_a_second_bonus(store, penalty):
    store.award(penalty, team(2), player())
    assert not store.award(replace(penalty, name="Unsportsmanlike Conduct"), team(2), player())
    assert store.totals(1) == {2: 10}


def test_manual_refresh_fetches_week_and_keeps_new_awards_silent_after_restart(store, penalty, league):
    commander = Commands(league, store)
    source = Mock()
    source.fetch_week.return_value = [penalty]
    commander.penalty_monitor.source = source
    pages = commander.refresh_penalties(1)
    source.fetch_week.assert_called_once_with(2026, 1)
    league.box_scores.assert_called_once_with(week=1)
    assert "Silent" in pages[0]
    assert store.totals(1) == {2: 10}
    assert store.pending() == []
    assert not commander.penalty_monitor.caught_up

    restarted = PenaltyStore(store.path, 123, 2026)
    monitor = PenaltyMonitor(Commands(league, restarted), restarted, source)
    source.fetch_week.side_effect = lambda season, week: [penalty] if week == 1 else []
    monitor.poll()
    assert restarted.totals(1) == {2: 10}
    send = AsyncMock()
    asyncio.run(deliver_pending(restarted, 2, send))
    send.assert_not_awaited()


def test_manual_refresh_preserves_existing_pending_announcements(store, penalty, league):
    store.award(penalty, team(2), player())
    commander = Commands(league, store)
    commander.penalty_monitor.source = Mock()
    commander.penalty_monitor.source.fetch_week.return_value = [penalty, replace(penalty, play_id="new")]
    pages = commander.refresh_penalties(1)
    assert "Pending" in pages[0] and "Silent" in pages[0]
    assert [row["play_id"] for row in store.pending()] == ["play1"]
    assert store.totals(1) == {2: 20}


def test_manual_refresh_failure_propagates_and_future_week_is_rejected(store, league):
    commander = Commands(league, store)
    commander.penalty_monitor.source = Mock()
    commander.penalty_monitor.source.fetch_week.side_effect = requests.Timeout()
    with pytest.raises(requests.Timeout):
        commander.refresh_penalties(1)
    assert store.for_week(1) == []
    commander.penalty_monitor.source.reset_mock()
    with pytest.raises(ValueError, match="future week"):
        commander.refresh_penalties(3)
    commander.penalty_monitor.source.fetch_week.assert_not_called()


def test_existing_ledger_upgrade_preserves_notification_state(store, penalty):
    store.award(penalty, team(2), player())
    store.mark_notified(store.pending()[0]["id"], 99)
    store.award(replace(penalty, play_id="pending"), team(2), player())
    with closing(store.connect()) as db, db:
        db.execute("ALTER TABLE penalty_bonuses DROP COLUMN notification_suppressed")
    upgraded = PenaltyStore(store.path, 123, 2026)
    assert upgraded.totals(1) == {2: 20}
    assert [row["play_id"] for row in upgraded.pending()] == ["pending"]
    assert upgraded.for_week(1)[0]["discord_message_id"] == "99"
    PenaltyStore(store.path, 123, 2026)  # Reopening the upgraded schema is safe.


def test_penalty_table_reads_only_selected_week_league_and_season(store, penalty):
    store.award(penalty, team(2), player())
    store.mark_notified(store.pending()[0]["id"], 99)
    store.award(replace(penalty, play_id="second", name="Unsportsmanlike Conduct"), team(2), player())
    store.award(replace(penalty, play_id="next-week", week=2), team(3), player())
    for league_id, season in [(999, 2026), (123, 2025)]:
        PenaltyStore(store.path, league_id, season).award(penalty, team(4), player())
    # No League object is needed: this command must not make ESPN requests.
    pages = Commands(None, store).get_penalties(1)
    assert len(pages) == 1
    table = pages[0]
    for expected in [
        "Week 1",
        "2026",
        "2 penalties | +20 points total",
        "Team 2",
        "Avery Player",
        "Taunting",
        "Unsportsmanlike Conduct",
        "Sent",
        "Pending",
    ]:
        assert expected in table
    assert "Team 3" not in table
    assert "Team 4" not in table
    assert len(store.pending()) == 2  # Reading neither sends notices nor alters the ledger.


def test_penalty_table_empty_and_unconfigured(store):
    assert Commands(None, store).get_penalties(1) == [
        "Week 1 penalty bonuses (2026): No applicable penalties logged yet."
    ]
    assert Commands(None).get_penalties(1) == ["The penalty bonus database is not configured."]


@pytest.mark.parametrize("week", [-1, 0, 19])
def test_penalty_table_rejects_invalid_weeks(store, week):
    with pytest.raises(ValueError, match="between 1 and 18"):
        Commands(None, store).get_penalties(week)


def test_penalty_table_paginates_all_rows_and_bounds_discord_size(store, penalty):
    for index in range(19):
        owner = team(index)
        owner.team_name = "```\n" + "🏈" * 100
        athlete = player()
        athlete.name = f"Player {index:02} " + "🏈" * 100
        store.award(replace(penalty, play_id=str(index)), owner, athlete)
    pages = Commands(None, store).get_penalties(1)
    assert len(pages) == 3
    for index in range(19):
        assert sum(f"Player {index:02}" in page for page in pages) == 1
    for index, page in enumerate(pages, start=1):
        assert f"page {index}/3" in page
        assert "19 penalties | +190 points total" in page
        assert page.count("```") == 2
        assert len(page.encode("utf-16-le")) // 2 <= 2000

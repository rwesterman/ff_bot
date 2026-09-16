from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from utils import contest
from utils.contest import (
    ContestLedger,
    ContestService,
    PlayerWeek,
    ResolutionContext,
    build_payouts,
    format_season,
    format_week,
    high_scoring_teams,
    settle_week,
    snapshot_week,
    split_pot,
)
from utils.contest_schedule import SCHEDULE, Contest, contest_for_week


def row(
    week=1,
    team_id=1,
    player_id=100,
    name="Player",
    position="WR",
    slot="RB/WR/TE",
    pro_team="BUF",
    points=10.0,
    stats=None,
    scoring=None,
    game_date=None,
):
    return PlayerWeek(
        week=week,
        team_id=team_id,
        team_name=f"Team {team_id}",
        player_id=player_id,
        player_name=name,
        position=position,
        slot_position=slot,
        pro_team=pro_team,
        points=points,
        stats=stats or {},
        scoring=scoring or {},
        game_date=game_date,
    )


def context(rows, resolver="highest_scoring_starter", week=1, lookback_to=0, **kwargs):
    weeks = {}
    for entry in rows:
        weeks.setdefault(entry.week, []).append(entry)
    stub = Contest(week=week, name="Test", rule="Test", resolver=resolver, lookback_to=lookback_to)
    return ResolutionContext(contest=stub, weeks=weeks, **kwargs)


class TestSplitPot:
    def test_even_split(self):
        assert split_pot(2000, 2) == [1000, 1000]

    def test_remainder_is_distributed_without_losing_cents(self):
        shares = split_pot(2000, 3)

        assert shares == [667, 667, 666]
        assert sum(shares) == 2000

    def test_single_winner_takes_the_pot(self):
        assert split_pot(2000, 1) == [2000]


def test_high_score_uses_starters_only():
    rows = [
        row(team_id=1, player_id=1, points=30.0),
        row(team_id=1, player_id=2, points=99.0, slot="BE"),
        row(team_id=2, player_id=3, points=40.0),
    ]

    winners = high_scoring_teams(rows)

    assert [winner.team_id for winner in winners] == [2]
    assert winners[0].detail == "40.00 points"


def test_high_score_tie_returns_both_teams():
    rows = [row(team_id=1, player_id=1, points=25.0), row(team_id=2, player_id=2, points=25.0)]

    assert [winner.team_id for winner in high_scoring_teams(rows)] == [1, 2]


def test_week_1_highest_scoring_starter_ignores_bench():
    rows = [
        row(team_id=1, player_id=1, name="Starter", points=48.3),
        row(team_id=2, player_id=2, name="Benched", points=60.0, slot="BE"),
    ]

    winners = contest.highest_scoring_starter(context(rows))

    assert len(winners) == 1
    assert winners[0].detail == "Starter scored 48.30"


def test_conference_contests_split_by_pro_team():
    rows = [
        row(team_id=1, player_id=1, pro_team="KC", points=20.0),
        row(team_id=1, player_id=2, pro_team="DAL", points=30.0),
        row(team_id=2, player_id=3, pro_team="BUF", points=25.0),
    ]

    afc = contest.most_afc_points(context(rows))
    nfc = contest.most_nfc_points(context(rows))

    assert [winner.team_id for winner in afc] == [2]
    assert [winner.team_id for winner in nfc] == [1]


def test_first_down_bonus_reads_espn_stat_ids():
    rows = [
        row(team_id=1, player_id=1, scoring={"211": 0.6, "213": 1.0}),
        row(team_id=2, player_id=2, scoring={"212": 1.0}),
        row(team_id=2, player_id=3, scoring={"213": 0.4}, slot="BE"),
    ]

    winners = contest.most_first_down_points(context(rows))

    assert [winner.team_id for winner in winners] == [1]
    assert winners[0].detail == "1.60 first-down bonus points"


def test_kicker_and_defense_contests_use_lineup_slot():
    rows = [
        row(team_id=1, player_id=1, position="K", slot="K", points=20.0),
        row(team_id=2, player_id=2, position="K", slot="BE", points=30.0),
        row(team_id=2, player_id=3, position="D/ST", slot="D/ST", points=25.0),
    ]

    assert [w.team_id for w in contest.highest_kicker_points(context(rows))] == [1]
    assert [w.team_id for w in contest.highest_defense_points(context(rows))] == [2]


def test_gronk_challenge_counts_tight_ends_started_in_flex():
    rows = [
        row(team_id=1, player_id=1, position="TE", slot="TE", stats={"receivingYards": 40.0}),
        row(team_id=2, player_id=2, position="TE", slot="RB/WR/TE", stats={"receivingYards": 68.0}),
        row(team_id=3, player_id=3, position="TE", slot="BE", stats={"receivingYards": 69.0}),
    ]

    winners = contest.tight_end_closest_to_69(context(rows))

    assert [winner.team_id for winner in winners] == [2]
    assert winners[0].detail == "Player at 68 yards (1 away)"


def test_gronk_challenge_splits_when_equally_far_from_69():
    rows = [
        row(team_id=1, player_id=1, position="TE", slot="TE", stats={"receivingYards": 66.0}),
        row(team_id=2, player_id=2, position="TE", slot="TE", stats={"receivingYards": 72.0}),
    ]

    assert [winner.team_id for winner in contest.tight_end_closest_to_69(context(rows))] == [1, 2]


def test_receptions_contest_breaks_ties_on_receiving_yards():
    rows = [
        row(team_id=1, player_id=1, name="Even", stats={"receivingReceptions": 9.0, "receivingYards": 105.0}),
        row(team_id=2, player_id=2, name="Ahead", stats={"receivingReceptions": 9.0, "receivingYards": 144.0}),
    ]

    winners = contest.most_receptions(context(rows))

    assert [winner.team_id for winner in winners] == [2]
    assert winners[0].detail == "Ahead with 9 catches for 144 yards"


def test_cumulative_skill_player_counts_started_weeks_only():
    rows = [
        row(week=1, team_id=1, player_id=1, name="Star", points=30.0),
        row(week=2, team_id=1, player_id=1, name="Star", points=40.0, slot="BE"),
        row(week=1, team_id=2, player_id=2, name="Steady", points=20.0),
        row(week=2, team_id=2, player_id=2, name="Steady", points=25.0),
    ]

    winners = contest.best_cumulative_skill_player(context(rows, lookback_to=1, week=2))

    assert [winner.team_id for winner in winners] == [2]
    assert winners[0].detail == "Steady scored 45.00 while started"


def test_cumulative_credits_each_owner_only_for_weeks_they_held_the_player():
    rows = [
        row(week=1, team_id=1, player_id=7, name="Traded", points=30.0),
        row(week=2, team_id=2, player_id=7, name="Traded", points=30.0),
    ]

    winners = contest.best_cumulative_skill_player(context(rows, lookback_to=1, week=2))

    assert [winner.team_id for winner in winners] == [1, 2]
    assert all(winner.detail == "Traded scored 30.00 while started" for winner in winners)


def test_keeper_king_ignores_teams_without_a_keeper():
    rows = [
        row(week=1, team_id=1, player_id=11, name="Keeper", points=50.0),
        row(week=1, team_id=2, player_id=22, name="Not a keeper", points=90.0),
    ]

    winners = contest.best_keeper(context(rows, lookback_to=1, keepers={1: 11}))

    assert [winner.team_id for winner in winners] == [1]


def test_keeper_king_returns_nothing_without_keeper_data():
    assert contest.best_keeper(context([row()])) == []


def test_thanksgiving_contest_filters_to_wednesday_through_friday():
    rows = [
        row(team_id=1, player_id=1, name="Sunday", points=40.0, game_date=datetime(2026, 11, 29, 13).isoformat()),
        row(team_id=2, player_id=2, name="Thursday", points=35.0, game_date=datetime(2026, 11, 26, 13).isoformat()),
        row(
            team_id=3,
            player_id=3,
            name="Benched Friday",
            points=30.0,
            slot="BE",
            game_date=datetime(2026, 11, 27, 15).isoformat(),
        ),
    ]

    winners = contest.best_thanksgiving_player(context(rows))

    assert [winner.team_id for winner in winners] == [2]


def test_waiver_pickup_excludes_anyone_rostered_in_week_one():
    rows = [
        row(week=1, team_id=1, player_id=1, name="Drafted", points=10.0),
        row(week=2, team_id=1, player_id=1, name="Drafted", points=90.0),
        row(week=2, team_id=2, player_id=2, name="Pickup", points=20.0),
        row(week=3, team_id=2, player_id=2, name="Pickup", points=25.0),
    ]

    winners = contest.best_waiver_pickup(context(rows, lookback_to=1, week=3))

    assert [winner.team_id for winner in winners] == [2]
    assert winners[0].detail == "Pickup scored 45.00 while started"


def test_penalty_contest_applies_the_ten_point_house_rule():
    winners = contest.manual_penalties(context([row()], penalties={1: ("Team 1", 3), 2: ("Team 2", 1)}))

    assert [winner.team_id for winner in winners] == [1]
    assert winners[0].detail == "30 penalty points"


def test_every_scheduled_contest_has_a_resolver():
    assert all(entry.resolver in contest.RESOLVERS for entry in SCHEDULE)


def test_cumulative_contests_declare_their_lookback():
    for entry in SCHEDULE:
        if entry.resolver in {"best_cumulative_skill_player", "best_keeper", "best_waiver_pickup"}:
            assert entry.weeks_required()[0] == 1


def test_payouts_split_both_pots_and_preserve_the_totals():
    high = [contest.Winner(1, "Team 1", "a"), contest.Winner(2, "Team 2", "b")]
    side = [contest.Winner(3, "Team 3", "c")]

    payouts = build_payouts(contest_for_week(1), high, side, week=1)

    assert sum(p.amount_cents for p in payouts if p.category == contest.HIGH_SCORE) == 2000
    assert sum(p.amount_cents for p in payouts if p.category == contest.SIDE_CONTEST) == 2000


def test_unresolved_side_contest_still_pays_the_high_score():
    payouts = build_payouts(contest_for_week(13), [contest.Winner(1, "Team 1", "a")], [], week=13)

    assert [payout.category for payout in payouts] == [contest.HIGH_SCORE]


class FakePlayer:
    def __init__(self, player_id, name, points, slot="RB/WR/TE", position="WR", pro_team="BUF", game_date=None):
        self.playerId = player_id
        self.name = name
        self.points = points
        self.slot_position = slot
        self.position = position
        self.proTeam = pro_team
        self.breakdown = {"receivingYards": points * 2}
        self.points_breakdown = {"213": 1.0}
        self.game_date = game_date


class FakeLeague:
    def __init__(self):
        self.teams = [SimpleNamespace(team_id=1, team_name="Alpha"), SimpleNamespace(team_id=2, team_name="Beta")]
        self.draft = []
        self.calls = []
        self.current_week = 1
        self.refreshes = 0

    def refresh(self):
        self.refreshes += 1

    def box_scores(self, week=None):
        self.calls.append(week)
        return [
            SimpleNamespace(
                home_team=self.teams[0],
                away_team=self.teams[1],
                home_lineup=[FakePlayer(1, "Alpha Star", 30.0), FakePlayer(2, "Alpha Bench", 99.0, slot="BE")],
                away_lineup=[FakePlayer(3, "Beta Star", 25.0)],
            )
        ]


def test_snapshot_week_captures_bench_and_lineup_slots():
    rows = snapshot_week(FakeLeague(), 1)

    assert len(rows) == 3
    assert {row_.slot_position for row_ in rows} == {"RB/WR/TE", "BE"}
    assert sum(1 for row_ in rows if row_.started) == 2


def test_ledger_round_trips_a_week(tmp_path):
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        ledger.store_week(snapshot_week(FakeLeague(), 1))

        loaded = ledger.load_week(1)

        assert len(loaded) == 3
        assert ledger.cached_weeks() == {1}
        assert loaded[0].stats["receivingYards"] == 60.0


def test_storing_a_week_twice_updates_instead_of_duplicating(tmp_path):
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        ledger.store_week(snapshot_week(FakeLeague(), 1))
        ledger.store_week(snapshot_week(FakeLeague(), 1))

        assert len(ledger.load_week(1)) == 3


def test_settle_week_records_both_pots_and_totals(tmp_path):
    league = FakeLeague()
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        payouts = settle_week(ledger, league, 1)

        assert sum(payout.amount_cents for payout in payouts) == 4000
        assert ledger.is_settled(1)
        assert ledger.season_totals()[0] == ("Alpha", 4000, 2)


def test_resettling_a_week_replaces_rather_than_doubles_payouts(tmp_path):
    league = FakeLeague()
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        settle_week(ledger, league, 1)
        settle_week(ledger, league, 1)

        assert sum(payout.amount_cents for payout in ledger.week_payouts(1)) == 4000


def test_restoring_a_week_drops_players_no_longer_rostered(tmp_path):
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        ledger.store_week([row(week=3, player_id=1, name="Kept"), row(week=3, player_id=2, name="Dropped")])
        ledger.store_week([row(week=3, player_id=1, name="Kept")])

        assert [entry.player_name for entry in ledger.load_week(3)] == ["Kept"]


def test_unsettled_weeks_are_refetched_so_late_games_land(tmp_path):
    league = FakeLeague()
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        contest.sync_weeks(ledger, league, [1])
        contest.sync_weeks(ledger, league, [1])

        assert league.calls == [1, 1]


def test_settled_weeks_are_not_refetched_from_espn(tmp_path):
    league = FakeLeague()
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        settle_week(ledger, league, 1)
        calls_after_first = len(league.calls)
        settle_week(ledger, league, 1)

        assert len(league.calls) == calls_after_first


def test_saved_payouts_read_back_with_the_high_score_first(tmp_path):
    league = FakeLeague()
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        computed = settle_week(ledger, league, 1)

        assert [payout.category for payout in ledger.week_payouts(1)] == [p.category for p in computed]


def test_week_output_names_the_contest_and_lists_payouts(tmp_path):
    league = FakeLeague()
    with ContestLedger(tmp_path / "contest.db", 2026) as ledger:
        payouts = settle_week(ledger, league, 1)

        rendered = format_week(1, payouts, contest_for_week(1))

        assert "Opening Week Bang" in rendered
        assert "$20.00" not in rendered
        assert "Alpha" in rendered
        assert "```" not in rendered


def test_season_output_uses_a_numbered_list_without_redundant_paid_total():
    rendered = format_season([("Alpha", 4000, 2), ("Beta", 1000, 1)])

    assert "1. Alpha — $40.00 (2 pots)" in rendered
    assert "2. Beta — $10.00 (1 pot)" in rendered
    assert "paid out so far" not in rendered
    assert "```" not in rendered


def test_empty_season_output_is_friendly():
    assert "No payouts recorded yet." in format_season([])


def test_display_week_uses_current_week_thursday_through_monday(tmp_path):
    league = FakeLeague()
    league.current_week = 4
    service = ContestService(league, tmp_path / "contest.db", 2026, timezone=ZoneInfo("UTC"))

    assert service.display_week(datetime(2026, 9, 17, 12, tzinfo=UTC)) == 4
    assert service.display_week(datetime(2026, 9, 21, 12, tzinfo=UTC)) == 4


def test_display_week_uses_previous_week_tuesday_and_wednesday(tmp_path):
    league = FakeLeague()
    league.current_week = 4
    service = ContestService(league, tmp_path / "contest.db", 2026, timezone=ZoneInfo("UTC"))

    assert service.display_week(datetime(2026, 9, 15, 12, tzinfo=UTC)) == 3
    assert service.display_week(datetime(2026, 9, 16, 12, tzinfo=UTC)) == 3


def test_report_shows_live_pending_results_during_current_week(tmp_path):
    league = FakeLeague()
    service = ContestService(league, tmp_path / "contest.db", 2026, timezone=ZoneInfo("UTC"))

    rendered = service.report(datetime(2026, 9, 17, 12, tzinfo=UTC))

    assert "**Week 1 results — Pending**" in rendered
    assert "**Opening Week Bang**" in rendered
    assert "- **High score:** Alpha (30.00 points)" in rendered
    assert "$20.00" not in rendered
    assert league.refreshes == 1
    with service.ledger() as ledger:
        assert not ledger.is_settled(1)


def test_report_automatically_records_final_results_and_shows_next_challenge(tmp_path):
    league = FakeLeague()
    league.current_week = 2
    service = ContestService(league, tmp_path / "contest.db", 2026, timezone=ZoneInfo("UTC"))

    rendered = service.report(datetime(2026, 9, 15, 12, tzinfo=UTC))

    assert "**Week 1 results — Final**" in rendered
    assert "**Coming in Week 2**" in rendered
    assert "Ronnie's Failed Rule: AFC Edition" in rendered
    with service.ledger() as ledger:
        assert ledger.is_settled(1)
        assert sum(payout.amount_cents for payout in ledger.week_payouts(1)) == 4000

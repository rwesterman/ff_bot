"""Weekly payout tracking for the league's high-score and side-contest pots.

Money is recorded, not recomputed. ESPN issues stat corrections for days after a game, so a settled
week is written to the ledger once and every later read comes from those rows. Season totals are a
sum over the ledger and never shift underneath a payout that has already been made.

Every contest is answered from one cached snapshot per player-week, taken from `league.box_scores`,
which returns the roster as it stood that week. Ownership over time therefore falls out of the cache:
summing a player's weeks grouped by fantasy team credits each manager only for the weeks they held him.
"""

import csv
import io
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from utils.contest_schedule import (
    AFC_TEAMS,
    HIGH_SCORE_POT_CENTS,
    NFC_TEAMS,
    Contest,
    contest_for_week,
)


logger = logging.getLogger(__name__)

BENCH_SLOTS = frozenset({"BE", "IR"})
SKILL_POSITIONS = frozenset({"RB", "WR", "TE"})
FIRST_DOWN_STAT_IDS = ("211", "212", "213")
THANKSGIVING_WEEKDAYS = frozenset({2, 3, 4})
PENALTY_POINTS = 10

HIGH_SCORE = "high_score"
SIDE_CONTEST = "side_contest"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contest_player_weeks (
    league_year INTEGER NOT NULL,
    week INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    player_id INTEGER NOT NULL,
    player_name TEXT NOT NULL,
    position TEXT NOT NULL,
    slot_position TEXT NOT NULL,
    pro_team TEXT NOT NULL,
    points REAL NOT NULL,
    stats_json TEXT NOT NULL,
    scoring_json TEXT NOT NULL,
    game_date TEXT,
    PRIMARY KEY (league_year, week, team_id, player_id)
);

CREATE TABLE IF NOT EXISTS contest_weeks (
    league_year INTEGER NOT NULL,
    week INTEGER NOT NULL,
    status TEXT NOT NULL,
    settled_at TEXT,
    settled_by INTEGER,
    PRIMARY KEY (league_year, week)
);

CREATE TABLE IF NOT EXISTS contest_payouts (
    league_year INTEGER NOT NULL,
    week INTEGER NOT NULL,
    category TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    detail TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (league_year, week, category, team_id)
);

CREATE TABLE IF NOT EXISTS contest_penalties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league_year INTEGER NOT NULL,
    week INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    team_name TEXT NOT NULL,
    player_name TEXT NOT NULL,
    penalty_count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def format_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def split_pot(pot_cents: int, share_count: int) -> list[int]:
    """Divide a pot into whole cents that add back to exactly the pot."""
    if share_count <= 0:
        raise ValueError("share_count must be positive")
    base, remainder = divmod(pot_cents, share_count)
    return [base + (1 if index < remainder else 0) for index in range(share_count)]


@dataclass(frozen=True, slots=True)
class PlayerWeek:
    week: int
    team_id: int
    team_name: str
    player_id: int
    player_name: str
    position: str
    slot_position: str
    pro_team: str
    points: float
    stats: dict
    scoring: dict
    game_date: str | None

    @property
    def started(self) -> bool:
        return self.slot_position not in BENCH_SLOTS

    def stat(self, name: str) -> float:
        return float(self.stats.get(name) or 0)

    def scored(self, stat_id: str) -> float:
        return float(self.scoring.get(stat_id) or 0)


@dataclass(frozen=True, slots=True)
class Winner:
    team_id: int
    team_name: str
    detail: str


@dataclass(frozen=True, slots=True)
class Payout:
    week: int
    category: str
    team_id: int
    team_name: str
    amount_cents: int
    detail: str


@dataclass
class ResolutionContext:
    contest: Contest
    weeks: dict[int, list[PlayerWeek]]
    keepers: dict[int, int] = field(default_factory=dict)
    penalties: dict[int, tuple[str, int]] = field(default_factory=dict)

    @property
    def source_week(self) -> int:
        return self.contest.source_week()

    def rows(self, week: int | None = None) -> list[PlayerWeek]:
        return self.weeks.get(self.source_week if week is None else week, [])

    def starters(self, week: int | None = None) -> list[PlayerWeek]:
        return [row for row in self.rows(week) if row.started]

    def all_weeks(self) -> list[PlayerWeek]:
        return [row for week in sorted(self.weeks) for row in self.weeks[week]]


def _team_names(rows: list[PlayerWeek]) -> dict[int, str]:
    return {row.team_id: row.team_name for row in rows}


def _winners_by_value(values: dict[int, float], names: dict[int, str], describe) -> list[Winner]:
    """Every team holding the maximum value wins, so ties split the pot."""
    if not values:
        return []
    best = max(values.values())
    return [
        Winner(team_id=team_id, team_name=names.get(team_id, str(team_id)), detail=describe(value))
        for team_id, value in sorted(values.items())
        if value == best
    ]


def _winners_by_row(candidates: list[tuple[float, PlayerWeek]], describe, largest: bool = True) -> list[Winner]:
    """Pick the best player-row(s). Every tied row wins, including two on the same team."""
    if not candidates:
        return []
    best = max(candidates, key=lambda item: item[0])[0] if largest else min(candidates, key=lambda item: item[0])[0]
    return [
        Winner(team_id=row.team_id, team_name=row.team_name, detail=describe(value, row))
        for value, row in sorted(candidates, key=lambda item: (item[1].team_id, item[1].player_name))
        if value == best
    ]


def _cumulative_by_player(rows, started_only: bool = True) -> tuple[dict[tuple[int, int], float], dict]:
    totals: dict[tuple[int, int], float] = {}
    labels: dict[tuple[int, int], PlayerWeek] = {}
    for row in rows:
        if started_only and not row.started:
            continue
        key = (row.team_id, row.player_id)
        totals[key] = totals.get(key, 0.0) + row.points
        labels[key] = row
    return totals, labels


def _best_cumulative(totals, labels, describe) -> list[Winner]:
    if not totals:
        return []
    best = max(totals.values())
    return [
        Winner(team_id=labels[key].team_id, team_name=labels[key].team_name, detail=describe(value, labels[key]))
        for key, value in sorted(totals.items())
        if value == best
    ]


def highest_scoring_starter(context: ResolutionContext) -> list[Winner]:
    candidates = [(row.points, row) for row in context.starters()]
    return _winners_by_row(candidates, lambda value, row: f"{row.player_name} scored {value:.2f}")


def _conference_points(context: ResolutionContext, conference: frozenset, label: str) -> list[Winner]:
    rows = context.starters()
    totals: dict[int, float] = {}
    for row in rows:
        if row.pro_team in conference:
            totals[row.team_id] = totals.get(row.team_id, 0.0) + row.points
    return _winners_by_value(totals, _team_names(rows), lambda value: f"{value:.2f} points from {label} starters")


def most_afc_points(context: ResolutionContext) -> list[Winner]:
    return _conference_points(context, AFC_TEAMS, "AFC")


def most_nfc_points(context: ResolutionContext) -> list[Winner]:
    return _conference_points(context, NFC_TEAMS, "NFC")


def best_cumulative_skill_player(context: ResolutionContext) -> list[Winner]:
    rows = [row for row in context.all_weeks() if row.position in SKILL_POSITIONS]
    totals, labels = _cumulative_by_player(rows)
    return _best_cumulative(totals, labels, lambda value, row: f"{row.player_name} scored {value:.2f} while started")


def most_first_down_points(context: ResolutionContext) -> list[Winner]:
    rows = context.starters()
    totals: dict[int, float] = {}
    for row in rows:
        bonus = sum(row.scored(stat_id) for stat_id in FIRST_DOWN_STAT_IDS)
        totals[row.team_id] = totals.get(row.team_id, 0.0) + bonus
    return _winners_by_value(totals, _team_names(rows), lambda value: f"{value:.2f} first-down bonus points")


def _slot_points(context: ResolutionContext, slot: str, label: str) -> list[Winner]:
    rows = context.starters()
    totals: dict[int, float] = {}
    for row in rows:
        if row.slot_position == slot:
            totals[row.team_id] = totals.get(row.team_id, 0.0) + row.points
    return _winners_by_value(totals, _team_names(rows), lambda value: f"{value:.2f} {label} points")


def highest_kicker_points(context: ResolutionContext) -> list[Winner]:
    return _slot_points(context, "K", "kicker")


def highest_defense_points(context: ResolutionContext) -> list[Winner]:
    return _slot_points(context, "D/ST", "D/ST")


def best_keeper(context: ResolutionContext) -> list[Winner]:
    """Teams without a keeper designated in ESPN are not eligible."""
    if not context.keepers:
        return []
    rows = [row for row in context.all_weeks() if context.keepers.get(row.team_id) == row.player_id]
    totals, labels = _cumulative_by_player(rows)
    return _best_cumulative(totals, labels, lambda value, row: f"{row.player_name} scored {value:.2f} while started")


def tight_end_closest_to_69(context: ResolutionContext) -> list[Winner]:
    """Tight ends start in the flex as well as the TE slot, so filter on position."""
    candidates = [(abs(row.stat("receivingYards") - 69), row) for row in context.starters() if row.position == "TE"]
    return _winners_by_row(
        candidates,
        lambda value, row: f"{row.player_name} at {row.stat('receivingYards'):.0f} yards ({value:.0f} away)",
        largest=False,
    )


def most_rushing_yards(context: ResolutionContext) -> list[Winner]:
    candidates = [(row.stat("rushingYards"), row) for row in context.starters()]
    return _winners_by_row(candidates, lambda value, row: f"{row.player_name} rushed for {value:.0f} yards")


def most_receptions(context: ResolutionContext) -> list[Winner]:
    """Receptions decide it; receiving yards break the tie before the pot is split."""
    rows = context.starters()
    if not rows:
        return []
    ranked = [((row.stat("receivingReceptions"), row.stat("receivingYards")), row) for row in rows]
    best = max(value for value, _ in ranked)
    return [
        Winner(
            team_id=row.team_id,
            team_name=row.team_name,
            detail=f"{row.player_name} with {value[0]:.0f} catches for {value[1]:.0f} yards",
        )
        for value, row in sorted(ranked, key=lambda item: (item[1].team_id, item[1].player_name))
        if value == best
    ]


def best_thanksgiving_player(context: ResolutionContext) -> list[Winner]:
    """Starters and bench both count, limited to games kicking off Wednesday through Friday."""
    candidates = []
    for row in context.rows():
        if not row.game_date:
            continue
        if datetime.fromisoformat(row.game_date).weekday() in THANKSGIVING_WEEKDAYS:
            candidates.append((row.points, row))
    return _winners_by_row(candidates, lambda value, row: f"{row.player_name} scored {value:.2f}")


def manual_penalties(context: ResolutionContext) -> list[Winner]:
    """Penalties are not fantasy stats, so this settles from counts logged during the season."""
    if not context.penalties:
        return []
    totals = {team_id: count * PENALTY_POINTS for team_id, (_, count) in context.penalties.items()}
    names = {team_id: name for team_id, (name, _) in context.penalties.items()}
    return _winners_by_value(totals, names, lambda value: f"{value:.0f} penalty points")


def best_waiver_pickup(context: ResolutionContext) -> list[Winner]:
    """Anyone absent from every week-1 roster was picked up later; stints are summed."""
    weeks = sorted(context.weeks)
    if not weeks:
        return []
    drafted = {row.player_id for row in context.weeks.get(weeks[0], [])}
    rows = [row for row in context.all_weeks() if row.player_id not in drafted]
    totals, labels = _cumulative_by_player(rows)
    return _best_cumulative(totals, labels, lambda value, row: f"{row.player_name} scored {value:.2f} while started")


RESOLVERS = {
    "highest_scoring_starter": highest_scoring_starter,
    "most_afc_points": most_afc_points,
    "most_nfc_points": most_nfc_points,
    "best_cumulative_skill_player": best_cumulative_skill_player,
    "most_first_down_points": most_first_down_points,
    "highest_kicker_points": highest_kicker_points,
    "highest_defense_points": highest_defense_points,
    "best_keeper": best_keeper,
    "tight_end_closest_to_69": tight_end_closest_to_69,
    "most_rushing_yards": most_rushing_yards,
    "most_receptions": most_receptions,
    "best_thanksgiving_player": best_thanksgiving_player,
    "manual_penalties": manual_penalties,
    "best_waiver_pickup": best_waiver_pickup,
}


def high_scoring_teams(rows: list[PlayerWeek]) -> list[Winner]:
    """The standing weekly pot: most points from a starting lineup."""
    totals: dict[int, float] = {}
    for row in rows:
        if row.started:
            totals[row.team_id] = totals.get(row.team_id, 0.0) + row.points
    return _winners_by_value(totals, _team_names(rows), lambda value: f"{value:.2f} points")


def snapshot_week(league, week: int) -> list[PlayerWeek]:
    """Flatten one ESPN week into player rows. Includes bench and IR."""
    rows = []
    for box_score in league.box_scores(week=week):
        sides = ((box_score.home_team, box_score.home_lineup), (box_score.away_team, box_score.away_lineup))
        for team, lineup in sides:
            if team is None:
                continue
            for player in lineup or []:
                game_date = getattr(player, "game_date", None)
                rows.append(
                    PlayerWeek(
                        week=week,
                        team_id=team.team_id,
                        team_name=team.team_name,
                        player_id=player.playerId,
                        player_name=player.name,
                        position=player.position or "",
                        slot_position=player.slot_position or "",
                        pro_team=player.proTeam or "",
                        points=float(player.points or 0),
                        stats=dict(getattr(player, "breakdown", {}) or {}),
                        scoring={str(k): v for k, v in (getattr(player, "points_breakdown", {}) or {}).items()},
                        game_date=game_date.isoformat() if game_date else None,
                    )
                )
    return rows


def keepers_from_draft(league) -> dict[int, int]:
    return {
        pick.team.team_id: pick.playerId
        for pick in (getattr(league, "draft", None) or [])
        if pick.keeper_status and getattr(pick, "team", None) is not None
    }


class ContestLedger:
    def __init__(self, path: str | Path, league_year: int):
        self.path = Path(path)
        self.league_year = league_year
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.close()

    def store_week(self, rows: list[PlayerWeek]):
        # Replace the week outright; an upsert alone would strand players dropped since the last snapshot.
        for week in {row.week for row in rows}:
            self.connection.execute(
                "DELETE FROM contest_player_weeks WHERE league_year = ? AND week = ?", (self.league_year, week)
            )
        self.connection.executemany(
            """
            INSERT INTO contest_player_weeks (
                league_year, week, team_id, team_name, player_id, player_name, position,
                slot_position, pro_team, points, stats_json, scoring_json, game_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(league_year, week, team_id, player_id) DO UPDATE SET
                team_name = excluded.team_name,
                player_name = excluded.player_name,
                position = excluded.position,
                slot_position = excluded.slot_position,
                pro_team = excluded.pro_team,
                points = excluded.points,
                stats_json = excluded.stats_json,
                scoring_json = excluded.scoring_json,
                game_date = excluded.game_date
            """,
            [
                (
                    self.league_year,
                    row.week,
                    row.team_id,
                    row.team_name,
                    row.player_id,
                    row.player_name,
                    row.position,
                    row.slot_position,
                    row.pro_team,
                    row.points,
                    json.dumps(row.stats),
                    json.dumps(row.scoring),
                    row.game_date,
                )
                for row in rows
            ],
        )
        self.connection.commit()

    def load_week(self, week: int) -> list[PlayerWeek]:
        cursor = self.connection.execute(
            """
            SELECT week, team_id, team_name, player_id, player_name, position, slot_position,
                   pro_team, points, stats_json, scoring_json, game_date
            FROM contest_player_weeks WHERE league_year = ? AND week = ?
            """,
            (self.league_year, week),
        )
        return [
            PlayerWeek(
                week=row[0],
                team_id=row[1],
                team_name=row[2],
                player_id=row[3],
                player_name=row[4],
                position=row[5],
                slot_position=row[6],
                pro_team=row[7],
                points=row[8],
                stats=json.loads(row[9]),
                scoring=json.loads(row[10]),
                game_date=row[11],
            )
            for row in cursor
        ]

    def cached_weeks(self) -> set[int]:
        cursor = self.connection.execute(
            "SELECT DISTINCT week FROM contest_player_weeks WHERE league_year = ?", (self.league_year,)
        )
        return {row[0] for row in cursor}

    def is_settled(self, week: int) -> bool:
        row = self.connection.execute(
            "SELECT status FROM contest_weeks WHERE league_year = ? AND week = ?", (self.league_year, week)
        ).fetchone()
        return bool(row) and row[0] == "settled"

    def record_penalty(self, week: int, team_id: int, team_name: str, player_name: str, count: int):
        self.connection.execute(
            """
            INSERT INTO contest_penalties (
                league_year, week, team_id, team_name, player_name, penalty_count, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (self.league_year, week, team_id, team_name, player_name, count, utc_now()),
        )
        self.connection.commit()

    def penalty_totals(self) -> dict[int, tuple[str, int]]:
        cursor = self.connection.execute(
            """
            SELECT team_id, MAX(team_name), SUM(penalty_count)
            FROM contest_penalties WHERE league_year = ? GROUP BY team_id
            """,
            (self.league_year,),
        )
        return {row[0]: (row[1], row[2]) for row in cursor}

    def save_payouts(self, week: int, payouts: list[Payout], settled_by: int | None = None):
        self.connection.execute(
            """
            INSERT INTO contest_weeks (league_year, week, status, settled_at, settled_by)
            VALUES (?, ?, 'settled', ?, ?)
            ON CONFLICT(league_year, week) DO UPDATE SET
                status = 'settled', settled_at = excluded.settled_at, settled_by = excluded.settled_by
            """,
            (self.league_year, week, utc_now(), settled_by),
        )
        self.connection.execute(
            "DELETE FROM contest_payouts WHERE league_year = ? AND week = ?", (self.league_year, week)
        )
        self.connection.executemany(
            """
            INSERT INTO contest_payouts (
                league_year, week, category, team_id, team_name, amount_cents, detail, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.league_year,
                    week,
                    payout.category,
                    payout.team_id,
                    payout.team_name,
                    payout.amount_cents,
                    payout.detail,
                    utc_now(),
                )
                for payout in payouts
            ],
        )
        self.connection.commit()

    def week_payouts(self, week: int) -> list[Payout]:
        cursor = self.connection.execute(
            """
            SELECT week, category, team_id, team_name, amount_cents, detail
            FROM contest_payouts WHERE league_year = ? AND week = ?
            ORDER BY CASE category WHEN 'high_score' THEN 0 ELSE 1 END, team_name
            """,
            (self.league_year, week),
        )
        return [Payout(*row) for row in cursor]

    def settled_weeks(self) -> list[int]:
        cursor = self.connection.execute(
            "SELECT week FROM contest_weeks WHERE league_year = ? AND status = 'settled' ORDER BY week",
            (self.league_year,),
        )
        return [row[0] for row in cursor]

    def season_totals(self) -> list[tuple[str, int, int]]:
        cursor = self.connection.execute(
            """
            SELECT team_name, SUM(amount_cents) AS total, COUNT(*) AS wins
            FROM contest_payouts WHERE league_year = ?
            GROUP BY team_id ORDER BY total DESC, team_name
            """,
            (self.league_year,),
        )
        return [(row[0], row[1], row[2]) for row in cursor]

    def export_rows(self) -> list[tuple]:
        cursor = self.connection.execute(
            """
            SELECT week, category, team_name, amount_cents, detail
            FROM contest_payouts WHERE league_year = ?
            ORDER BY week, CASE category WHEN 'high_score' THEN 0 ELSE 1 END, team_name
            """,
            (self.league_year,),
        )
        return list(cursor)


def sync_weeks(ledger: ContestLedger, league, weeks, force: bool = False) -> int:
    """Refresh every week that is not yet settled.

    Settling freezes a week, which is what keeps a recorded payout stable against ESPN's later stat
    corrections. Unsettled weeks are refetched so a lineup change or a Monday-night game is picked up.
    """
    fetched = 0
    for week in weeks:
        if not force and ledger.is_settled(week):
            continue
        ledger.store_week(snapshot_week(league, week))
        fetched += 1
    return fetched


def build_payouts(contest: Contest | None, high_scorers: list[Winner], side_winners: list[Winner], week: int):
    payouts = []
    for winner, amount in zip(high_scorers, split_pot(HIGH_SCORE_POT_CENTS, len(high_scorers) or 1)):
        payouts.append(Payout(week, HIGH_SCORE, winner.team_id, winner.team_name, amount, winner.detail))
    if contest and side_winners:
        for winner, amount in zip(side_winners, split_pot(contest.pot_cents, len(side_winners))):
            payouts.append(Payout(week, SIDE_CONTEST, winner.team_id, winner.team_name, amount, winner.detail))
    return payouts


def calculate_week(ledger: ContestLedger, league, week: int) -> list[Payout]:
    contest = contest_for_week(week)
    required = set(contest.weeks_required()) if contest else set()
    required.add(week)
    sync_weeks(ledger, league, sorted(required))

    weeks = {source: ledger.load_week(source) for source in sorted(required)}
    high_scorers = high_scoring_teams(weeks.get(week, []))

    side_winners: list[Winner] = []
    if contest:
        context = ResolutionContext(
            contest=contest,
            weeks=weeks,
            keepers=keepers_from_draft(league),
            penalties=ledger.penalty_totals(),
        )
        resolver = RESOLVERS.get(contest.resolver)
        if resolver is None:
            logger.error("No resolver named %s for week %s", contest.resolver, week)
        else:
            side_winners = resolver(context)

    return build_payouts(contest, high_scorers, side_winners, week)


def settle_week(ledger: ContestLedger, league, week: int, settled_by: int | None = None) -> list[Payout]:
    payouts = calculate_week(ledger, league, week)
    ledger.save_payouts(week, payouts, settled_by=settled_by)
    return payouts


def resolve_team(league, query: str):
    """Match a team by name so the ledger never asks anyone for a numeric team id."""
    needle = query.strip().casefold()
    teams = getattr(league, "teams", []) or []
    exact = [team for team in teams if team.team_name.casefold() == needle]
    if exact:
        return exact[0]
    partial = [team for team in teams if needle in team.team_name.casefold()]
    if len(partial) == 1:
        return partial[0]
    return None


class ContestService:
    """Opens a short-lived ledger per operation, so calls stay safe from worker threads."""

    def __init__(
        self,
        league,
        database_path: str | Path,
        league_year: int,
        admin_ids=frozenset(),
        timezone: ZoneInfo | None = None,
    ):
        self.league = league
        self.database_path = Path(database_path)
        self.league_year = league_year
        self.admin_ids = frozenset(admin_ids)
        self.timezone = timezone or ZoneInfo("America/Chicago")

    @classmethod
    def from_environment(cls, league, database_path: str | Path):
        raw_admins = os.getenv("CONTEST_ADMIN_IDS", "")
        admin_ids = {int(part) for part in raw_admins.replace(",", " ").split() if part.strip().isdigit()}
        league_year = int(os.getenv("LEAGUE_YEAR", str(datetime.now(UTC).year)))
        timezone = ZoneInfo(os.getenv("CONTEST_TIMEZONE", "America/Chicago"))
        return cls(league, database_path, league_year, admin_ids, timezone)

    def is_admin(self, user_id: int) -> bool:
        return not self.admin_ids or user_id in self.admin_ids

    def ledger(self) -> ContestLedger:
        return ContestLedger(self.database_path, self.league_year)

    def current_week(self) -> int:
        return max(1, int(getattr(self.league, "current_week", 1) or 1))

    def display_week(self, now: datetime | None = None) -> int:
        local_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
        current = self.current_week()
        return max(1, current - 1) if local_now.weekday() in {1, 2} else current

    def report(self, now: datetime | None = None) -> str:
        refresh = getattr(self.league, "refresh", None)
        if callable(refresh):
            refresh()

        current = self.current_week()
        target = self.display_week(now)
        completed_through = current - 1
        with self.ledger() as ledger:
            for week in range(1, completed_through + 1):
                if not ledger.is_settled(week):
                    settle_week(ledger, self.league, week)

            is_final = target <= completed_through
            if is_final:
                payouts = ledger.week_payouts(target)
            else:
                payouts = calculate_week(ledger, self.league, target)

            sections = [
                format_week(target, payouts, contest_for_week(target), status="Final" if is_final else "Pending"),
                format_season(ledger.season_totals()),
            ]
            local_now = now.astimezone(self.timezone) if now else datetime.now(self.timezone)
            if local_now.weekday() in {1, 2}:
                sections.insert(1, format_upcoming(target + 1, contest_for_week(target + 1)))
            return "\n\n".join(sections)

    def season(self) -> str:
        with self.ledger() as ledger:
            settled = ledger.settled_weeks()
            header = f"_Weeks settled: {', '.join(str(week) for week in settled)}._" if settled else ""
            return f"{format_season(ledger.season_totals())}\n{header}".strip()

    def log_penalty(self, week: int, team_query: str, player_name: str, count: int) -> str:
        team = resolve_team(self.league, team_query)
        if team is None:
            names = "\n".join(f"- {each.team_name}" for each in getattr(self.league, "teams", []) or [])
            return f"I could not match a team named `{team_query}`. Try one of:\n{names}"
        with self.ledger() as ledger:
            ledger.record_penalty(week, team.team_id, team.team_name, player_name, count)
            running = ledger.penalty_totals().get(team.team_id, (team.team_name, 0))[1]
        plural = "penalty" if count == 1 else "penalties"
        return f"Logged {count} {plural} for {player_name} on {team.team_name}. Season total: {running}."

    def export_csv(self) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["week", "category", "team", "amount", "detail"])
        with self.ledger() as ledger:
            for week, category, team_name, amount_cents, detail in ledger.export_rows():
                writer.writerow([week, category, team_name, f"{amount_cents / 100:.2f}", detail])
        return buffer.getvalue()


def format_week(week: int, payouts: list[Payout], contest: Contest | None, status: str | None = None) -> str:
    status_text = f" — {status}" if status else ""
    lines = [f"**Week {week} results{status_text}**"]
    if contest:
        lines.extend([f"**{contest.name}**", f"_{contest.rule}_"])

    if not payouts:
        lines.append("No results are available yet.")
        return "\n".join(lines)

    for payout in payouts:
        category = "High score" if payout.category == HIGH_SCORE else contest.name if contest else "Contest"
        lines.append(f"- **{category}:** {payout.team_name} ({payout.detail})")

    if contest and not any(payout.category == SIDE_CONTEST for payout in payouts):
        lines.append(f"- **{contest.name}:** No result is available yet.")
    return "\n".join(lines)


def format_upcoming(week: int, contest: Contest | None) -> str:
    lines = [f"**Coming in Week {week}**"]
    if contest:
        lines.extend([f"**{contest.name}**", contest.rule])
    else:
        lines.append("No side contest is scheduled.")
    return "\n".join(lines)


def format_season(totals: list[tuple[str, int, int]]) -> str:
    if not totals:
        return "**Season totals**\nNo payouts recorded yet."
    lines = ["**Season totals**"]
    for index, (name, cents, wins) in enumerate(totals, start=1):
        pot_label = "pot" if wins == 1 else "pots"
        lines.append(f"{index}. {name} — {format_money(cents)} ({wins} {pot_label})")
    return "\n".join(lines)

"""Public ESPN penalty ingestion and persistent league bonus ledger.

No Discord or fantasy-league requests happen at import time.
"""

from contextlib import closing
import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import sqlite3
from threading import RLock

import requests


logger = logging.getLogger(__name__)
QUALIFYING = {"unsportsmanlike conduct", "taunting"}
OFFENSIVE_POSITIONS = {"QB", "RB", "WR", "TE", "K", "FB"}
ACTIVE_SLOTS = {"QB", "RB", "WR", "TE", "K", "RB/WR", "WR/TE", "RB/WR/TE", "OP", "FLEX"}
CORE_URL = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
PENALTY_CLAUSE = re.compile(r"penalty on\s+([A-Z]+)-(?:\d+-)?([^,]+),\s*([^,\.]+)", re.I)


def penalty_poll_minutes():
    """Read the scheduled polling interval at startup, in whole minutes."""
    value = os.getenv("PENALTY_POLL_MINUTES", "10")
    try:
        minutes = int(value)
    except ValueError:
        raise ValueError("PENALTY_POLL_MINUTES must be a positive integer") from None
    if minutes <= 0:
        raise ValueError("PENALTY_POLL_MINUTES must be a positive integer")
    return minutes


@dataclass(frozen=True)
class Penalty:
    game_id: str
    play_id: str
    occurrence: int
    player_id: int
    name: str
    description: str
    week: int


class EspnPenaltySource:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.athlete_names = {}

    def _get(self, url, **params):
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _short_name(self, season, player_id):
        key = (season, player_id)
        if key not in self.athlete_names:
            athlete = self._get(f"{CORE_URL}/seasons/{season}/athletes/{player_id}")
            self.athlete_names[key] = athlete["shortName"]
        return self.athlete_names[key]

    def parse_play(self, season, week, game_id, play):
        description = play.get("text", "")
        clauses = list(PENALTY_CLAUSE.finditer(description))
        if not any(c[3].strip().lower() in QUALIFYING for c in clauses):
            if any(name in description.lower() for name in QUALIFYING):
                logger.warning("Unrecognized penalty description: game=%s play=%s", game_id, play["id"])
            return []
        participants = []
        for participant in play.get("participants", []):
            if participant.get("type") == "penalized":
                match = re.search(r"/athletes/(\d+)", participant["athlete"]["$ref"])
                if match:
                    participants.append(int(match[1]))
        participants = sorted(set(participants))
        result = []
        occurrences = {}
        for clause in clauses:
            name = clause[3].strip()
            if name.lower() not in QUALIFYING:
                continue
            # A sole penalized participant is authoritative only for a sole penalty.
            candidates = participants
            if len(clauses) != 1 or len(participants) != 1:

                def normalize(value):
                    return re.sub(r"[^a-z]", "", value.lower())

                candidates = [
                    pid for pid in participants if normalize(self._short_name(season, pid)) == normalize(clause[2])
                ]
            if len(candidates) != 1:
                logger.warning("Unresolved penalty player: game=%s play=%s", game_id, play["id"])
                continue
            player_id = candidates[0]
            index = occurrences.get(player_id, 0)
            occurrences[player_id] = index + 1
            result.append(Penalty(str(game_id), str(play["id"]), index, player_id, name, description, week))
        return result

    def fetch_games(self, season, week):
        scoreboard = self._get(SCOREBOARD_URL, dates=season, seasontype=2, week=week, limit=100)
        if (
            int(scoreboard["season"]["year"]) != season
            or int(scoreboard["season"]["type"]) != 2
            or int(scoreboard["week"]["number"]) != week
        ):
            raise ValueError("ESPN returned a different season/week than requested")
        return scoreboard["events"]

    def fetch_game(self, season, week, game_id):
        penalties = []
        page = 1
        while True:
            data = self._get(f"{CORE_URL}/events/{game_id}/competitions/{game_id}/plays", limit=400, page=page)
            for play in data["items"]:
                penalties.extend(self.parse_play(season, week, game_id, play))
            if page >= int(data["pageCount"]):
                break
            page += 1
        return penalties

    def fetch_week(self, season, week):
        penalties = []
        for game in self.fetch_games(season, week):
            if game["status"]["type"]["state"] == "pre":
                continue
            penalties.extend(self.fetch_game(season, week, game["id"]))
        return penalties


class PenaltyStore:
    def __init__(self, path, league_id, season):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.league_id = int(league_id)
        self.season = int(season)
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS penalty_bonuses (
                    id INTEGER PRIMARY KEY,
                    league_id INTEGER NOT NULL, season INTEGER NOT NULL,
                    game_id TEXT NOT NULL, play_id TEXT NOT NULL, occurrence INTEGER NOT NULL,
                    team_id INTEGER NOT NULL, team_name TEXT NOT NULL,
                    player_id INTEGER NOT NULL, player_name TEXT NOT NULL,
                    penalty_name TEXT NOT NULL, description TEXT NOT NULL,
                    week INTEGER NOT NULL, points INTEGER NOT NULL DEFAULT 10,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    notified_at TEXT, discord_message_id TEXT,
                    UNIQUE(league_id, season, game_id, play_id, player_id, occurrence)
                )
            """)

            columns = {row["name"] for row in db.execute("PRAGMA table_info(penalty_bonuses)")}
            if "notification_suppressed" not in columns:
                db.execute("ALTER TABLE penalty_bonuses ADD COLUMN notification_suppressed INTEGER NOT NULL DEFAULT 0")
            db.execute("""
                CREATE TABLE IF NOT EXISTS penalty_game_checks (
                    league_id INTEGER NOT NULL, season INTEGER NOT NULL, game_id TEXT NOT NULL,
                    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (league_id, season, game_id)
                )
            """)

    def completed_game_ids(self):
        with closing(self.connect()) as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT game_id FROM penalty_game_checks WHERE league_id = ? AND season = ?",
                    (self.league_id, self.season),
                )
            }

    def mark_game_checked(self, game_id):
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO penalty_game_checks (league_id, season, game_id) VALUES (?, ?, ?)",
                (self.league_id, self.season, str(game_id)),
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def award(self, penalty, team, player, *, notify=True):
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                """
                INSERT INTO penalty_bonuses
                    (league_id, season, game_id, play_id, occurrence, team_id, team_name,
                     player_id, player_name, penalty_name, description, week, notification_suppressed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(league_id, season, game_id, play_id, player_id, occurrence) DO NOTHING
            """,
                (
                    self.league_id,
                    self.season,
                    penalty.game_id,
                    penalty.play_id,
                    penalty.occurrence,
                    team.team_id,
                    team.team_name,
                    penalty.player_id,
                    player.name,
                    penalty.name,
                    penalty.description,
                    penalty.week,
                    int(not notify),
                ),
            )
            return cursor.rowcount == 1

    def totals(self, week):
        with closing(self.connect()) as db:
            return dict(
                db.execute(
                    """
                SELECT team_id, SUM(points) FROM penalty_bonuses
                WHERE league_id = ? AND season = ? AND week = ? GROUP BY team_id
            """,
                    (self.league_id, self.season, week),
                ).fetchall()
            )

    def for_week(self, week):
        with closing(self.connect()) as db:
            return db.execute(
                """
                SELECT * FROM penalty_bonuses
                WHERE league_id = ? AND season = ? AND week = ? ORDER BY id
                """,
                (self.league_id, self.season, week),
            ).fetchall()

    def pending(self):
        with closing(self.connect()) as db:
            return db.execute(
                """
                SELECT * FROM penalty_bonuses
                WHERE league_id = ? AND season = ? AND notified_at IS NULL
                    AND notification_suppressed = 0 ORDER BY id
            """,
                (self.league_id, self.season),
            ).fetchall()

    def mark_notified(self, record_id, message_id):
        with closing(self.connect()) as db, db:
            db.execute(
                """
                UPDATE penalty_bonuses SET notified_at = CURRENT_TIMESTAMP, discord_message_id = ?
                WHERE id = ? AND league_id = ? AND season = ?
            """,
                (str(message_id), record_id, self.league_id, self.season),
            )


def record_starter_penalties(store, penalties, box_scores, *, notify=True):
    starters = {}
    for box in box_scores:
        for side in ("home", "away"):
            team = getattr(box, f"{side}_team")
            if not team:
                continue
            for player in getattr(box, f"{side}_lineup"):
                if player.position in OFFENSIVE_POSITIONS and player.slot_position in ACTIVE_SLOTS:
                    starters.setdefault(int(player.playerId), []).append((team, player))
    count = 0
    for penalty in penalties:
        owners = starters.get(penalty.player_id, [])
        if len(owners) == 1:
            count += store.award(penalty, *owners[0], notify=notify)
        elif owners:
            logger.warning("Player %s has multiple active fantasy owners; skipping", penalty.player_id)
    return count


def announcement(record, current_week):
    when = "this week" if record["week"] == current_week else f"in Week {record['week']}"
    # Bound names so even unusual user-controlled team names fit a Discord message.
    return (
        f"{record['player_name'][:200]} has been flagged for Unsportsmanlike Conduct! "
        f"{record['team_name'][:200]} will be awarded ten additional points {when}."
    )


async def deliver_pending(store, current_week, send):
    """Persist delivery receipts only after the supplied async transport succeeds."""
    for record in await asyncio.to_thread(store.pending):
        message_id = await send(announcement(record, current_week))
        await asyncio.to_thread(store.mark_notified, record["id"], message_id)


class PenaltyMonitor:
    def __init__(self, commander, store, source=None):
        self.commander = commander
        self.store = store
        self.source = source or EspnPenaltySource()
        self.caught_up = False
        self.poll_lock = RLock()
        self.outstanding_weeks = set()

    def _record_week(self, week, *, notify):
        penalties = self.source.fetch_week(self.store.season, week)
        if penalties:
            with self.commander.league_lock:
                boxes = self.commander.league.box_scores(week=week)
                record_starter_penalties(self.store, penalties, boxes, notify=notify)

    def poll_week(self, week, *, notify=False):
        if not 1 <= week <= 18:
            raise ValueError("Week must be between 1 and 18.")
        with self.poll_lock:
            with self.commander.league_lock:
                self.commander.league.refresh()
                if week > self.commander.league.current_week:
                    raise ValueError("Cannot refresh penalties for a future week.")
            self._record_week(week, notify=notify)

    def poll(self):
        with self.poll_lock:
            # Share the commander's lock because espn-api refresh mutates its League.
            with self.commander.league_lock:
                league = self.commander.league
                league.refresh()
                current = min(league.current_week, 18)
            weeks = set(range(1 if not self.caught_up else max(1, current - 1), current + 1))
            checked = self.store.completed_game_ids()
            for week in sorted(weeks | self.outstanding_weeks):
                games = self.source.fetch_games(self.store.season, week)
                self.outstanding_weeks.add(week)
                for game in games:
                    status = game["status"]["type"]
                    game_id = str(game["id"])
                    completed = status.get("completed", False)
                    if not completed and status["state"] != "in":
                        continue
                    if completed and game_id in checked:
                        continue
                    penalties = self.source.fetch_game(self.store.season, week, game_id)
                    if penalties:
                        with self.commander.league_lock:
                            boxes = league.box_scores(week=week)
                            record_starter_penalties(self.store, penalties, boxes)
                    # Only checkpoint after the entire feed and any awards were saved successfully.
                    if completed:
                        self.store.mark_game_checked(game_id)
                        checked.add(game_id)
                if games and all(str(game["id"]) in checked for game in games):
                    self.outstanding_weeks.discard(week)
            self.caught_up = True

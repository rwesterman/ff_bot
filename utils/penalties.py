"""Public ESPN penalty ingestion and persistent league bonus ledger.

No Discord or fantasy-league requests happen at import time.
"""

from contextlib import closing
import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import sqlite3

import requests


logger = logging.getLogger(__name__)
QUALIFYING = {"unsportsmanlike conduct", "taunting"}
OFFENSIVE_POSITIONS = {"QB", "RB", "WR", "TE", "K", "FB"}
ACTIVE_SLOTS = {"QB", "RB", "WR", "TE", "K", "RB/WR", "WR/TE", "RB/WR/TE", "OP", "FLEX"}
CORE_URL = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
PENALTY_CLAUSE = re.compile(r"penalty on\s+([A-Z]+)-(?:\d+-)?([^,]+),\s*([^,\.]+)", re.I)


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

    def fetch_week(self, season, week):
        scoreboard = self._get(SCOREBOARD_URL, dates=season, seasontype=2, week=week, limit=100)
        if (
            int(scoreboard["season"]["year"]) != season
            or int(scoreboard["season"]["type"]) != 2
            or int(scoreboard["week"]["number"]) != week
        ):
            raise ValueError("ESPN returned a different season/week than requested")
        penalties = []
        for game in scoreboard["events"]:
            if game["status"]["type"]["state"] == "pre":
                continue
            game_id = game["id"]
            page = 1
            while True:
                data = self._get(f"{CORE_URL}/events/{game_id}/competitions/{game_id}/plays", limit=400, page=page)
                for play in data["items"]:
                    penalties.extend(self.parse_play(season, week, game_id, play))
                if page >= int(data["pageCount"]):
                    break
                page += 1
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

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def award(self, penalty, team, player):
        with closing(self.connect()) as db, db:
            cursor = db.execute(
                """
                INSERT INTO penalty_bonuses
                    (league_id, season, game_id, play_id, occurrence, team_id, team_name,
                     player_id, player_name, penalty_name, description, week)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                WHERE league_id = ? AND season = ? AND notified_at IS NULL ORDER BY id
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


def record_starter_penalties(store, penalties, box_scores):
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
            count += store.award(penalty, *owners[0])
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

    def poll(self):
        # Share the commander's lock because espn-api refresh mutates its League.
        with self.commander.league_lock:
            league = self.commander.league
            league.refresh()
            current = min(league.current_week, 18)
        weeks = range(1 if not self.caught_up else max(1, current - 1), current + 1)
        for week in weeks:
            penalties = self.source.fetch_week(self.store.season, week)
            if penalties:
                with self.commander.league_lock:
                    boxes = league.box_scores(week=week)
                    record_starter_penalties(self.store, penalties, boxes)
        self.caught_up = True

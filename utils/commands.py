# from utils.players import Projections, SleeperPlayers
from cachetools import cached, TTLCache
from collections import namedtuple
from copy import copy
import logging
from threading import RLock
from tabulate import tabulate

logger = logging.getLogger(__name__)


class Commands:
    def __init__(self, league, penalty_store=None):
        self.league = league
        self.penalty_store = penalty_store
        self.league_lock = RLock()

        self.TeamStats = namedtuple("TeamStats", ["wins", "losses", "team_name", "points_for"])
        # A mapping from groupme user ID to team number
        # self.gm_id_to_team = {
        #                     11847036 : 0,
        #                     4334131 : 1,
        #                     11847032 : 2,
        #                     30849273 : 3,
        #                     11847033 : 4,
        #                     2249412 : 5,
        #                     11847034 : 6,
        #                     26527139 : 7,
        #                     577750 : 8,
        #                     399917 : 9}

    def commands_help(self):
        text = "You can use the following commands:\n"
        text += "/matchups - Returns this week's matchups\n"
        text += "/standings - Returns the overall league standings\n"
        text += "/scores - Returns this week's scores\n"
        text += "/penalties <week> - Lists recorded penalty bonuses for that week\n"
        text += "/projections - Returns the projected points for each of your players this week\n"

        return text

    def get_penalties(self, week):
        """Return bounded Discord table pages from the ledger without fetching ESPN."""
        if not 1 <= week <= 18:
            raise ValueError("Week must be between 1 and 18.")
        if self.penalty_store is None:
            return ["The penalty bonus database is not configured."]
        records = self.penalty_store.for_week(week)
        title = f"Week {week} penalty bonuses ({self.penalty_store.season})"
        if not records:
            return [f"{title}: No applicable penalties logged yet."]

        def cell(value, width):
            text = " ".join(str(value).replace("`", "'").split())
            return text if len(text) <= width else text[: width - 1] + "…"

        pages = []
        page_count = (len(records) + 7) // 8
        for offset in range(0, len(records), 8):
            rows = [
                [
                    cell(record["team_name"], 22),
                    cell(record["player_name"], 22),
                    cell(record["penalty_name"], 23),
                    f"+{record['points']}",
                    "Sent" if record["notified_at"] else "Pending",
                ]
                for record in records[offset : offset + 8]
            ]
            table = tabulate(rows, headers=["Fantasy team", "Player", "Penalty", "Bonus", "Notice"], tablefmt="simple")
            heading = f"{title} — page {offset // 8 + 1}/{page_count}"
            summary = f"{len(records)} penalties | +{sum(r['points'] for r in records)} points total"
            pages.append(f"{heading}\n{summary}\n```\n{table}\n```")
        return pages

    # Only allow updating League once every hour
    @cached(cache=TTLCache(maxsize=1, ttl=60 * 60 * 1))
    def refresh_league(self):
        logger.info("Refreshed League object!")
        with self.league_lock:
            self.league.refresh()

    def bonus_totals(self, week):
        return self.penalty_store.totals(week) if self.penalty_store else {}

    def box_scores(self, week=None, adjusted=True):
        """Copy ESPN scores so repeated reads never compound the local bonuses."""
        week = week or self.league.current_week
        with self.league_lock:
            boxes = [copy(box) for box in self.league.box_scores(week=week)]
        if adjusted and self.penalty_store:
            # ESPN totals may span several scoring weeks in a playoff matchup.
            periods = getattr(getattr(self.league, "settings", None), "matchup_periods", {})
            weeks = next((weeks for weeks in periods.values() if week in weeks), [week])
            totals = {}
            for scoring_week in weeks:
                for team_id, points in self.bonus_totals(scoring_week).items():
                    totals[team_id] = totals.get(team_id, 0) + points
            for box in boxes:
                for side in ("home", "away"):
                    team = getattr(box, f"{side}_team")
                    if team:
                        attr = f"{side}_score"
                        setattr(box, attr, getattr(box, attr) + totals.get(team.team_id, 0))
        return boxes

    def get_scoreboard_short(self, week=None):
        # Gets current week's scoreboard
        self.refresh_league()
        box_scores = self.box_scores(week=week)
        score = [
            "%s %.2f - %.2f %s" % (i.home_team.team_abbrev, i.home_score, i.away_score, i.away_team.team_abbrev)
            for i in box_scores
            if i.away_team
        ]
        text = ["Score Update"] + score
        return "\n".join(text)

    def _get_projected_total(self, lineup):
        total_projected = 0
        for i in lineup:
            if i.slot_position != "BE":
                if i.points != 0 or i.game_played > 0:
                    total_projected += i.points
                else:
                    total_projected += i.projected_points
        return total_projected

    def get_standings(self, week=None):
        self.refresh_league()
        teams = self.league.teams

        top_half_totals = {t.team_name: 0 for t in teams}
        if not week:
            week = self.league.current_week
        settings = getattr(self.league, "settings", None)
        end_week = min(week, getattr(settings, "reg_season_count", week - 1) + 1)
        win_delta = {t.team_name: 0 for t in teams}
        loss_delta = {t.team_name: 0 for t in teams}
        ties = {t.team_name: getattr(t, "ties", 0) for t in teams}
        extra_points = {t.team_name: 0 for t in teams}
        for w in range(1, end_week):
            top_half_totals = self.top_half_wins(top_half_totals, w)
            bonuses = self.bonus_totals(w)
            if not bonuses:
                continue
            for box in self.box_scores(w, adjusted=False):
                home, away = box.home_team, box.away_team
                for team in (home, away):
                    if team:
                        extra_points[team.team_name] += bonuses.get(team.team_id, 0)
                if not home or not away:
                    continue
                before = round(box.home_score - box.away_score, 2)
                after = round(before + bonuses.get(home.team_id, 0) - bonuses.get(away.team_id, 0), 2)
                for team, sign in ((home, 1), (away, -1)):
                    name = team.team_name
                    win_delta[name] += int(sign * after > 0) - int(sign * before > 0)
                    loss_delta[name] += int(sign * after < 0) - int(sign * before < 0)
                    ties[name] += int(after == 0) - int(before == 0)

        standings = []
        for t in teams:
            wins = top_half_totals[t.team_name] + t.wins + win_delta[t.team_name]
            standings.append(
                self.TeamStats(
                    wins, t.losses + loss_delta[t.team_name], t.team_name, t.points_for + extra_points[t.team_name]
                )
            )

        # Sort standings by the following criteria:
        # 1) Most wins
        # 2) Least losses
        # 3) Most 'Points For'
        standings = sorted(standings, key=lambda x: (x.wins, -x.losses, x.points_for), reverse=True)

        standings_txt = [
            f"{pos + 1}: {team_name} ({wins} - {losses}"
            f"{f' - {ties[team_name]}' if ties[team_name] else ''}) (+{top_half_totals[team_name]})"
            for pos, (wins, losses, team_name, pf) in enumerate(standings)
        ]
        text = ["Current Standings:"] + standings_txt

        return "\n".join(text)

    def top_half_wins(self, top_half_totals, week):
        # Todo: Consider caching the scores for earlier weeks so this only has to be run once per day
        box_scores = self.box_scores(week=week)

        scores = [(i.home_score, i.home_team.team_name) for i in box_scores] + [
            (i.away_score, i.away_team.team_name) for i in box_scores if i.away_team
        ]

        scores = sorted(scores, key=lambda tup: tup[0], reverse=True)

        for idx in range(0, len(scores) // 2):
            points, team_name = scores[idx]
            top_half_totals[team_name] += 1

        return top_half_totals

    def all_played(self, lineup):
        self.refresh_league()
        for i in lineup:
            if i.slot_position != "BE" and i.game_played < 100:
                return False
        return True

    def get_recent_activity(self, size=20, offset=0):
        """
        Returns a list of Activity objects from espn_api.football.activity
        """
        return self.league.recent_activity(size=size, offset=offset)

    def get_projected_scoreboard(self, week=None):
        # Gets current week's scoreboard projections
        self.refresh_league()
        box_scores = self.box_scores(week=week)
        bonuses = self.bonus_totals(week or self.league.current_week)
        score = [
            "%s %.2f - %.2f %s"
            % (
                i.home_team.team_abbrev,
                self._get_projected_total(i.home_lineup) + bonuses.get(getattr(i.home_team, "team_id", None), 0),
                self._get_projected_total(i.away_lineup) + bonuses.get(getattr(i.away_team, "team_id", None), 0),
                i.away_team.team_abbrev,
            )
            for i in box_scores
            if i.away_team
        ]
        text = ["Approximate Projected Scores"] + score
        return "\n".join(text)

    def get_matchups(self, week=None):
        # Gets current week's Matchups
        self.refresh_league()
        matchups = self.box_scores(week=week)

        score = [
            "%s(%s-%s) vs %s(%s-%s)"
            % (
                i.home_team.team_name,
                i.home_team.wins,
                i.home_team.losses,
                i.away_team.team_name,
                i.away_team.wins,
                i.away_team.losses,
            )
            for i in matchups
            if i.away_team
        ]
        text = ["Matchups:"] + score
        return "\n".join(text)

    def get_close_scores(self, week=None):
        # Gets current closest scores (15.999 points or closer)
        self.refresh_league()
        matchups = self.box_scores(week=week)
        score = []

        for i in matchups:
            if i.away_team:
                diffScore = i.away_score - i.home_score
                if (-16 < diffScore <= 0 and not self.all_played(i.away_lineup)) or (
                    0 <= diffScore < 16 and not self.all_played(i.home_lineup)
                ):
                    score += [
                        "%s %.2f - %.2f %s"
                        % (i.home_team.team_abbrev, i.home_score, i.away_score, i.away_team.team_abbrev)
                    ]
        if not score:
            return ""
        text = ["Close Scores"] + score
        return "\n".join(text)

    # TODO: Add chatGPT interface here to generate team summaries
    def get_power_rankings(self, week=None):
        self.refresh_league()
        # power rankings requires an integer value, so this grabs the current week for that
        if not week:
            week = self.league.current_week
        # Gets current week's power rankings
        # Using 2 step dominance, as well as a combination of points scored and margin of victory.
        # It's weighted 80/15/5 respectively
        power_rankings = self.league.power_rankings(week=week)

        score = ["%s - %s" % (i[0], i[1].team_name) for i in power_rankings if i]
        text = ["Power Rankings"] + score
        return "\n".join(text)

    def get_last_place_team(self):
        return self.league.standings()[-1]

    def get_trophies(self, week=None):
        self.refresh_league()
        # Gets trophies for highest score, lowest score, closest score, and biggest win
        # if not week:
        # 	week = self.power_rankings_week()

        # if datetime.datetime.today().weekday() in [0,1,2] and self.league.current_week > 0:
        # 	week = self.league.current_week - 1
        # else:
        # 	week = self.league.current_week

        matchups = self.box_scores(week=week)
        low_score = 9999
        low_team_name = ""
        high_score = -1
        high_team_name = ""
        closest_score = 9999
        close_winner = ""
        close_loser = ""
        biggest_blowout = -1
        blown_out_team_name = ""
        ownerer_team_name = ""

        for i in matchups:
            if not i.away_team:
                continue
            if i.home_score > high_score:
                high_score = i.home_score
                high_team_name = i.home_team.team_name
            if i.home_score < low_score:
                low_score = i.home_score
                low_team_name = i.home_team.team_name
            if i.away_score > high_score:
                high_score = i.away_score
                high_team_name = i.away_team.team_name
            if i.away_score < low_score:
                low_score = i.away_score
                low_team_name = i.away_team.team_name
            if abs(i.away_score - i.home_score) < closest_score:
                closest_score = abs(i.away_score - i.home_score)
                if i.away_score - i.home_score < 0:
                    close_winner = i.home_team.team_name
                    close_loser = i.away_team.team_name
                else:
                    close_winner = i.away_team.team_name
                    close_loser = i.home_team.team_name
            if abs(i.away_score - i.home_score) > biggest_blowout:
                biggest_blowout = abs(i.away_score - i.home_score)
                if i.away_score - i.home_score < 0:
                    ownerer_team_name = i.home_team.team_name
                    blown_out_team_name = i.away_team.team_name
                else:
                    ownerer_team_name = i.away_team.team_name
                    blown_out_team_name = i.home_team.team_name

        low_score_str = ["Low score: %s with %.2f points" % (low_team_name, low_score), ""]
        high_score_str = ["High score: %s with %.2f points" % (high_team_name, high_score), ""]

        # Check that the closest score is reasonably close
        if closest_score <= 15:
            close_score_str = ["%s barely beat %s by a margin of %.2f" % (close_winner, close_loser, closest_score), ""]
        else:
            close_score_str = ["None of these games were especially close. Try to do better next week.", ""]

        # IF the blowout is more than 20 points, report it
        # Otherwise, don't report a blowout (but congratulate last place on not losing hard!)
        if biggest_blowout >= 20:
            blowout_str = [
                "%s blown out by %s by a margin of %.2f" % (blown_out_team_name, ownerer_team_name, biggest_blowout),
                "",
            ]
        else:
            last_place_team = self.get_last_place_team()
            blowout_str = [
                "No teams were destroyed this week. (Good job {}!)".format(last_place_team.owner.split(" ")[0].title()),
                "",
            ]

        text = ["Trophies of the week:"] + low_score_str + high_score_str + close_score_str + blowout_str
        return "\n".join(text)

    def get_final(self):
        self.refresh_league()
        week = self.league.current_week - 1
        text = "Final " + self.get_scoreboard_short(week=week)
        text = text + "\n\n" + self.get_trophies(week=week)

        return text

    def mock_user(self, text):
        msg_list = text.lower().split(" ")
        mock_msg_list = []
        punctuation = {",", "'", '"', "-", ":", ";", "!", "@", "#", "$", "%", "&"}
        for word in msg_list:
            mock_word = ""
            punctuation_offset = 0
            for idx, letter in enumerate(word):
                if letter in punctuation:
                    punctuation_offset += 1

                idx -= punctuation_offset

                if idx % 2 != 0:
                    letter = letter.upper()
                mock_word += letter
            mock_msg_list.append(mock_word)

        return " ".join(mock_msg_list)

from utils.players import Projections, SleeperPlayers
import datetime
import re
from collections import namedtuple

class Commands:
    def __init__(self, league):
        self.league = league

        self.TeamStats = namedtuple("TeamStats", ["wins", "losses", "team_name", "points_for"])
        # A mapping from groupme user ID to team number
        self.gm_id_to_team = {
                            11847036 : 0,
                            4334131 : 1,
                            11847032 : 2,
                            30849273 : 3,
                            11847033 : 4,
                            2249412 : 5,
                            11847034 : 6,
                            26527139 : 7,
                            577750 : 8,
                            399917 : 9}


    def commands_help(self):
        text = "You can use the following commands:\n"
        text += "/matchups - Returns this week's matchups\n"
        text += "/standings - Returns the overall league standings\n"
        text += "/scores - Returns this week's scores\n"
        text += "/projections - Returns the projected points for each of your players this week\n"

        return text


    def get_scoreboard_short(self, week=None):
        #Gets current week's scoreboard
        box_scores = self.league.box_scores(week=week)
        score = ['%s %.2f - %.2f %s' % (i.home_team.team_abbrev, i.home_score,
                i.away_score, i.away_team.team_abbrev) for i in box_scores
                if i.away_team]
        text = ['Score Update'] + score
        return '\n'.join(text)

    def get_projected_total(self, lineup):
        total_projected = 0
        for i in lineup:
            if i.slot_position != 'BE':
                if i.points != 0 or i.game_played > 0:
                    total_projected += i.points
                else:
                    total_projected += i.projected_points
        return total_projected

    def get_standings(self, week=None):
        teams = self.league.teams

        top_half_totals = {t.team_name: 0 for t in teams}
        if not week:
            week = self.league.current_week
        for w in range(1, week):
            top_half_totals = self.top_half_wins(top_half_totals, w)

        standings = []
        for t in teams:
            wins = top_half_totals[t.team_name] + t.wins
            standings.append(self.TeamStats(wins, t.losses, t.team_name, t.points_for))

        # Sort standings by the following criteria:
        # 1) Most wins
        # 2) Least losses
        # 3) Most 'Points For'
        standings = sorted(standings, key=lambda x: (x.wins, -x.losses, x.points_for), reverse=True)

        standings_txt = [f"{pos + 1}: {team_name} ({wins} - {losses}) (+{top_half_totals[team_name]})" for \
            pos, (wins, losses, team_name, pf) in enumerate(standings)]
        text = ["Current Standings:"] + standings_txt

        return "\n".join(text)

    def top_half_wins(self, top_half_totals, week):
        # Todo: Consider caching the scores for earlier weeks so this only has to be run once per day
        box_scores = self.league.box_scores(week=week)
        
        scores = [(i.home_score, i.home_team.team_name) for i in box_scores] + \
                [(i.away_score, i.away_team.team_name) for i in box_scores if i.away_team]

        scores = sorted(scores, key=lambda tup: tup[0], reverse=True)

        for idx in range(0, len(scores)//2):
            points, team_name = scores[idx]
            top_half_totals[team_name] += 1

        return top_half_totals


    def all_played(self, lineup):
        for i in lineup:
            if i.slot_position != 'BE' and i.game_played < 100:
                return False
        return True

    def get_recent_activity(self, size=20, offset=0):
        """
        Returns a list of Activity objects from espn_api.football.activity
        """
        return self.league.recent_activity(size=size, offset=offset)

    def get_projected_scoreboard(self, week=None):
        #Gets current week's scoreboard projections
        box_scores = self.league.box_scores(week=week)
        score = ['%s %.2f - %.2f %s' % (i.home_team.team_abbrev, self.get_projected_total(i.home_lineup),
                                        self.get_projected_total(i.away_lineup), i.away_team.team_abbrev) for i in box_scores
                if i.away_team]
        text = ['Approximate Projected Scores'] + score
        return '\n'.join(text)

    def get_matchups(self, week=None):
        #Gets current week's Matchups
        matchups = self.league.box_scores(week=week)

        score = ['%s(%s-%s) vs %s(%s-%s)' % (i.home_team.team_name, i.home_team.wins, i.home_team.losses,
                i.away_team.team_name, i.away_team.wins, i.away_team.losses) for i in matchups
                if i.away_team]
        text = ['Matchups:'] + score
        return '\n'.join(text)

    def get_close_scores(self, week=None):
        #Gets current closest scores (15.999 points or closer)
        matchups = self.league.box_scores(week=week)
        score = []

        for i in matchups:
            if i.away_team:
                diffScore = i.away_score - i.home_score
                if ( -16 < diffScore <= 0 and not self.all_played(i.away_lineup)) or (0 <= diffScore < 16 and not self.all_played(i.home_lineup)):
                    score += ['%s %.2f - %.2f %s' % (i.home_team.team_abbrev, i.home_score,
                            i.away_score, i.away_team.team_abbrev)]
        if not score:
            return('')
        text = ['Close Scores'] + score
        return '\n'.join(text)

    # TODO: Add chatGPT interface here to generate team summaries
    def get_power_rankings(self, week=None):
        # power rankings requires an integer value, so this grabs the current week for that
        if not week:
            week = self.league.current_week
        #Gets current week's power rankings
        #Using 2 step dominance, as well as a combination of points scored and margin of victory.
        #It's weighted 80/15/5 respectively
        power_rankings = self.league.power_rankings(week=week)

        score = ['%s - %s' % (i[0], i[1].team_name) for i in power_rankings
                if i]
        text = ['Power Rankings'] + score
        return '\n'.join(text)

    def get_last_place_team(self):
        return self.league.standings()[-1]

    def get_trophies(self, week=None):
        # Gets trophies for highest score, lowest score, closest score, and biggest win
        # if not week:
        # 	week = self.power_rankings_week()

        # if datetime.datetime.today().weekday() in [0,1,2] and self.league.current_week > 0:
        # 	week = self.league.current_week - 1
        # else:
        # 	week = self.league.current_week

        matchups = self.league.box_scores(week=week)
        low_score = 9999
        low_team_name = ''
        high_score = -1
        high_team_name = ''
        closest_score = 9999
        close_winner = ''
        close_loser = ''
        biggest_blowout = -1
        blown_out_team_name = ''
        ownerer_team_name = ''

        for i in matchups:
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

        low_score_str = ['Low score: %s with %.2f points' % (low_team_name, low_score), ""]
        high_score_str = ['High score: %s with %.2f points' % (high_team_name, high_score), ""]

        # Check that the closest score is reasonably close
        if closest_score <= 15:
            close_score_str = ['%s barely beat %s by a margin of %.2f' % (close_winner, close_loser, closest_score), ""]
        else:
            close_score_str = ["None of these games were especially close. Try to do better next week.", ""]

        # IF the blowout is more than 20 points, report it
        # Otherwise, don't report a blowout (but congratulate last place on not losing hard!)
        if biggest_blowout >= 20:
            blowout_str = [
                '%s blown out by %s by a margin of %.2f' % (blown_out_team_name, ownerer_team_name, biggest_blowout), ""]
        else:
            last_place_team = self.get_last_place_team()
            blowout_str = [
                "No teams were destroyed this week. (Good job {}!)".format(last_place_team.owner.split(" ")[0].title()), ""]

        text = ['Trophies of the week:'] + low_score_str + high_score_str + close_score_str + blowout_str
        return '\n'.join(text)

    def get_final(self):
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
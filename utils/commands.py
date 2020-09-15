from utils.players import Projections, SleeperPlayers
import datetime

class Commands:
	def __init__(self, gm_bot, league):
		self.gm_bot = gm_bot
		self.league = league

		# self.sleeper_players = SleeperPlayers()

		# self.projections = Projections(self.league.year, self.league.nfl_week)

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

		self.last_message = ""

		# Mapping of GroupMe command to class method
		self.cmd_dict = {
			"/help": self.commands_help,
			"/matchups": self.get_matchups,
			"/scores": self.get_scoreboard_short,
			"/close": self.get_close_scores,
			"/pwr": self.get_power_rankings,
			"/trophies": self.get_trophies,
			"/final": self.get_final,
			"/projections": self.get_projected_scoreboard,
			"/standings": self.get_standings,
			"/mock": self.mock_user,
		}
		

	def parse(self, gm_data):
		# Receives groupme data (name of sender, content of message, etc) as json

		# Split the message text into words and make them all lowercase, then take the first word
		cmd_word = gm_data['text'].lower().split()[0]
		
		text = ''
		if not cmd_word.startswith("/"):
			# If the first word of a message doesn't start with a "/", then it isn't a command
			self.last_message = gm_data['text']
			print("Last message is: {}".format(self.last_message))
			return None
		elif cmd_word in self.cmd_dict:
			# Run the corresponding class method
			text = self.cmd_dict[cmd_word]()
		else:
			text = "Sorry, {} is not a valid command.".format(cmd_word)

		return text

	def commands_help(self):
		text = "You can use the following commands:\n"
		text += "/matchups - Returns this week's matchups\n"
		text += "/scores - Returns this week's scores\n"
		text += "/close - Returns close scores from this week's FF matchups\n"
		text += "/pwr - Returns the power rankings for each team in the league\n"
		text += "/trophies - Returns a list of various awards\n"
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
		matchups = self.league.box_scores(week=week)

		standings = [(i.home_team.standing, i.home_team.team_name, i.home_team.wins, i.home_team.losses) for i in matchups] + \
					[(i.away_team.standing, i.away_team.team_name, i.away_team.wins, i.away_team.losses) for i in matchups if i.away_team]

		standings = sorted(standings, key=lambda tup: tup[0])
		standings_txt = [f"{pos}: {team_name} ({wins} - {losses})" for pos, team_name, wins, losses in standings]
		text = ["Current Standings:"] + standings_txt

		return "\n".join(text)

	def all_played(self, lineup):
		for i in lineup:
			if i.slot_position != 'BE' and i.game_played < 100:
				return False
		return True

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
		text = ['Matchups'] + score
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

	# def get_team_projections(self, user_id):
	# 	"""Checks GroupMe ID of sender and returns the projections for their team"""
	# 	# Try to update player projections
	# 	self.projections.fetch_projs(self.league.nfl_week)

	# 	# Figure out which team is being referenced by checking the sender's ID
	# 	user_team_num = self.gm_id_to_team[int(user_id)]
	# 	user_team = self.league.teams[user_team_num]

	# 	text = "Projections for {}:\n\n".format(user_team.team_name)

	# 	for player in user_team.roster:
	# 		# player is a Player object
	# 		# Get the sleeper player object from the player's espn ID
	# 		print("{} - {}".format(player.playerId, player.name))

	# 		if player.playerId < 0:
	# 			# Filter out negative espn player IDs (used for defences only)
	# 			continue

	# 		try:
	# 			# Catch any key errors that occur here
	# 			sleeper_player = self.sleeper_players.espn_indexing[player.playerId]
	# 			player_proj = self.projections.all_projs[str(sleeper_player.sleeper_id)]
	# 			text += "{} - {}\n".format(sleeper_player, player_proj.pts_half_ppr)
	# 		except KeyError:
	# 			# Handle the key error by saying here is no projection for the player
	# 			text += "{} - n/a\n".format(sleeper_player, player_proj.pts_half_ppr)


	# 	return text

	def mock_user(self):
		msg_list = self.last_message.lower().split(" ")
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



	def send_message(self, text):
		self.gm_bot.send_message(text)


if __name__ == '__main__':

	msg_list = "Look guys I'm just saying...".lower().split(" ")
	mock_msg_list = []
	# Offset index by number of punctuation characters so that punctuation doesn't interfere with capitalization of letters
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

	print(" ".join(mock_msg_list))
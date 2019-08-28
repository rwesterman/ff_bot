from utils.players import Projections, SleeperPlayers

class Commands:
	def __init__(self, gm_bot, league):
		self.gm_bot = gm_bot
		self.league = league

		self.sleeper_players = SleeperPlayers()

		self.projections = Projections(self.league.year, self.league.nfl_week)

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

	def parse(self, gm_data):
		# Receives groupme data (name of sender, content of message, etc) as json

		# Split the message text into words and make them all lowercase, then take the first word
		cmd_word = gm_data['text'].lower().split()[0]

		text = ''
		if not cmd_word.startswith("/"):
			# If the first word of a message doesn't start with a "/", then it isn't a command
			return None
		elif cmd_word == "/help":
			text = self.commands_help()
		elif cmd_word == "/matchups":
			text = self.get_matchups()
		# elif cmd_word == "/scoreboard":
		# 	text = self.get_scoreboard(self.league)
		elif cmd_word == "/scores":
			text = self.get_scoreboard_short()
		elif cmd_word == "/close":
			text = self.get_close_scores()
		elif cmd_word == "/pwr":
			text = self.get_power_rankings()
		elif cmd_word == "/trophies":
			text = self.get_trophies()
		elif cmd_word == "/final":
			text = self.get_final()
		elif cmd_word == "/projections":
			text = self.get_team_projections(gm_data["sender_id"])
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

	def power_rankings_week(self):
		count = 1
		first_team = next(iter(self.league.teams or []), None)
		# Iterate through the first team's scores until you reach a week with 0 points scored
		for o in first_team.scores:
			if o == 0:
				if count != 1:
					count = count - 1
				break
			else:
				count = count + 1

		return count

	def get_scoreboard_short(self, final=False):
		# Gets current week's scoreboard
		if not final:
			matchups = self.league.scoreboard()
		else:
			matchups = self.league.scoreboard(week=self.power_rankings_week())
		score = ['%s %.2f - %.2f %s' % (i.home_team.team_abbrev, i.home_score,
		                                i.away_score, i.away_team.team_abbrev) for i in matchups
		         if i.away_team]
		text = ['Score Update'] + score
		return '\n'.join(text)

	def get_scoreboard(self):
		# Gets current week's scoreboard
		matchups = self.league.scoreboard()
		score = ['%s %.2f - %.2f %s' % (i.home_team.team_name, i.home_score,
		                                i.away_score, i.away_team.team_name) for i in matchups
		         if i.away_team]
		text = ['Score Update'] + score
		return '\n'.join(text)

	def get_matchups(self):
		# Gets current week's Matchups
		matchups = self.league.scoreboard()

		score = ['%s(%s-%s) vs %s(%s-%s)' % (i.home_team.team_name, i.home_team.wins, i.home_team.losses,
		                                     i.away_team.team_name, i.away_team.wins, i.away_team.losses) for i in
		         matchups if i.away_team]
		text = ['This Week\'s Matchups'] + score + ['\n']
		return '\n'.join(text)

	def get_close_scores(self):
		# Gets current closest scores (15.999 points or closer)
		matchups = self.league.scoreboard()
		score = []

		for i in matchups:
			if i.away_team:
				diffScore = i.away_score - i.home_score
				if -16 < diffScore < 16:
					score += ['%s %.2f - %.2f %s' % (i.home_team.team_abbrev, i.home_score,
					                                 i.away_score, i.away_team.team_abbrev)]
		if not score:
			score = ['None']
		text = ['Close Scores'] + score
		return '\n'.join(text)

	def get_power_rankings(self):
		# Gets current week's power rankings
		# Using 2 step dominance, as well as a combination of points scored and margin of victory.
		# It's weighted 80/15/5 respectively
		power_rankings = self.league.power_rankings(week=self.power_rankings_week())

		score = ['%s - %s' % (i[0], i[1].team_name) for i in power_rankings
		         if i]
		text = ['This Week\'s Power Rankings'] + score
		return '\n'.join(text)

	def get_last_place_team(self):
		return self.league.standings()[-1]

	def get_trophies(self, week=None):
		# Gets trophies for highest score, lowest score, closest score, and biggest win
		if not week:
			week = self.power_rankings_week()
		matchups = self.league.scoreboard(week=week)
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
				"No teams were destroyed this week. (Good job {}!)".format(last_place_team.owner.split(" ")[0]), ""]

		text = ['Trophies of the week:'] + low_score_str + high_score_str + close_score_str + blowout_str
		return '\n'.join(text)

	def get_final(self):
		text = "Final " + self.get_scoreboard_short(True)
		text = text + "\n\n" + self.get_trophies(self.league)

		return text

	def get_team_projections(self, user_id):
		"""Checks GroupMe ID of sender and returns the projections for their team"""
		# Try to update player projections
		self.projections.fetch_projs(self.league.nfl_week)

		# Figure out which team is being referenced by checking the sender's ID
		user_team_num = self.gm_id_to_team[int(user_id)]
		user_team = self.league.teams[user_team_num]

		text = "Projections for {}:\n\n".format(user_team.team_name)

		for player in user_team.roster:
			# player is a Player object
			# Get the sleeper player object from the player's espn ID
			print("{} - {}".format(player.playerId, player.name))

			if player.playerId < 0:
				# Filter out negative espn player IDs (used for defences only)
				continue

			try:
				# Catch any key errors that occur here
				sleeper_player = self.sleeper_players.espn_indexing[player.playerId]
				player_proj = self.projections.all_projs[str(sleeper_player.sleeper_id)]
				text += "{} - {}\n".format(sleeper_player, player_proj.pts_half_ppr)
			except KeyError:
				# Handle the key error by saying here is no projection for the player
				text += "{} - n/a\n".format(sleeper_player, player_proj.pts_half_ppr)


		return text


	def send_message(self, text):
		self.gm_bot.send_message(text)
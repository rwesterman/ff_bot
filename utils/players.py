import json
import os
from datetime import datetime, timedelta
from collections import defaultdict

import requests

class SleeperPlayer:
	def __init__(self, sleeper_id, attrs):
		self.sleeper_id = sleeper_id
		self.full_name = attrs["full_name"].lower() # make all names lowercase
		self.first_name = attrs["search_first_name"]
		self.last_name = attrs["search_last_name"]

		# Playing info
		self.active = attrs["active"]
		self.position = attrs["position"]
		self.depth_chart_order = attrs["depth_chart_order"]
		self.fantasy_position = attrs["fantasy_positions"] # This is a list

		self.rotowire_id = attrs["rotowire_id"]
		self.espn_id = attrs["espn_id"]
		self.yahoo_id = attrs["yahoo_id"]
		self.fantasy_data_id = attrs["fantasy_data_id"]

		# Misc
		self.college = attrs["college"]

	def __repr__(self):
		return "{}: {}".format(self.position, self.full_name.title())


class SleeperPlayers:
	def __init__(self):
		# Make dictionary with lists as values
		# Index all Players by their last names
		self.last_name_dict = defaultdict(list)
		# Index all players by their espn numbers
		self.espn_indexing = {}

		self.sleeper_indexing = {}

		# populate these dictionaries from players.json
		self.populate_players()

	def populate_players(self):
		"""Read in players.json file and populate all players into the two dictionaries"""

		with open(os.path.join("..","players.json"), "r") as fp:
			player_json = json.load(fp)

		for sleeper_id, player_attrs in player_json.items():
			try:
				a_player = SleeperPlayer(sleeper_id, player_attrs)
				# Add this new player to the list of players with his last name.
				# The idea is that each of these lists should be short enough to iterate through when needed
				self.last_name_dict[a_player.last_name].append(a_player)

				# Index each player by his ESPN ID for easy lookup when finding projections
				self.espn_indexing[a_player.espn_id] = a_player
				# Index by sleeper ID for projection grab
				self.sleeper_indexing[sleeper_id] = a_player

			except KeyError:
				print("{} is not a player".format(sleeper_id))


class Projection:
	def __init__(self, sleeper_id, proj):
		self.sleeper_id = sleeper_id
		try:
			self.td = proj["td"]
		except KeyError:
			self.td = 0

		try:
			self.rush_yards = proj["rush_yd"]
		except KeyError:
			self.rush_yards = 0

		try:
			self.rec_yards = proj["rec_yd"]
		except KeyError:
			self.rec_yards = 0

		try:
			self.pts_std = proj["pts_std"]
		except KeyError:
			self.pts_std = 0

		try:
			self.pts_ppr = proj["pts_ppr"]
		except KeyError:
			self.pts_ppr = 0

		try:
			self.pts_half_ppr = proj["pts_half_ppr"]
		except KeyError:
			self.pts_half_ppr = 0

	def __repr__(self):
		return "Standard: {}, PPR: {}, Half-PPR: {}".format(self.pts_std, self.pts_ppr, self.pts_half_ppr)

class Projections:
	"""This class holds the projections for all players on a given week"""
	def __init__(self, year, week):
		self.year = year
		# Initialize all_projs to empty dict to check if it has been populated
		self.all_projs = {}

		self.projs_last_updated = datetime.now()
		self.fetch_projs(week)

	def fetch_projs(self, week):
		"""Get projections for all players for a given week, and store these in the Projections object"""

		# Check that projections weren't updated within the last day before fetching them again
		if (not datetime.now() - self.projs_last_updated > timedelta(days=1)) and self.all_projs:
			print("Projections already cached for the day!")
			return

		response = requests.get("https://api.sleeper.app/v1/projections/nfl/regular/{}/{}".format(self.year, week))
		response.raise_for_status()

		# self.all_projs is dictionary with Key=Sleeper ID, Value = Dictionary of projections (points, touchdown, rush yards, etc)
		proj_dict = response.json()

		for sleeper_id, proj in proj_dict.items():
			self.all_projs[sleeper_id] = Projection(sleeper_id, proj)

if __name__ == '__main__':
	myplayers = SleeperPlayers()
	projections = Projections(2019, 1)

	print(myplayers.last_name_dict["kamara"])
	alv_kamara = myplayers.last_name_dict["kamara"][0]

	kamara_proj = projections.all_projs[alv_kamara.sleeper_id]
	print(kamara_proj.rush_yards)
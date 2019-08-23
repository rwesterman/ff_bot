from ff_espn_api import League
from collections import defaultdict
from statistics import mean, median
from pprint import pprint
from ff_bot.ff_bot import get_trophies
#League ID and year

league_id = 950634
# year = 2018
# Secret stuff
espn_s2 = "AECcqBAxkb6iztLdTzvhM6dSAdobKKCPuSY8DF3qSTGmjjVUtPZT8NSSv7KywiL569X2Ml8wZb0rxUNrUY%2F1ky%2FSzYlFigLbX%2FQZhA8D7nkkB752d9kMJmWO6B43%2FZFspi1tyvRPUPSciqK1A0hsYMI9HYyUa37MLrQFTbXrEcSwpb1%2BH0uwWdmm2%2BS2GZM04fjCWtC4GjuIgdBx%2FxE8VYOz6STEAPyGSn9RxDonMuDrCGHEljM1a1I2vi4m3eesI9Rmx%2FqH0kq0Sv7ybGL0YxHD"
swid = "{BFD1DF0E-0120-4EF1-A54E-128EAE53AA82}"

def calculate_overall_pwrs():
	teamscores = defaultdict(list)
	for year in range(2012, 2019):
		myleague = League(league_id, year, espn_s2, swid)
		all_teams = myleague.teams
		num_teams = len(all_teams)
		pwr_ranks = myleague.power_rankings(myleague.current_week)

		# for score, team in pwr_ranks:
		# 	owner = team.owner
		# 	teamscores[owner].append(float(score))
		for team in all_teams:
			owner = team.owner
			# print("{} finished in {} place".format(owner, team.final_standing))
			# print("{} gets {} points".format(owner, num_teams - team.final_standing))
			# Append the number of points each player gets
			teamscores[owner].append(team.final_standing)

	# pprint(teamscores)
	averages_tuples = []
	team_avgs = {}
	team_medians = {}
	# print("Average power rankings across all seasons")
	print("Average across all seasons")
	for owner, pointslist in teamscores.items():
		avg_points = mean(pointslist)
		team_medians[owner] = median(pointslist)
		team_avgs[owner] = avg_points

		averages_tuples.append((owner, avg_points))

	sorted_tuples = sorted(averages_tuples, key=lambda x: x[1], reverse = True)
	for owner, points in sorted_tuples:
		print("{} - {:.2f}".format(owner, points))

def try_league_stuff(league):
	print(league.power_rankings(league.current_week))
	# league.p

def get_last_place_team(league):
	print(league.standings()[-1].owner.split(" ")[0])
	# pwr_ranks = league.power_rankings(league.current_week)
	# print(pwr_ranks[-1][1].owner.split(" ")[0])

def check_trophies(league, week):
	trophies = get_trophies(league, week)
	print(trophies)

def check_box_scores(league, week):
	box_score_list = league.box_scores(week)
	# pprint(league.box_scores(week))
	for matchup in box_score_list:
		print("{}: {} -- {}: {}".format(matchup.home_team, matchup.home_score, matchup.away_team, matchup.away_score))
	print()

if __name__ == '__main__':
	# calculate_overall_pwrs()
	myleague = League(league_id, 2018, espn_s2, swid)
	for i in range(1, 8):
		# check_trophies(myleague, i)
		check_box_scores(myleague, i)
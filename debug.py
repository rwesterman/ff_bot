import os
from wsgi import commander

os.environ["DEBUG"] = "True"
# Check if debug mode is set, and if so then default to debug groupme bot
if os.getenv("DEBUG", False):
	os.environ["ESPN_S2"] = "AECcqBAxkb6iztLdTzvhM6dSAdobKKCPuSY8DF3qSTGmjjVUtPZT8NSSv7KywiL569X2Ml8wZb0rxUNrUY%2F1ky%2FSzYlFigLbX%2FQZhA8D7nkkB752d9kMJmWO6B43%2FZFspi1tyvRPUPSciqK1A0hsYMI9HYyUa37MLrQFTbXrEcSwpb1%2BH0uwWdmm2%2BS2GZM04fjCWtC4GjuIgdBx%2FxE8VYOz6STEAPyGSn9RxDonMuDrCGHEljM1a1I2vi4m3eesI9Rmx%2FqH0kq0Sv7ybGL0YxHD"

	os.environ["SWID"] = "{BFD1DF0E-01204EF1-A54E-128EAE53AA82}"

	os.environ["BOT_ID"] = "d6b7111ac8a3b7da98aed334ed"

	os.environ["LEAGUE_YEAR"] = "2019"

	os.environ["LEAGUE_ID"] = "950634"


if __name__ == '__main__':
	while True:
		dbg_cmd = input("What command should I debug?  ")
		if dbg_cmd == "pwr":
			text = commander.get_power_rankings()
		elif dbg_cmd == "matchups":
			text = commander.get_matchups()
		elif dbg_cmd == "close_scores":
			text = commander.get_close_scores()
		elif dbg_cmd == "scores":
			text = commander.get_scoreboard_short()
		elif dbg_cmd == "final":
			text = commander.get_final()
		else:
			text = "Sorry, that's not a valid selection."

		print(text)

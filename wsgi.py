import os
import logging

from espn_api.football import League
from apscheduler.schedulers.background import BackgroundScheduler

from utils.bots import GroupMeBot, SlackBot, DiscordBot
from utils.commands import Commands

from flask import Flask, request

app = Flask(__name__)

# Set up logging here
logger = logging.getLogger("flask")
logging.basicConfig(level=logging.DEBUG)


@app.route('/FF', methods=['POST'])
def ff_webhook():
	"""This webhook is called whenever a message is sent to the group chat.
	The message is checked for a command, then the message text is sent to the Commands object so the
	appropriate action can be taken."""
	# data received at GroupMe callback URL
	gm_data = request.get_json()
	logger.info("Received {}".format(gm_data))

	# Don't respond to bots
	if not "bot" in gm_data['name'].lower():
		response = commander.parse(gm_data)
		# Check to see that there's a message to send
		if response:
			commander.send_message(response)

	# This prevents a ValueError raised by Flask
	return "OK"

def init_scheduler():
	"""
	Schedule the chatbot to report scores/matchups/etc at particular times
	:return:
	"""
	ff_start_date = os.getenv("START_DATE", '2020-09-10')
	ff_end_date = os.getenv("END_DATE", '2019-12-30')

	my_timezone = os.getenv("TIMEZONE",'America/New_York')

	game_timezone='America/New_York'
	sched = BackgroundScheduler(job_defaults={'misfire_grace_time': 15*60})

	#power rankings:                     tuesday evening at 6:30pm local time.
	#matchups:                           thursday evening at 7:30pm east coast time.
	#close scores (within 15.99 points): monday evening at 6:30pm east coast time.
	#trophies:                           tuesday morning at 7:30am local time.
	#score update:                       friday, monday, and tuesday morning at 7:30am local time.
	#score update:                       sunday at 4pm, 8pm east coast time.

	sched.add_job(commander.get_power_rankings, 'cron', id='power_rankings',
		day_of_week='tue', hour=18, minute=30, start_date=ff_start_date, end_date=ff_end_date,
		timezone=my_timezone, replace_existing=True)
	sched.add_job(commander.get_matchups, 'cron', id='matchups',
		day_of_week='thu', hour=19, minute=30, start_date=ff_start_date, end_date=ff_end_date,
		timezone=game_timezone, replace_existing=True)
	sched.add_job(commander.get_close_scores, 'cron', id='close_scores',
		day_of_week='mon', hour=18, minute=30, start_date=ff_start_date, end_date=ff_end_date,
		timezone=game_timezone, replace_existing=True)
	sched.add_job(commander.get_final, 'cron', id='final',
		day_of_week='tue', hour=7, minute=30, start_date=ff_start_date, end_date=ff_end_date,
		timezone=my_timezone, replace_existing=True)
	sched.add_job(commander.get_scoreboard_short, 'cron', id='scoreboard1',
		day_of_week='fri,mon', hour=7, minute=30, start_date=ff_start_date, end_date=ff_end_date,
		timezone=my_timezone, replace_existing=True)
	sched.add_job(commander.get_scoreboard_short, 'cron', id='scoreboard2',
		day_of_week='sun', hour='16,20', start_date=ff_start_date, end_date=ff_end_date,
		timezone=game_timezone, replace_existing=True)

	sched.start()
	print("Ready!")

def initialize_bot():
	"""Initialize a chatbot using the required Environmental Variables
	Return each bot (slack, discord, groupme) and the Leauge object
	"""

	bot_id = os.getenv("BOT_ID", 1)
	slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL", 1)
	discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", 1)
	league_id = int(os.getenv("LEAGUE_ID", "1"))
	year = int(os.getenv("LEAGUE_YEAR", 2019))
	swid = os.getenv("SWID", "{1}")

	if swid.find("{", 0) == -1:
		swid = "{" + swid
	if swid.find("}", -1) == -1:
		swid = swid + "}"

	espn_s2 = os.getenv("ESPN_S2", "1")

	bot = GroupMeBot(bot_id)
	slack_bot = SlackBot(slack_webhook_url)
	discord_bot = DiscordBot(discord_webhook_url)
	if swid == '{1}' and espn_s2 == '1':
		league = League(league_id, year)
	else:
		league = League(league_id, year, espn_s2, swid)

	return {"gm_bot": bot, "slack_bot": slack_bot, "discord_bot": discord_bot, "league": league}

# os.environ["DEBUG"] = "True"
# Check if debug mode is set, and if so then default to debug groupme bot
if os.getenv("DEBUG", False):
	os.environ["ESPN_S2"] = "AECcqBAxkb6iztLdTzvhM6dSAdobKKCPuSY8DF3qSTGmjjVUtPZT8NSSv7KywiL569X2Ml8wZb0rxUNrUY%2F1ky%2FSzYlFigLbX%2FQZhA8D7nkkB752d9kMJmWO6B43%2FZFspi1tyvRPUPSciqK1A0hsYMI9HYyUa37MLrQFTbXrEcSwpb1%2BH0uwWdmm2%2BS2GZM04fjCWtC4GjuIgdBx%2FxE8VYOz6STEAPyGSn9RxDonMuDrCGHEljM1a1I2vi4m3eesI9Rmx%2FqH0kq0Sv7ybGL0YxHD"

	os.environ["SWID"] = "{BFD1DF0E-01204EF1-A54E-128EAE53AA82}"

	os.environ["BOT_ID"] = "d6b7111ac8a3b7da98aed334ed"

	os.environ["LEAGUE_YEAR"] = "2020"

	os.environ["LEAGUE_ID"] = "950634"

# Initialize an instance of the chatbot and create a Commands instance for the bot
init_dict = initialize_bot()
commander = Commands(init_dict["gm_bot"], init_dict["league"])

# Do scheduler initialization here
init_scheduler()

if __name__ == '__main__':
	# Run the flask app if this script is called directly
	app.run(host="0.0.0.0", debug=True)



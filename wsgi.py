import os
import logging

from espn_api.football import League
# from apscheduler.schedulers.background import BackgroundScheduler

from utils.bots import GroupMeBot, SlackBot, DiscordBot
from utils.commands import Commands
import slack_sdk as slack
from slack_sdk.errors import SlackRequestError, SlackApiError
from flask import Flask, request, make_response

app = Flask(__name__)

# Set up logging here
logger = logging.getLogger("flask")
logging.basicConfig(level=logging.DEBUG)

bot_token = os.getenv("SLACK_BOT_TOKEN", None)
slack_client = slack.WebClient(token=bot_token)

def initialize_bot():
	"""Initialize a chatbot using the required Environmental Variables
	Return each bot (slack, discord, groupme) and the Leauge object
	"""

	bot_id = os.getenv("BOT_ID", 1)
	slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL", 1)
	discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", 1)
	league_id = int(os.getenv("LEAGUE_ID", "1"))
	year = int(os.getenv("LEAGUE_YEAR", 2022))
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

# Initialize an instance of the chatbot and create a Commands instance for the bot
init_dict = initialize_bot()
commander = Commands(init_dict["slack_bot"], init_dict["league"])

@app.route('/FF', methods=['POST'])
def ff_webhook():
	"""This webhook is called whenever a message is sent to the group chat.
	The message is checked for a command, then the message text is sent to the Commands object so the
	appropriate action can be taken."""
	# data received at GroupMe callback URL
	gm_data = request.get_json()
	logger.debug("Received {}".format(gm_data))

	# Don't respond to bots
	if not "bot" in gm_data['name'].lower():
		response = commander.parse(gm_data)
		# Check to see that there's a message to send
		if response:
			commander.send_message(response)

	# This prevents a ValueError raised by Flask
	return "OK"

# @app.route("/event/", methods=['POST'])
# def event_webhook():
# 	logger.debug("Received POST command to /event/")
# 	data = request.form
# 	logger.debug(f"Event data: {data}")

# 	return "OK"

# https://ff-bot-groupme.herokuapp.com/help/
@app.route("/help/", methods=['POST'])
def help():
	try:
		data = request.form
		logger.debug(f"Help command: data = {data}")
		channel_id = data.get('channel_id')
	except (AttributeError, KeyError) as e:
		logger.error(f"Error when retrieving form data. {e}")
		return make_response(f"Failed to retrieve form data"), 400

	message = commander.commands_help()
	response_status = post_slack_message(channel_id, message)
	return response_status


@app.route("/matchups/", methods=['POST'])
def matchups():
	try:
		data = request.form
		logger.debug(f"Matchups command: data = {data}")
		channel_id = data.get('channel_id')
	except (AttributeError, KeyError) as e:
		logger.error(f"Error when retrieving form data. {e}")
		return make_response(f"Failed to retrieve form data"), 400

	try:
		message=commander.get_matchups()
	except KeyError as e:
		post_slack_message("Could not retrieve matchups. This could be due to the ESPN API failing to return season data.")
		return make_response(f"Failed to retrieve matchups. Error {e}", 404)
	response_status = post_slack_message(channel_id, message)
	return response_status

@app.route("/scores/", methods=['POST'])
def scores():
	try:
		data = request.form
		logger.debug(f"scores command: data = {data}")
		channel_id = data.get('channel_id')
	except (AttributeError, KeyError) as e:
		logger.error(f"Error when retrieving form data. {e}")
		return make_response(f"Failed to retrieve form data"), 400

	try:
		message=commander.get_scoreboard_short()
	except KeyError as e:
		post_slack_message("Could not retrieve scores. This could be due to the ESPN API failing to return season data.")
		return make_response(f"Failed to retrieve scores. Error {e}", 404)
	response_status = post_slack_message(channel_id, message)
	return response_status

@app.route("/final/", methods=['POST'])
def final():
	try:
		data = request.form
		logger.debug(f"Final command: data = {data}")
		channel_id = data.get('channel_id')
	except (AttributeError, KeyError) as e:
		logger.error(f"Error when retrieving form data. {e}")
		return make_response(f"Failed to retrieve form data"), 400

	try:
		message=commander.get_final()
	except KeyError as e:
		post_slack_message("Could not retrieve final scores. This could be due to the ESPN API failing to return season data.")
		return make_response(f"Failed to retrieve final scores. Error {e}", 404)
	response_status = post_slack_message(channel_id, message)
	return response_status


@app.route("/projections/", methods=['POST'])
def projections():
	try:
		data = request.form
		logger.debug(f"Projections command: data = {data}")
		channel_id = data.get('channel_id')
	except (AttributeError, KeyError) as e:
		logger.error(f"Error when retrieving form data. {e}")
		return make_response(f"Failed to retrieve form data"), 400

	try:
		message=commander.get_projected_scoreboard()
	except KeyError as e:
		post_slack_message("Could not retrieve projetions. This could be due to the ESPN API failing to return season data.")
		return make_response(f"Failed to retrieve projections. Error {e}", 404)
	response_status = post_slack_message(channel_id, message)
	return response_status

@app.route("/standings/", methods=["POST"])
def standings():
	try:
		data = request.form
		logger.debug(f"Projections command: data = {data}")
		channel_id = data.get('channel_id')
	except (AttributeError, KeyError) as e:
		logger.error(f"Error when retrieving form data. {e}")
		return make_response(f"Failed to retrieve form data"), 400

	try:
		message=commander.get_standings()
	except KeyError as e:
		post_slack_message("Could not retrieve standings. This could be due to the ESPN API failing to return season data.")
		return make_response(f"Failed to retrieve standings. Error {e}", 404)
	
	response_status = post_slack_message(channel_id, message)
	return response_status


def post_slack_message(channel, message):
	try:
		slack_client.chat_postMessage(channel=channel, text=message)
	except SlackApiError as e:
		err_code = e.response["error"]
		return make_response(f"Failed to post message due to {err_code}", 500)

	return make_response(""), 200

if __name__ == '__main__':
	# Run the flask app if this script is called directly
	app.run(host="0.0.0.0", debug=True)



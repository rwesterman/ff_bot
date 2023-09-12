import os
import logging

from espn_api.football import League

# from utils.bots import GroupMeBot, SlackBot, DiscordBot
from utils.commands import Commands
import discord
from discord.ext import commands


# Set up logging here
logger = logging.getLogger("discord_bot")
logging.basicConfig(level=logging.DEBUG)


def initialize_bot():
    """Initialize a chatbot using the required Environmental Variables
    Return each bot (slack, discord, groupme) and the Leauge object
    """

    league_id = int(os.getenv("LEAGUE_ID", "1"))
    year = int(os.getenv("LEAGUE_YEAR", 2023))
    swid = os.getenv("SWID", "{1}")

    if swid.find("{", 0) == -1:
        swid = "{" + swid
    if swid.find("}", -1) == -1:
        swid = swid + "}"

    espn_s2 = os.getenv("ESPN_S2", "1")

    # For Discord Bot
    intents = discord.Intents.default()
    intents.message_content = True
    discord_bot = commands.Bot(command_prefix='/', intents=intents)

    if swid == '{1}' and espn_s2 == '1':
        league = League(league_id, year)
    else:
        league = League(league_id, year, espn_s2, swid)

    return {"bot": discord_bot, "league": league}

# Initialize an instance of the chatbot and create a Commands instance for the bot
init_dict = initialize_bot()
bot = init_dict["bot"]
league = init_dict["league"]
commander = Commands(league)

# @app.route('/FF', methods=['POST'])
# def ff_webhook():
#     """This webhook is called whenever a message is sent to the group chat.
#     The message is checked for a command, then the message text is sent to the Commands object so the
#     appropriate action can be taken."""
#     # data received at GroupMe callback URL
#     gm_data = request.get_json()
#     logger.debug("Received {}".format(gm_data))

#     # Don't respond to bots
#     if not "bot" in gm_data['name'].lower():
#         response = commander.parse(gm_data)
#         # Check to see that there's a message to send
#         if response:
#             commander.send_message(response)

#     # This prevents a ValueError raised by Flask
#     return "OK"

@bot.command(name="history", brief="Prints the last 10 messages in the channel")
async def print_history(context):
    messages = [message async for message in context.channel.history(limit=10)]
    await context.send("\n".join(messages))

# # https://ff-bot-groupme.herokuapp.com/help/
# @bot.command(name="help")
# async def help(context):
#     logger.debug(f"Help command")

#     help_msg = commander.commands_help()
#     msg = await context.send(help_msg)

@bot.command(name="matchups", brief="Sends the matchups for the current week")
async def matchups(context):
    try:
        matchups=commander.get_matchups()
        msg = await context.send(matchups)
    except KeyError:
        msg = await context.send("Could not retrieve matchups. This could be due to the ESPN API failing to return season data.")

@bot.command(name="scores", brief="Sends the scores for the current week")
async def scores(context):
    try:
        scores=commander.get_scoreboard_short()
        msg = await context.send(scores)
    except KeyError:
        msg = await context.send("Could not retrieve scores. This could be due to the ESPN API failing to return season data.")

@bot.command("final", brief="Final scores for the previous week")
async def final(context):
    try:
        final_scores=commander.get_final()
        msg = await context.send(final_scores)
    except KeyError:
        msg = await context.send(f"Could not retrieve final scores. This could be due to the ESPN API failing to return season data.")

@bot.command(name="projections", brief="Projected scores for the current week")
async def projections(context):
    try:
        projections=commander.get_projected_scoreboard()
        msg = await context.send(projections)
    except KeyError:
        msg = context.send(f"Could not retrieve projections. This could be due to the ESPN API failing to return season data.")

@bot.command(name="standings", brief="Current league standings with top-half scoring wins added.")
async def standings(context, *, cmd_text: str):
    logger.debug(f"Standings command: cmd_text = {cmd_text}")

    try:
        msg = await context.send("Calculating standings...")
        standings=commander.get_standings()
        await msg.edit(content=standings)
    except KeyError as e:
        msg = await context.send(f"Could not retrieve standings. This could be due to the ESPN API failing to return season data.")


bot_token = os.getenv("DISCORD_BOT_TOKEN", None)
bot.run(bot_token)



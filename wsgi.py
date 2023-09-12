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

@bot.command(name="mock", brief="Mock the previous message.")
async def mock(context):
    messages = [message.content async for message in context.channel.history(limit=10) if not message.author.bot]
    if messages:
        # Mock the previous message. Index 0 will contain the command itself, so we want the next message.
        mocking = commander.mock_user(messages[1])
        msg = await context.send(mocking)

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



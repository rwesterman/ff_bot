import os
import logging

from espn_api.football import League

# from utils.bots import GroupMeBot, SlackBot, DiscordBot
from utils.commands import Commands
from utils.transaction import Transaction
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

@bot.command(name="waivers", brief="Show recent waiver activity.")
async def waviers(context):
    msg = await context.send("Pulling recent waiver activity...")
    recent_activity = commander.get_recent_activity()
    formatted_activity = []
    for activity in recent_activity:
        transaction = Transaction(activity.actions)
        formatted_activity.append(transaction.build_message())

    await msg.edit(content="\n".join(formatted_activity))

@bot.command(name="mock", brief="Mock the previous message.")
async def mock(context):
    messages = [message async for message in context.channel.history(limit=10) if not message.author.bot]
    if messages:
        # Mock the previous message. Index 0 will contain the command itself, so we want the next message.
        mocked_message = messages[1]
        mock_text = commander.mock_user(mocked_message.content)
        msg = await context.send(mock_text, reference=mocked_message)

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
async def standings(context):
    try:
        msg = await context.send("Calculating standings...")
        standings=commander.get_standings()
        await msg.edit(content=standings)
    except KeyError:
        msg = await context.send(f"Could not retrieve standings. This could be due to the ESPN API failing to return season data.")


bot_token = os.getenv("DISCORD_BOT_TOKEN", None)
bot.run(bot_token)



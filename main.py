import os
import logging
from tabulate import tabulate
from urllib3.exceptions import HTTPError

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
    discord_bot = commands.Bot(command_prefix="/", intents=intents)
    discord_client = discord.Client(intents=intents)

    if swid == "{1}" and espn_s2 == "1":
        league = League(league_id, year)
    else:
        league = League(league_id, year, espn_s2, swid)

    return {"bot": discord_bot, "league": league, "client": discord_client}


# Initialize an instance of the chatbot and create a Commands instance for the bot
init_dict = initialize_bot()
bot = init_dict["bot"]
# client = init_dict["client"]
league = init_dict["league"]
commander = Commands(league)


# TODO: Add an option to specify how many transactions to show
# Will need to break them into N at a time
@bot.command(name="waivers", brief="Show recent waiver activity.")
async def waivers(ctx):
    sort_by_bid = True
    activity_size = 10
    recent_activity = commander.get_recent_activity(size=activity_size)
    await ctx.send("Pulling recent waiver activity...")
    for i in range(0, activity_size, 10):
        formatted_activity = []
        for activity in recent_activity[i : i + 10]:
            transaction = Transaction(activity.actions)
            formatted_activity.append(transaction.build_message_tabulate())
        if sort_by_bid:
            formatted_activity = sorted(formatted_activity, key=lambda x: x[3], reverse=True)
        waiver_message = tabulate(formatted_activity, headers=["Team", "Added", "Dropped", "Bid"], tablefmt="github")
        await ctx.send(f" \n```{waiver_message}```")


# TODO: Add an option to specify how many transactions to show
@waivers.error
async def waivers_error(ctx, error):
    logger.error(f"Error with waivers\n{error}")
    if isinstance(error, HTTPError):
        await ctx.send("Sorry, the message output was too long.")


@bot.command(name="mock", brief="Mock the previous message.")
async def mock(ctx):
    messages = [message async for message in ctx.channel.history(limit=10) if not message.author.bot]
    if messages:
        # Mock the previous message. Index 0 will contain the command itself, so we want the next message.
        mocked_message = messages[1]
        mock_text = commander.mock_user(mocked_message.content)
        await ctx.send(mock_text, reference=mocked_message)


@bot.command(name="matchups", brief="Sends the matchups for the current week")
async def matchups(ctx):
    try:
        matchups = commander.get_matchups()
        await ctx.send(matchups)
    except KeyError:
        await ctx.send("Could not retrieve matchups. This could be due to the ESPN API failing to return season data.")


@bot.command(name="scores", brief="Sends the scores for the current week")
async def scores(ctx):
    try:
        scores = commander.get_scoreboard_short()
        await ctx.send(scores)
    except KeyError:
        await ctx.send("Could not retrieve scores. This could be due to the ESPN API failing to return season data.")


@bot.command(name="final", brief="Final scores for the previous week")
async def final(ctx):
    try:
        final_scores = commander.get_final()
        await ctx.send(final_scores)
    except KeyError:
        await ctx.send(
            "Could not retrieve final scores. This could be due to the ESPN API failing to return season data."
        )


@bot.command(name="projections", brief="Projected scores for the current week")
async def projections(ctx):
    try:
        projections = commander.get_projected_scoreboard()
        await ctx.send(projections)
    except KeyError:
        await ctx.send(
            "Could not retrieve projections. This could be due to the ESPN API failing to return season data."
        )


@bot.command(name="standings", brief="Current league standings with top-half scoring wins added.")
async def standings(ctx):
    try:
        msg = await ctx.send("Calculating standings...")
        standings = commander.get_standings()
        await msg.edit(content=standings)
    except KeyError:
        msg = await ctx.send(
            "Could not retrieve standings. This could be due to the ESPN API failing to return season data."
        )


bot_token = os.getenv("DISCORD_BOT_TOKEN", None)
bot.run(bot_token)

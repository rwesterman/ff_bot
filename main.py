import asyncio
import io
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from espn_api.football import League
from tabulate import tabulate
from urllib3.exceptions import HTTPError

from utils.chat_rag import HistoryRagService, format_discord_answer, split_discord_message
from utils.commands import Commands
from utils.penalties import PenaltyMonitor, PenaltyStore, deliver_pending
from utils.contest import ContestService
from utils.rules import RulesService, temporary_pdf_path, write_rules_pdf
from utils.transaction import Transaction


logger = logging.getLogger("discord_bot")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


def initialize_league():
    league_id = int(os.getenv("LEAGUE_ID", "1"))
    year = int(os.getenv("LEAGUE_YEAR", "2023"))
    swid = os.getenv("SWID", "{1}")

    if "{" not in swid:
        swid = "{" + swid
    if "}" not in swid:
        swid = swid + "}"

    espn_s2 = os.getenv("ESPN_S2", "1")
    if swid == "{1}" and espn_s2 == "1":
        return League(league_id, year)
    return League(league_id, year, espn_s2, swid)


def initialize_bot():
    intents = discord.Intents.default()
    intents.message_content = True
    return commands.Bot(command_prefix="/", intents=intents)


def register_league_commands(bot, commander):
    @bot.command(name="penalties", brief="List recorded penalty bonuses for a week.")
    async def penalties(ctx, week: int):
        if not 1 <= week <= 18:
            await ctx.send("Week must be between 1 and 18. Usage: `/penalties <week>`")
            return
        try:
            pages = await asyncio.to_thread(commander.get_penalties, week)
            for page in pages:
                await ctx.send(page, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("Could not retrieve logged penalty bonuses")
            await ctx.send("I could not read the penalty bonus database. Please try again later.")

    @penalties.error
    async def penalties_error(ctx, error):
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send("Usage: `/penalties <week>` with a week number from 1 to 18, for example `/penalties 1`.")
        else:
            logger.error("Penalty command failed: %s", error)
            await ctx.send("I could not show the logged penalties. Please try again later.")

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
                formatted_activity = sorted(formatted_activity, key=lambda row: row[3], reverse=True)
            waiver_message = tabulate(
                formatted_activity, headers=["Team", "Added", "Dropped", "Bid"], tablefmt="github"
            )
            await ctx.send(f" \n```{waiver_message}```")

    @waivers.error
    async def waivers_error(ctx, error):
        logger.error("Error with waivers\n%s", error)
        if isinstance(error, HTTPError):
            await ctx.send("Sorry, the message output was too long.")

    @bot.command(name="mock", brief="Mock the previous message.")
    async def mock(ctx):
        messages = [message async for message in ctx.channel.history(limit=10) if not message.author.bot]
        if len(messages) > 1:
            mocked_message = messages[1]
            mock_text = commander.mock_user(mocked_message.content)
            await ctx.send(mock_text, reference=mocked_message)

    @bot.command(name="matchups", brief="Sends the matchups for the current week")
    async def matchups(ctx):
        try:
            await ctx.send(commander.get_matchups())
        except KeyError:
            await ctx.send(
                "Could not retrieve matchups. This could be due to the ESPN API failing to return season data."
            )

    @bot.command(name="scores", brief="Sends the scores for the current week")
    async def scores(ctx):
        try:
            await ctx.send(commander.get_scoreboard_short())
        except KeyError:
            await ctx.send(
                "Could not retrieve scores. This could be due to the ESPN API failing to return season data."
            )

    @bot.command(name="final", brief="Final scores for the previous week")
    async def final(ctx):
        try:
            await ctx.send(commander.get_final())
        except KeyError:
            await ctx.send(
                "Could not retrieve final scores. This could be due to the ESPN API failing to return season data."
            )

    @bot.command(name="projections", brief="Projected scores for the current week")
    async def projections(ctx):
        try:
            await ctx.send(commander.get_projected_scoreboard())
        except KeyError:
            await ctx.send(
                "Could not retrieve projections. This could be due to the ESPN API failing to return season data."
            )

    @bot.command(name="standings", brief="Current league standings with top-half scoring wins added.")
    async def standings(ctx):
        try:
            message = await ctx.send("Calculating standings...")
            await message.edit(content=commander.get_standings())
        except KeyError:
            await ctx.send(
                "Could not retrieve standings. This could be due to the ESPN API failing to return season data."
            )


def register_rag_commands(bot, rag_service):
    @bot.command(name="ask", brief="Ask a question about league chat history.")
    @commands.cooldown(rate=1, per=30, type=commands.BucketType.user)
    async def ask(ctx, *, question: str):
        if ctx.guild is None:
            await ctx.send("Chat-history questions are only available inside the league server.")
            return
        status = await ctx.send("Refreshing chat history and searching...")
        stale = False
        try:
            refresh_result = await rag_service.refresh(bot)
            if refresh_result.unavailable_channel_ids:
                logger.warning(
                    "RAG refresh could not access %d configured channels", len(refresh_result.unavailable_channel_ids)
                )
        except Exception:
            logger.exception("Chat history refresh failed")
            if not rag_service.has_index():
                await status.edit(
                    content="I could not refresh or search the chat-history index. Please try again later."
                )
                return
            stale = True

        try:
            answer = await rag_service.answer(question)
            parts = split_discord_message(format_discord_answer(answer, stale=stale))
            await status.edit(content=parts[0])
            for part in parts[1:]:
                await ctx.send(part)
        except Exception:
            logger.exception("Chat history question failed")
            await status.edit(content="I could not answer that from the chat history. Please try again later.")

    @ask.error
    async def ask_error(ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `/ask <question>`")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Please wait {error.retry_after:.0f} seconds before asking another question.")
        else:
            logger.error("Error with ask command: %s", error)


def register_rules_command(bot, rules_service):
    @bot.command(name="rules", brief="Answer a question using the current league rules.")
    @commands.cooldown(rate=1, per=15, type=commands.BucketType.user)
    async def rules(ctx, *, question: str):
        if ctx.guild is None:
            await ctx.send("League-rule questions are only available inside the league server.")
            return
        try:
            async with ctx.typing():
                answer = await rules_service.answer(question)
                result = answer.sources
                if not result.matches:
                    await ctx.send("I could not find a relevant section in the current league rules.")
                    return
                filename = f"league-rules-{result.commit_sha[:7]}.pdf"
                with temporary_pdf_path() as attachment_path:
                    await asyncio.to_thread(write_rules_pdf, attachment_path, question, result)
                    response = (
                        f"{answer.text}\n\n_Source: attached excerpts from rules revision `{result.commit_sha[:12]}`._"
                    )
                    parts = split_discord_message(response)
                    await ctx.send(
                        parts[0],
                        file=discord.File(attachment_path, filename=filename),
                    )
                    for part in parts[1:]:
                        await ctx.send(part)
        except Exception:
            logger.exception("League rules lookup failed")
            await ctx.send("I could not answer from the latest league rules. Please try again later.")

    @rules.error
    async def rules_error(ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `/rules <question>`")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"Please wait {error.retry_after:.0f} seconds before asking another rules question.")
        else:
            logger.error("Error with rules command: %s", error)


def register_contest_commands(bot, contest_service):
    async def deny_non_admin(ctx):
        await ctx.send("Only the league treasurer can change the payout ledger.")
        logger.info("Rejected contest write from %s", ctx.author.id)

    @bot.group(name="weeklycontest", invoke_without_command=True, brief="Weekly payouts and season totals.")
    async def weeklycontest(ctx, week: int | None = None):
        target = week or contest_service.default_week()
        await ctx.send(await asyncio.to_thread(contest_service.report, target))

    @weeklycontest.command(name="settle", brief="Score a week and record its payouts.")
    async def settle(ctx, week: int | None = None):
        if not contest_service.is_admin(ctx.author.id):
            await deny_non_admin(ctx)
            return
        target = week or contest_service.default_week()
        status = await ctx.send(f"Scoring week {target}...")
        try:
            await status.edit(content=await asyncio.to_thread(contest_service.settle, target, ctx.author.id))
        except Exception:
            logger.exception("Failed to settle contest week %s", target)
            await status.edit(content=f"I could not score week {target}. ESPN may be unavailable right now.")

    @weeklycontest.command(name="totals", brief="Season payout totals.")
    async def totals(ctx):
        await ctx.send(await asyncio.to_thread(contest_service.season))

    @weeklycontest.command(name="penalty", brief="Log taunting/unsportsmanlike penalties for week 13.")
    async def penalty(ctx, week: int, team: str, count: int, *, player: str):
        if not contest_service.is_admin(ctx.author.id):
            await deny_non_admin(ctx)
            return
        await ctx.send(await asyncio.to_thread(contest_service.log_penalty, week, team, player, count))

    @weeklycontest.command(name="export", brief="Download the payout ledger as CSV.")
    async def export(ctx):
        payload = await asyncio.to_thread(contest_service.export_csv)
        attachment = discord.File(io.BytesIO(payload.encode()), filename=f"payouts-{contest_service.league_year}.csv")
        await ctx.send("Full payout ledger attached.", file=attachment)

    @weeklycontest.error
    @settle.error
    @penalty.error
    async def contest_error(ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `/weeklycontest penalty <week> <team> <count> <player>`")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("That does not look like a week number. Try `/weeklycontest 5`.")
        else:
            logger.error("Error with weeklycontest command: %s", error)


def configure_history_refresh(bot, rag_service):
    interval_seconds = float(os.getenv("RAG_SYNC_INTERVAL_SECONDS", "3600"))

    @tasks.loop(seconds=interval_seconds)
    async def refresh_chat_history():
        try:
            result = await rag_service.refresh(bot)
            logger.info(
                "Chat history refreshed: messages=%d chunks=%d new_embeddings=%d",
                result.messages_synced,
                result.index.chunks,
                result.index.embeddings_created,
            )
            if result.unavailable_channel_ids:
                logger.warning("Could not access %d configured RAG channels", len(result.unavailable_channel_ids))
        except Exception:
            logger.exception("Scheduled chat history refresh failed")

    @refresh_chat_history.before_loop
    async def before_refresh_chat_history():
        await bot.wait_until_ready()

    @bot.event
    async def on_ready():
        logger.info("Connected to Discord as %s", bot.user)
        if not refresh_chat_history.is_running():
            refresh_chat_history.start()

    bot.history_refresh_loop = refresh_chat_history


def configure_penalty_polling(bot, commander, store, channel_id):
    monitor = PenaltyMonitor(commander, store)

    @tasks.loop(minutes=10)
    async def poll_penalties():
        try:
            await asyncio.to_thread(monitor.poll)
        except Exception:
            logger.exception("Penalty polling failed; will retry in ten minutes")
        # A source outage must not prevent delivery of already recorded bonuses.
        try:

            async def send(text):
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                message = await channel.send(
                    text,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return message.id

            await deliver_pending(store, commander.league.current_week, send)
        except Exception:
            logger.exception("Penalty announcement failed; pending messages will be retried")

    @poll_penalties.before_loop
    async def before_poll_penalties():
        await bot.wait_until_ready()

    @bot.listen("on_ready")
    async def start_penalty_polling():
        if not poll_penalties.is_running():
            poll_penalties.start()

    bot.penalty_poll_loop = poll_penalties


def create_application():
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    bot = initialize_bot()
    league = initialize_league()
    chat_database_path = Path(os.getenv("CHAT_HISTORY_DB", "data/chat_history.db"))
    league_database_path = Path(os.getenv("LEAGUE_DB") or "data/league.db")
    penalty_store = PenaltyStore(league_database_path, league.league_id, league.year)
    commander = Commands(league, penalty_store=penalty_store)
    register_league_commands(bot, commander)
    channel_id = os.getenv("PENALTY_CHANNEL_ID", "").strip()
    if channel_id:
        configure_penalty_polling(bot, commander, penalty_store, int(channel_id))
    else:
        logger.warning("Penalty polling is disabled: set PENALTY_CHANNEL_ID")

    register_contest_commands(bot, ContestService.from_environment(league, league_database_path))
    try:
        rag_service = HistoryRagService.from_environment(chat_database_path)
    except RuntimeError as error:
        logger.warning("Chat-history Q&A is disabled: %s", error)
    else:
        register_rag_commands(bot, rag_service)
        configure_history_refresh(bot, rag_service)

    try:
        rules_service = RulesService.from_environment(chat_database_path)
    except RuntimeError as error:
        logger.warning("League-rules lookup is disabled: %s", error)
    else:
        register_rules_command(bot, rules_service)
    return bot


def main():
    bot = create_application()
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token:
        raise SystemExit("DISCORD_BOT_TOKEN is required")
    bot.run(bot_token)


if __name__ == "__main__":
    main()

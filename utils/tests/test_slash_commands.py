import ast
import asyncio
import io
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.ext import commands
import pytest

from utils.chat_rag import split_discord_message


@pytest.fixture
def command_definitions():
    # Exercise the real handlers without importing main.py or running its application initialization.
    path = Path(__file__).resolve().parents[2] / "main.py"
    source = ast.parse(path.read_text(), filename=str(path))
    names = {
        "FantasyBot",
        "initialize_bot",
        "register_help_command",
        "register_league_commands",
        "register_contest_commands",
        "register_rag_commands",
        "register_rules_command",
    }
    definitions = ast.Module(
        body=[node for node in source.body if getattr(node, "name", None) in names], type_ignores=[]
    )
    namespace = {
        "__name__": "isolated_discord_commands",
        "asyncio": asyncio,
        "commands": commands,
        "discord": discord,
        "io": io,
        "logger": logging.getLogger("discord_bot"),
        "split_discord_message": split_discord_message,
        "format_discord_answer": Mock(return_value="An answer from league history."),
    }
    exec(compile(definitions, str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


@pytest.fixture
def services():
    return SimpleNamespace(
        commander=Mock(),
        contests=Mock(
            report=Mock(return_value="Weekly contest results"),
            season=Mock(return_value="Season payout totals"),
            export_csv=Mock(return_value="team,cents\nTeam One,2000\n"),
            league_year=2026,
        ),
        rag=Mock(
            refresh=AsyncMock(return_value=SimpleNamespace(unavailable_channel_ids=())),
            answer=AsyncMock(return_value=object()),
        ),
        rules=Mock(answer=AsyncMock(return_value=SimpleNamespace(sources=SimpleNamespace(matches=[])))),
    )


@pytest.fixture
def bot(command_definitions, services):
    bot = command_definitions.initialize_bot()
    command_definitions.register_league_commands(bot, services.commander)
    command_definitions.register_contest_commands(bot, services.contests)
    command_definitions.register_rag_commands(bot, services.rag)
    command_definitions.register_rules_command(bot, services.rules)
    bot._connection.user = discord.ClientUser(
        state=bot._connection,
        data={"id": "123", "username": "Fantasy Bot", "discriminator": "0", "avatar": None, "bot": True},
    )
    # Any unmocked Discord HTTP request is a test failure.
    bot.http.request = AsyncMock(side_effect=AssertionError("Unexpected Discord HTTP request"))
    return bot


def interaction(bot, name, *, options=()):
    payload = {
        "id": str(discord.utils.time_snowflake(discord.utils.utcnow())),
        "application_id": "123",
        "type": discord.InteractionType.application_command.value,
        "token": "dummy-interaction-token",
        "version": 1,
        "attachment_size_limit": 8_388_608,
        "guild_id": "456",
        "channel": {"id": "789", "type": 0, "name": "league-chat", "position": 0, "permission_overwrites": []},
        "member": {
            "user": {"id": "42", "username": "League Member", "discriminator": "0", "avatar": None},
            "roles": [],
            "joined_at": "2026-01-01T00:00:00+00:00",
            "flags": 0,
        },
        "data": {"id": "111", "name": name, "type": 1, "options": list(options)},
    }
    result = discord.Interaction(data=payload, state=bot._connection)
    response = SimpleNamespace(
        is_done=Mock(return_value=False), send_message=AsyncMock(return_value=SimpleNamespace(resource=None))
    )

    async def defer(**kwargs):
        response.is_done.return_value = True

    response.defer = AsyncMock(side_effect=defer)
    result._cs_response = response
    result._original_response = SimpleNamespace(edit=AsyncMock())
    result._cs_followup = SimpleNamespace(send=AsyncMock(return_value=result._original_response))
    return result


def dispatch(bot, request):
    async def run():
        await bot._async_setup_hook()
        await bot.tree._call(request)
        assert not request.command_failed
        await bot.close()

    asyncio.run(run())


def test_public_commands_are_registered_with_descriptions_and_typed_options(bot):
    payload = {item.name: item.to_dict(bot.tree) for item in bot.tree.get_commands()}
    assert set(payload) == {
        "help",
        "penalties",
        "waivers",
        "mock",
        "matchups",
        "scores",
        "final",
        "projections",
        "standings",
        "weeklycontest",
        "weeklycontests",
        "ask",
        "rules",
    }
    for item in bot.tree.walk_commands():
        assert 1 <= len(item.description) <= 100
        assert item.description != "…"
    assert all(isinstance(command, (commands.HybridCommand, commands.HybridGroup)) for command in bot.walk_commands())
    assert payload["scores"]["options"] == []
    assert payload["weeklycontest"]["options"] == []
    assert [(option["name"], option["type"], option["required"]) for option in payload["penalties"]["options"]] == [
        ("week", discord.AppCommandOptionType.integer.value, True)
    ]
    for name in ("ask", "rules"):
        assert [(option["name"], option["type"], option["required"]) for option in payload[name]["options"]] == [
            ("question", discord.AppCommandOptionType.string.value, True)
        ]
    contests = bot.tree.get_command("weeklycontests")
    assert {command.name for command in contests.commands} == {"results", "totals"}
    assert contests.get_command("results").wrapped is bot.get_command("weeklycontests")
    assert bot.get_command("weeklycontest") is not bot.get_command("weeklycontests")
    assert bot.get_command("weeklycontests penalty") is None
    export = bot.get_command("weeklycontests export")
    assert export.hidden
    assert export.app_command is None


def test_startup_syncs_the_complete_global_tree(bot, caplog):
    bot._connection.application_id = 123
    payload = [command.to_dict(bot.tree) for command in bot.tree.get_commands()]
    remote_commands = [dict(item, id=index, application_id=123, version=1) for index, item in enumerate(payload, 1)]
    bot.http.bulk_upsert_global_commands = AsyncMock(return_value=remote_commands)

    async def start():
        await bot.setup_hook()
        await bot.close()

    with caplog.at_level(logging.INFO, logger="discord_bot"):
        asyncio.run(start())
    bot.http.bulk_upsert_global_commands.assert_awaited_once_with(123, payload=payload)
    assert "Registered 13 global slash commands with Discord" in caplog.text


def test_sync_failure_is_logged_and_stops_startup(bot, caplog):
    error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
    bot.tree.sync = AsyncMock(side_effect=error)
    with pytest.raises(discord.Forbidden), caplog.at_level(logging.ERROR, logger="discord_bot"):
        asyncio.run(bot.setup_hook())
    assert "Could not register slash commands with Discord" in caplog.text


@pytest.mark.parametrize(
    "name, method",
    [
        ("scores", "get_scoreboard_short"),
        ("matchups", "get_matchups"),
        ("final", "get_final"),
        ("projections", "get_projected_scoreboard"),
    ],
)
def test_league_slash_commands_defer_before_reading_espn_and_send_scores(bot, services, name, method):
    request = interaction(bot, name)
    text = "Score Update\nT1 100.00 - 95.00 T2"

    def read_scores():
        request.response.defer.assert_awaited_once()
        return text

    getter = getattr(services.commander, method)
    getter.side_effect = read_scores
    dispatch(bot, request)
    getter.assert_called_once_with()
    request.followup.send.assert_awaited_once()
    assert request.followup.send.await_args.kwargs["content"] == text


@pytest.mark.parametrize(
    "name, options",
    [("weeklycontest", []), ("weeklycontests", [{"name": "results", "type": 1}])],
)
def test_weekly_report_commands_run_report_and_respect_message_limits(bot, services, name, options):
    services.contests.report.return_value = "Result line\n" * 400
    request = interaction(bot, name, options=options)
    dispatch(bot, request)
    request.response.defer.assert_awaited_once()
    services.contests.report.assert_called_once_with()
    messages = [call.kwargs["content"] for call in request.followup.send.await_args_list]
    assert len(messages) > 1
    assert all(0 < len(message) <= 2000 for message in messages)


def test_contest_totals_dispatches_to_season_service(bot, services):
    request = interaction(bot, "weeklycontests", options=[{"name": "totals", "type": 1}])
    dispatch(bot, request)
    services.contests.season.assert_called_once_with()
    request.followup.send.assert_awaited_once()


@pytest.mark.parametrize(
    "content, method",
    [
        ("/weeklycontest", "report"),
        ("/weeklycontests", "report"),
        ("/weeklycontests export", "export_csv"),
    ],
)
def test_weekly_report_and_hidden_export_remain_callable_as_text_commands(bot, services, content, method):
    request = interaction(bot, "weeklycontest")
    message = discord.Message(
        state=bot._connection,
        channel=request.channel,
        data={
            "id": str(request.id),
            "content": content,
            "type": 0,
            "flags": 0,
            "reactions": [],
            "embeds": [],
            "mention_everyone": False,
            "tts": False,
            "pinned": False,
            "edited_timestamp": None,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
        },
    )
    message.author = request.user

    async def run():
        await bot._async_setup_hook()
        ctx = await bot.get_context(message)
        ctx.send = AsyncMock()
        await bot.invoke(ctx)
        assert not ctx.command_failed
        await bot.close()
        return ctx

    ctx = asyncio.run(run())
    getattr(services.contests, method).assert_called_once_with()
    ctx.send.assert_awaited_once()
    if method == "export_csv":
        assert ctx.command.qualified_name == "weeklycontests export"
        attachment = ctx.send.await_args.kwargs["file"]
        assert attachment.filename == "payouts-2026.csv"
        assert attachment.fp.getvalue() == b"team,cents\nTeam One,2000\n"


def test_ask_receives_multiword_question_and_retains_user_cooldown(bot, services):
    question = "Who won the league last year?"
    options = [{"name": "question", "type": 3, "value": question}]
    first = interaction(bot, "ask", options=options)
    second = interaction(bot, "ask", options=options)

    async def run():
        await bot._async_setup_hook()
        await bot.tree._call(first)
        await bot.tree._call(second)
        await bot.close()

    asyncio.run(run())
    assert not first.command_failed
    assert second.command_failed
    services.rag.answer.assert_awaited_once_with(question)
    status = first.followup.send.return_value
    status.edit.assert_awaited_once_with(content="An answer from league history.")
    assert "Please wait" in second.response.send_message.await_args.kwargs["content"]
    second.response.defer.assert_not_awaited()


def test_rules_receives_multiword_question_without_deferring_twice(bot, services):
    question = "What are the playoff tiebreakers?"
    request = interaction(bot, "rules", options=[{"name": "question", "type": 3, "value": question}])
    dispatch(bot, request)
    services.rules.answer.assert_awaited_once_with(question)
    request.response.defer.assert_awaited_once()
    assert request.followup.send.await_args.kwargs["content"] == (
        "I could not find a relevant section in the current league rules."
    )


def test_slash_help_lists_group_fallback_and_keeps_output_within_limit(bot):
    request = interaction(bot, "help")
    dispatch(bot, request)
    messages = [call.kwargs["content"] for call in request.followup.send.await_args_list]
    text = "\n".join(messages)
    assert "`/scores`" in text
    assert "`/weeklycontest`" in text
    assert "`/weeklycontests results`" in text
    assert "`/penalties <week>`" in text
    assert "/weeklycontests export" not in text
    assert "/weeklycontests penalty" not in text
    assert all(len(message) <= 2000 for message in messages)


def test_help_omits_unconfigured_optional_commands(bot):
    bot.remove_command("ask")
    bot.remove_command("rules")
    request = interaction(bot, "help")
    dispatch(bot, request)
    text = "\n".join(call.kwargs["content"] for call in request.followup.send.await_args_list)
    assert "`/ask" not in text
    assert "`/rules" not in text


@pytest.mark.parametrize("slash, expected_index", [(True, 0), (False, 1)])
def test_mock_selects_previous_message_for_slash_and_text_commands(bot, services, slash, expected_index):
    messages = [SimpleNamespace(content=f"message-{index}", author=SimpleNamespace(bot=False)) for index in range(2)]

    async def history(**kwargs):
        for message in messages:
            yield message

    ctx = SimpleNamespace(
        interaction=object() if slash else None,
        channel=SimpleNamespace(history=history),
        send=AsyncMock(),
    )
    services.commander.mock_user.return_value = "mOcKeD"
    asyncio.run(bot.get_command("mock").callback(ctx))
    services.commander.mock_user.assert_called_once_with(messages[expected_index].content)
    ctx.send.assert_awaited_once_with("mOcKeD", reference=messages[expected_index])


def test_mock_responds_when_there_is_no_previous_message(bot):
    async def history(**kwargs):
        for message in []:
            yield message

    ctx = SimpleNamespace(interaction=object(), channel=SimpleNamespace(history=history), send=AsyncMock())
    asyncio.run(bot.get_command("mock").callback(ctx))
    ctx.send.assert_awaited_once_with("There is no previous message to mock.")


def test_prefix_commands_are_not_deferred(bot):
    ctx = SimpleNamespace(interaction=None, defer=AsyncMock())
    asyncio.run(bot._before_invoke(ctx))
    ctx.defer.assert_not_awaited()

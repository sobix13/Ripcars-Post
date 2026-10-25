import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from bot import Wizard, create_bot, deliver, permissions
from core import API, DEFAULT, Store, full_text, message_parts, original, split_text, tweet_time, units, username


def tweet(id="100", text="hello", account="ripcarsio", created=None, **kwargs):
    return dict(id=id, text=text, author={"userName": account},
                createdAt=datetime.fromtimestamp(created or time.time(), timezone.utc).strftime(
                    "%a %b %d %H:%M:%S %z %Y"), **kwargs)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/test.sqlite3"
        self.store = Store(self.path)
        self.config = copy.deepcopy(DEFAULT)
        self.config.update(channel_id=123, roles=[12, 34])

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_baseline_and_settings_survive_restart(self):
        self.store.save(1, self.config)
        first = self.store.get(1)
        self.config["minutes"] = 30
        self.store.save(1, self.config)
        self.store.db.close()
        self.store = Store(self.path)
        second = self.store.get(1)
        self.assertEqual(first["baseline"], second["baseline"])
        self.assertEqual(second["config"]["roles"], [12, 34])
        self.assertEqual(second["config"]["minutes"], 30)

    def test_account_change_starts_from_now(self):
        with patch("core.time.time", return_value=100):
            self.store.save(1, self.config)
        self.config["username"] = "newaccount"
        with patch("core.time.time", return_value=200):
            self.store.save(1, self.config)
        self.assertEqual(self.store.get(1)["baseline"], 200)

    def test_failure_preserves_checkpoint(self):
        self.store.save(1, self.config)
        checked = self.store.get(1)["checked"]
        self.store.finish(1, checked + 100, "API unavailable")
        self.assertEqual(self.store.get(1)["checked"], checked)
        self.assertEqual(self.store.get(1)["error"], "API unavailable")

    def test_queue_deduplicates_and_tracks_partial_delivery(self):
        self.store.queue(1, self.config, tweet(text="x" * 5000))
        self.store.queue(1, self.config, tweet(text="changed"))
        row = self.store.pending(1)[0]
        self.store.progress(row, 1)
        self.assertEqual(len(self.store.pending(1)), 1)
        self.assertEqual(self.store.pending(1)[0]["sent"], 1)
        self.store.progress(row, len(json.loads(row["parts"])))
        self.assertEqual(self.store.pending(1), [])


class TextTests(unittest.TestCase):
    def test_account_input_does_not_inject_search_operators(self):
        self.assertEqual(username("https://x.com/Ripcarsio?s=11"), "ripcarsio")
        for value in ("ripcarsio OR from:someone", "x.com/ripcarsio", "@everyone!", ""):
            with self.assertRaises(ValueError):
                username(value)

    def test_only_thread_root_and_account(self):
        self.assertTrue(original(tweet(), "ripcarsio"))
        self.assertFalse(original(tweet(isReply=True), "ripcarsio"))
        self.assertFalse(original(tweet(inReplyToId="98"), "ripcarsio"))
        self.assertFalse(original(tweet(retweeted_tweet={"id": "2"}), "ripcarsio"))
        self.assertFalse(original(tweet(account="someone"), "ripcarsio"))

    def test_unicode_and_whitespace_are_preserved(self):
        value = ("hello\n فارسی 😀 " * 1000) + "\n\n"
        pieces = split_text(value)
        self.assertEqual("".join(pieces), value)
        self.assertTrue(all(units(p) <= 2000 for p in pieces))

    def test_full_long_text_and_single_footer(self):
        config = copy.deepcopy(DEFAULT)
        config["roles"] = [12, 34]
        body = "😀" * 4000 + "\nend"
        post = tweet(text="short...", note_tweet={"note_tweet_results": {"result": {"text": body}}})
        self.assertEqual(full_text(post), body)
        pieces = message_parts(post, config)
        joined = "".join(pieces)
        self.assertTrue(joined.startswith(body))
        self.assertEqual(joined.count("https://x.com/ripcarsio/status/100"), 1)
        self.assertTrue(pieces[-1].endswith("<@&12> <@&34>"))
        self.assertTrue(all(units(p) <= 2000 for p in pieces))

    def test_timezones_are_not_discarded(self):
        self.assertEqual(tweet_time({"createdAt": "2026-01-01T03:30:00+03:30"}),
                         tweet_time({"createdAt": "2026-01-01T00:00:00Z"}))


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name + "/test.sqlite3")
        self.config = copy.deepcopy(DEFAULT)
        self.config.update(channel_id=123, roles=[12, 34])

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def test_paginated_search_oldest_first_with_provider_filters(self):
        api = API(None, "not-a-real-key")
        api.get = AsyncMock(side_effect=[
            {"tweets": [tweet("103"), tweet("102", isReply=True)],
             "has_next_page": True, "next_cursor": "next"},
            {"tweets": [tweet("101"), tweet("103"), tweet("104", account="other")],
             "has_next_page": False}])
        results = await api.search("ripcarsio", 100, 200)
        self.assertEqual([t["id"] for t in results], ["101", "103"])
        params = api.get.call_args.args[1]
        self.assertIn("since_time:100 until_time:200", params["query"])
        self.assertEqual(params["cursor"], "next")

    async def test_broken_cursor_is_failure(self):
        api = API(None, "not-a-real-key")
        api.get = AsyncMock(return_value={"tweets": [], "has_next_page": True, "next_cursor": "same"})
        with self.assertRaisesRegex(RuntimeError, "pagination"):
            await api.search("ripcarsio", 100, 200)

    async def test_send_timeout_reconciles_history_after_restart(self):
        self.store.queue(1, self.config, tweet())
        row = self.store.pending(1)[0]
        delivered = []

        async def send(text, **kwargs):
            delivered.append(SimpleNamespace(id=len(delivered) + 100,
                content=text, author=SimpleNamespace(id=99)))
            # Discord received it, but the response never reached our process.
            raise TimeoutError()

        async def history(**kwargs):
            for message in delivered:
                if "after" not in kwargs or message.id > kwargs["after"].id:
                    yield message

        channel = SimpleNamespace(send=send, history=history)
        with self.assertRaises(TimeoutError):
            await deliver(self.store, row, channel, 99)
        await deliver(self.store, self.store.pending(1)[0], channel, 99)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.store.pending(1), [])

    async def test_only_selected_roles_ping_on_final_part(self):
        self.store.queue(1, self.config, tweet(text="@everyone <@12345> " + "x" * 5000))
        async def history(**kwargs):
            for message in []:
                yield message
        channel = SimpleNamespace(send=AsyncMock(), history=history)
        await deliver(self.store, self.store.pending(1)[0], channel, 99)
        calls = channel.send.call_args_list
        for call in calls[:-1]:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict()["parse"], [])
        last = calls[-1].kwargs["allowed_mentions"].to_dict()
        self.assertEqual(last["roles"], [12, 34])
        self.assertEqual(last["parse"], [])

    async def test_repeated_chunks_do_not_false_match_previous_part(self):
        self.store.queue(1, self.config, tweet(text="x" * 4000))
        messages = []
        calls = 0

        async def send(text, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError()  # second part was NOT accepted
            messages.append(SimpleNamespace(id=len(messages) + 100,
                content=text, author=SimpleNamespace(id=99)))

        async def history(**kwargs):
            candidates = list(reversed(messages))
            if kwargs.get("limit"):
                candidates = candidates[:kwargs["limit"]]
            for message in candidates:
                if "after" not in kwargs or message.id > kwargs["after"].id:
                    yield message

        channel = SimpleNamespace(send=send, history=history)
        with self.assertRaises(TimeoutError):
            await deliver(self.store, self.store.pending(1)[0], channel, 99)
        await deliver(self.store, self.store.pending(1)[0], channel, 99)
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0].content, messages[1].content)

    async def test_poll_does_not_advance_checkpoint_on_discord_failure(self):
        self.store.save(1, self.config)
        row = self.store.get(1)
        bot = create_bot(self.store)
        bot._connection.user = SimpleNamespace(id=99)
        bot.api = SimpleNamespace(search=AsyncMock(return_value=[tweet(created=row["baseline"] + 20)]),
                                  detail=AsyncMock(side_effect=lambda t: t))
        channel = SimpleNamespace(guild=SimpleNamespace(get_role=lambda id: SimpleNamespace(
            mentionable=True, is_default=lambda: False)))
        bot.text_channel = AsyncMock(return_value=channel)
        bot.drain = AsyncMock(side_effect=[None, TimeoutError()])
        with patch("bot.permissions"):
            with self.assertRaises(TimeoutError):
                await bot.check(row)
        self.assertEqual(self.store.get(1)["checked"], row["checked"])
        self.assertEqual(len(self.store.pending(1)), 1)
        await bot.close()

    async def test_wizard_is_multi_role_and_manager_only(self):
        bot = create_bot(self.store)
        guild = SimpleNamespace(id=1, get_channel=lambda id: None, get_role=lambda id: None)
        interaction = SimpleNamespace(guild=guild, user=SimpleNamespace(id=7))
        wizard = Wizard(bot, interaction)
        picker = next(c for c in wizard.children if isinstance(c, discord.ui.RoleSelect))
        self.assertEqual(picker.max_values, 25)
        self.assertEqual(picker.min_values, 0)
        response = SimpleNamespace(send_message=AsyncMock())
        unauthorized = SimpleNamespace(user=SimpleNamespace(id=8), response=response)
        self.assertFalse(await wizard.interaction_check(unauthorized))
        self.assertTrue(response.send_message.called)
        await bot.close()

    async def test_permission_validation_rejects_everyone(self):
        channel = SimpleNamespace(guild=SimpleNamespace(me=None), permissions_for=lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True, mention_everyone=True))
        with self.assertRaisesRegex(ValueError, "@everyone"):
            permissions(channel, [SimpleNamespace(is_default=lambda: True)])


if __name__ == "__main__":
    unittest.main()

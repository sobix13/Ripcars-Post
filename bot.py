"""Ripcars Post: a Discord setup wizard and a 15-minute X feed."""
import asyncio
import copy
import json
import logging
import os
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from core import API, DEFAULT, Store, original, tweet_time, username

log = logging.getLogger("ripcars-post")


def permissions(channel, roles):
    perms = channel.permissions_for(channel.guild.me)
    needed = ("view_channel", "send_messages", "read_message_history")
    missing = [name for name in needed if not getattr(perms, name)]
    if missing:
        raise ValueError("Missing channel permissions: " + ", ".join(missing))
    for role in roles:
        if role.is_default():
            raise ValueError("Choose specific roles, not @everyone.")
        if not role.mentionable and not perms.mention_everyone:
            raise ValueError(f"Make the role '{role.name}' mentionable, or grant the bot "
                             "Mention Everyone in the destination channel.")


async def deliver(store, row, channel, bot_id):
    parts = json.loads(row["parts"])
    for index in range(row["sent"], len(parts)):
        # Reconcile an uncertain send after a timeout or a process restart.
        recovered = False
        if row["attempt"] is not None:
            after = discord.Object(id=int(row["attempt"]))
            async for message in channel.history(limit=None, after=after):
                if message.author.id == bot_id and message.content == parts[index]:
                    recovered = True
                    break
        if not recovered:
            # Use a Discord message boundary, not the local clock. Identical
            # adjacent text chunks must not be mistaken for this pending part.
            boundary = "0"
            async for previous in channel.history(limit=1):
                boundary = str(previous.id)
            store.progress(row, index, boundary)
            allowed = json.loads(row["roles"]) if index == len(parts) - 1 else []
            await channel.send(parts[index], suppress_embeds=True, allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, roles=[discord.Object(id=r) for r in allowed],
                replied_user=False))
        store.progress(row, index + 1)
        row = dict(row)
        row["attempt"] = None


class FeedBot(discord.Client):
    def __init__(self, store):
        super().__init__(intents=discord.Intents(guilds=True),
                         allowed_mentions=discord.AllowedMentions.none())
        self.store = store
        self.tree = app_commands.CommandTree(self)
        self.locks = {}

    def lock(self, guild):
        return self.locks.setdefault(guild, asyncio.Lock())

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        self.api = API(self.session, os.environ["TWITTERAPI_KEY"])
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.poll.start()

    async def close(self):
        self.poll.cancel()
        if hasattr(self, "session"):
            await self.session.close()
        await super().close()

    async def on_ready(self):
        log.info("Connected as %s. Feed configuration is available through /setup.", self.user)

    async def text_channel(self, channel_id):
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("The destination must be a server text channel.")
        return channel

    async def drain(self, guild):
        for row in self.store.pending(guild):
            channel = await self.text_channel(row["channel"])
            role_ids = json.loads(row["roles"])
            roles = [channel.guild.get_role(r) for r in role_ids]
            if any(r is None for r in roles):
                raise ValueError("A selected role was deleted. Restore it before retrying.")
            permissions(channel, roles)
            await deliver(self.store, row, channel, self.user.id)

    async def check(self, row):
        guild, config = row["guild"], row["config"]
        await self.drain(guild)
        channel = await self.text_channel(config["channel_id"])
        roles = [channel.guild.get_role(r) for r in config["roles"]]
        if any(r is None for r in roles):
            raise ValueError("A selected role was deleted. Update /setup.")
        permissions(channel, roles)
        until = int(time.time()) - 60
        since = max(row["baseline"], row["checked"] - 900)
        tweets = await self.api.search(config["username"], since, until)
        for tweet in tweets:
            if tweet_time(tweet) < row["baseline"]:
                continue
            if self.store.post(guild, config["username"], tweet["id"]):
                continue
            detail = await self.api.detail(tweet)
            if not original(detail, config["username"]):
                raise ValueError("Post detail did not match the configured account.")
            self.store.queue(guild, config, detail)
        await self.drain(guild)
        self.store.finish(guild, until)

    @tasks.loop(seconds=15)
    async def poll(self):
        for row in self.store.due():
            async with self.lock(row["guild"]):
                row = self.store.get(row["guild"])
                if row["next_check"] > time.time():
                    continue
                try:
                    await self.check(row)
                except Exception as error:
                    # Only known, credential-free errors are shown in status.
                    message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
                    log.error("Feed %s failed: %s", row["guild"], message)
                    self.store.finish(row["guild"], row["checked"], message)

    @poll.before_loop
    async def before_poll(self):
        await self.wait_until_ready()


class Settings(discord.ui.Modal, title="Feed settings"):
    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard
        config = wizard.config
        self.account = discord.ui.TextInput(label="X username or profile URL",
                                            default=config["username"], max_length=100)
        self.minutes = discord.ui.TextInput(label="Check every N minutes (15 to 1440)",
                                            default=str(config["minutes"]), max_length=4)
        self.footer = discord.ui.TextInput(label="Text above the original post link",
                                           default=config["footer"], max_length=200)
        for item in (self.account, self.minutes, self.footer):
            self.add_item(item)

    async def on_submit(self, interaction):
        if not await self.wizard.interaction_check(interaction):
            return
        try:
            account = username(self.account.value)
            minutes = int(self.minutes.value)
            if not 15 <= minutes <= 1440:
                raise ValueError("Choose an interval between 15 and 1440 minutes.")
            footer = self.footer.value.strip()
            if not footer:
                raise ValueError("Enter the text above the link.")
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        self.wizard.config.update(username=account, minutes=minutes, footer=footer)
        await interaction.response.edit_message(content=self.wizard.summary(), view=self.wizard)


class Wizard(discord.ui.View):
    def __init__(self, bot, interaction):
        super().__init__(timeout=600)
        self.bot, self.owner = bot, interaction.user.id
        self.guild = interaction.guild
        row = bot.store.get(self.guild.id)
        self.config = copy.deepcopy(row["config"] if row else DEFAULT)
        channel = self.guild.get_channel(self.config["channel_id"])
        channels = discord.ui.ChannelSelect(placeholder="Choose the destination channel",
                    channel_types=[discord.ChannelType.text], row=0,
                    default_values=[channel] if channel else [])
        channels.callback = self.choose_channel
        self.add_item(channels)
        roles = [self.guild.get_role(r) for r in self.config["roles"]]
        picker = discord.ui.RoleSelect(placeholder="Choose notification roles (zero to 25)",
                    min_values=0, max_values=25, row=1,
                    default_values=[r for r in roles if r])
        picker.callback = self.choose_roles
        self.add_item(picker)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only the server manager who opened this wizard "
                                                      "may edit it.", ephemeral=True)
            return False
        return True

    def summary(self):
        config = self.config
        roles = ", ".join(f"<@&{r}>" for r in config["roles"]) or "None"
        channel = f"<#{config['channel_id']}>" if config["channel_id"] else "Not selected"
        return (f"Ripcars Post setup\nAccount: @{config['username']}\nChannel: {channel}"
                f"\nNotification roles: {roles}\nInterval: {config['minutes']} minutes"
                f"\nLink text: {config['footer']}\n\nChoose your settings, then press Save. "
                "Only new original posts are forwarded. Threads include the first post only.")

    async def choose_channel(self, interaction):
        selected = interaction.data["values"]
        self.config["channel_id"] = int(selected[0])
        await interaction.response.edit_message(content=self.summary(), view=self)

    async def choose_roles(self, interaction):
        self.config["roles"] = list(dict.fromkeys(int(r) for r in interaction.data.get("values", [])))
        await interaction.response.edit_message(content=self.summary(), view=self)

    @discord.ui.button(label="Account, interval and link text", row=2)
    async def settings(self, interaction, button):
        await interaction.response.send_modal(Settings(self))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await self.bot.text_channel(self.config["channel_id"])
            if channel.guild.id != self.guild.id:
                raise ValueError("Choose a channel in this server.")
            roles = [self.guild.get_role(r) for r in self.config["roles"]]
            if any(r is None for r in roles):
                raise ValueError("A selected role no longer exists.")
            permissions(channel, roles)
            async with self.bot.lock(self.guild.id):
                self.bot.store.save(self.guild.id, self.config)
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(content=self.summary().split("\n\n")[0]
                + "\n\nSaved. The next check runs after the configured interval. "
                  "Use /setup to change settings or /status to see delivery errors.", view=self)
            self.stop()
        except (ValueError, discord.HTTPException) as error:
            await interaction.followup.send(str(error), ephemeral=True)


def create_bot(store):
    bot = FeedBot(store)

    @bot.tree.command(name="setup", description="Configure the X feed, channel and notification roles")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup(interaction: discord.Interaction):
        wizard = Wizard(bot, interaction)
        await interaction.response.send_message(wizard.summary(), view=wizard, ephemeral=True)

    @bot.tree.command(name="status", description="Show feed settings and the most recent check")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(interaction: discord.Interaction):
        row = store.get(interaction.guild_id)
        if not row:
            await interaction.response.send_message("Run /setup first.", ephemeral=True)
            return
        config = row["config"]
        await interaction.response.send_message(
            f"Account: @{config['username']}\nChannel: <#{config['channel_id']}>"
            f"\nInterval: {config['minutes']} minutes\nNext check: <t:{int(row['next_check'])}:R>"
            f"\nLast successful checkpoint: <t:{int(row['checked'])}:F>"
            f"\nLast error: {row['error'] or 'None'}"
            f"\nPending posts: {len(store.pending(interaction.guild_id))}", ephemeral=True)

    @bot.tree.error
    async def command_error(interaction, error):
        text = "This command requires Manage Server permission."
        if not isinstance(error, app_commands.CheckFailure):
            text = "The command failed. Check the service logs."
            log.error("Command failure: %s", type(error).__name__)
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    return bot


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for key in ("DISCORD_BOT_TOKEN", "TWITTERAPI_KEY"):
        if not os.getenv(key):
            raise SystemExit(f"Set {key} before starting the bot.")
    path = os.getenv("DATABASE_PATH", "data/feed.sqlite3")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    create_bot(Store(path)).run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)

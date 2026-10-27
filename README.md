# Ripcars Post

Copies new original posts from a public X account into Discord. Default account:
`ripcarsio`. Default check interval: 15 minutes. Uses TwitterAPI.io, not the
official X developer API. No X login, cookies, or X developer account is needed.

## What you need

- Your TwitterAPI.io API key from the dashboard.
- A Discord bot application and its bot token.
- The numeric Discord server ID.
- An Ubuntu VPS with Python 3.10 or later, sudo, and outbound HTTPS access.

## Create the Discord bot once

1. Open https://discord.com/developers/applications and choose New Application.
2. Name it `Ripcars Post`. Set your chosen logo on the application/bot page.
3. In Bot, obtain the bot token using Reset Token when required.
4. Keep all Privileged Gateway Intents off. This bot does not need them.
5. In OAuth2 / URL Generator, select `bot` and `applications.commands`.
6. Select View Channels, Send Messages, Read Message History, and Embed Links. Invite it to
   your server using the generated URL. The equivalent permission integer is 84992.
7. Enable Developer Mode in your Discord client settings, then copy your server ID.

Discord's application setup is separate from X. The only X data credential is
the key issued by TwitterAPI.io.

## Install

On your VPS, clone this repository and run the installer:

```bash
git clone https://github.com/sobix13/Ripcars-Post.git
cd Ripcars-Post
sudo bash install.sh
```

The installer asks for both keys using hidden input, then the server ID. It creates
a dedicated system user, a Python virtual environment, a persistent SQLite database,
and a systemd service. It does not modify other bots or web server ports.
Credentials are stored in `/etc/ripcars-post.env` with mode 0600.

For an update, run these commands inside your existing checkout:

```bash
git pull --ff-only
sudo bash install.sh
```

Existing credentials, feed configuration and delivery progress are preserved.
The service restarts.

## Setup in Discord

Run `/setup` as a member with Manage Server permission.

- Select a destination text channel.
- Select zero, one, or multiple notification roles (up to 25).
- Use the settings button to edit the X username, interval, and link text.
- Press Save. Unsaved selections do not take effect.

Intervals range from 15 minutes to 24 hours. The default link text is
`Read the original post on X:`. All bot UI text is English.
Selected notification roles must be mentionable, or the bot must have Mention
Everyone permission in that destination channel. No @everyone or @here pings are
allowed by the bot. User mentions inside copied text do not notify anyone.

Run `/setup` again to change settings. Changes apply to newly queued posts.
Already queued posts retain their destination, message text and roles, so a retry
finishes the same delivery. Restore access to an old destination if it has pending
posts before removing that channel or its notification roles.
Use `/status` to see the next check, last successful checkpoint, and last error.

## Posting behavior

- Only posts created after the first saved setup are forwarded. Changing the
  account establishes a new starting time. Updating other settings preserves it.
- Replies and reposts are excluded. For threads, only the original first post is
  copied. Quote posts include the account's own text and a link to the original.
- A detail request is made for each unseen post before copying its text. The
  longest available full-text field is used without AI rewriting or summarizing.
- Posts exceeding Discord's 2,000-unit message limit are split without dropping
  characters. The original-post link and role pings appear on the final message.
- Media attachment short links are removed from the copied text. Other body links stay clickable with previews disabled. The canonical original-post link remains eligible for Discord's preview, which depends on X/Discord availability.
- This release copies text and the original link. It does not download images or
  videos, or expand X Articles into their article body.
- The poll uses timestamp-bounded search, not repeated downloads of the latest
  20 old posts. It follows pagination and delivers oldest first after an outage.
- A 15-minute overlap catches modest search-index delays. Persistent records
  suppress repeat deliveries. Delays longer than that overlap may miss a post.
- On failed delivery, the checkpoint is preserved and delivery is retried at the
  next configured interval. Each completed message part is saved separately.
- An uncertain send is reconciled against the channel history before retrying.
  Do not delete messages from an interrupted delivery until it has completed.
  SQLite and Discord are separate systems: this is not an atomic exactly-once
  guarantee. History reconciliation reduces duplicates after timeouts/restarts.
- A repeated/broken API cursor or 100-page catch-up limit is treated as a failure,
  not success, so the checkpoint is not advanced silently.

The provider determines which data is available. Full text and thread handling
must be checked with one real regular post, one long post, and a thread on your
account after installation. No live credentials were included with this package.

## Cost

At the documented $0.00015 minimum per search request, one account checked every
15 minutes costs a minimum of about $0.43 per 30 days. Returned posts, overlap
results, pagination and detail lookups add to that minimum. Multiple servers each
run their own feed checks. No fixed $1 monthly total is promised.
Sources checked on 2026-10-26:

- https://twitterapi.io/pricing/details
- https://docs.twitterapi.io/api-reference/endpoint/tweet_advanced_search
- https://docs.twitterapi.io/api-reference/endpoint/get_tweet_by_ids

## Operations

```bash
sudo systemctl status ripcars-post --no-pager
sudo journalctl -u ripcars-post -n 50 --no-pager
sudo systemctl restart ripcars-post
```

To change credentials: `sudo nano /etc/ripcars-post.env`, then restart the service.
Back up `/var/lib/ripcars-post` and keep a separate secure copy of your credentials.
For a consistent database backup, stop the service before copying the directory,
then start it again. Keep the database when reinstalling to retain delivery history.

## Local checks

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest -v
```

Tests use fake API and Discord responses. Live X/Discord delivery and the systemd
installation require your VPS and credentials and have not been run here.


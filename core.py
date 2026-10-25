"""Feed storage, API reads and lossless Discord message planning."""
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

DEFAULT = dict(username="ripcarsio", channel_id=0, roles=[], minutes=15,
               footer="Read the original post on X:")


def username(value):
    value = re.sub(r"^https?://(?:www\.)?(?:x|twitter)\.com/", "", value.strip())
    value = value.split("?")[0].strip("/@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
        raise ValueError("Enter a valid X username or profile URL.")
    return value.lower()


def tweet_time(tweet):
    value = tweet.get("createdAt", "")
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").timestamp()
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def original(tweet, account):
    return (isinstance(tweet, dict)
            and str(tweet.get("id", "")).isdigit()
            and tweet.get("author", {}).get("userName", "").lower() == account.lower()
            and not tweet.get("isReply") and not tweet.get("inReplyToId")
            and not tweet.get("retweeted_tweet")
            and not tweet.get("text", "").startswith("RT @"))


def full_text(tweet):
    candidates = [tweet.get(k) for k in ("text", "full_text", "fullText")]
    for key in ("note_tweet", "noteTweet"):
        note = tweet.get(key) or {}
        if isinstance(note, dict):
            candidates.append(note.get("text"))
            candidates.append(note.get("note_tweet_results", {}).get("result", {}).get("text"))
    return max((v for v in candidates if isinstance(v, str)), key=len, default="")


def units(text):
    return len(text.encode("utf-16-le")) // 2


def split_text(text, limit=2000):
    """Preserve every character, including whitespace and non-BMP characters."""
    parts, chunk, size = [], [], 0
    for char in text:
        width = units(char)
        if size + width > limit:
            parts.append("".join(chunk))
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        parts.append("".join(chunk))
    return parts


def message_parts(tweet, config):
    text = full_text(tweet)
    if not text:
        raise ValueError("The provider returned no post text.")
    link = f"https://x.com/{config['username']}/status/{tweet['id']}"
    mentions = " ".join(f"<@&{r}>" for r in config["roles"])
    footer = f"\n\n{config['footer']}\n{link}"
    if mentions:
        footer += "\n" + mentions
    # The footer stays together. Only the final part is allowed to ping roles.
    if units(text + footer) <= 2000:
        return [text + footer]
    parts = split_text(text)
    if units(parts[-1] + footer) <= 2000:
        parts[-1] += footer
    else:
        parts.append(footer.lstrip("\n"))
    return parts


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS feeds (
                guild INTEGER PRIMARY KEY, config TEXT NOT NULL,
                baseline REAL NOT NULL, checked REAL NOT NULL,
                next_check REAL NOT NULL, error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS posts (
                guild INTEGER, account TEXT, tweet TEXT, parts TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0, attempt TEXT,
                channel INTEGER NOT NULL, roles TEXT NOT NULL,
                PRIMARY KEY(guild, account, tweet));
        ''')

    def get(self, guild):
        row = self.db.execute("SELECT * FROM feeds WHERE guild=?", (guild,)).fetchone()
        if row:
            row = dict(row)
            row["config"] = json.loads(row["config"])
        return row

    def save(self, guild, config):
        old, now = self.get(guild), time.time()
        reset = not old or old["config"]["username"] != config["username"]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO feeds VALUES (?,?,?,?,?,?)", (
                guild, json.dumps(config), now if reset else old["baseline"],
                now if reset else old["checked"], now + config["minutes"] * 60, ""))

    def due(self):
        return [self.get(r[0]) for r in self.db.execute(
            "SELECT guild FROM feeds WHERE next_check <= ?", (time.time(),))]

    def finish(self, guild, checked, error=""):
        row = self.get(guild)
        with self.db:
            self.db.execute("UPDATE feeds SET checked=?,next_check=?,error=? WHERE guild=?", (
                checked if not error else row["checked"],
                time.time() + row["config"]["minutes"] * 60, error[:300], guild))

    def queue(self, guild, config, tweet):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO posts "
                            "(guild,account,tweet,parts,channel,roles) VALUES (?,?,?,?,?,?)", (
                                guild, config["username"], tweet["id"],
                                json.dumps(message_parts(tweet, config)),
                                config["channel_id"], json.dumps(config["roles"])))

    def post(self, guild, account, tweet):
        return self.db.execute("SELECT * FROM posts WHERE guild=? AND account=? AND tweet=?",
                               (guild, account, tweet)).fetchone()

    def pending(self, guild):
        return [r for r in self.db.execute("SELECT * FROM posts WHERE guild=? ORDER BY rowid",
                                          (guild,)) if r["sent"] < len(json.loads(r["parts"]))]

    def progress(self, row, sent, attempt=None):
        with self.db:
            self.db.execute("UPDATE posts SET sent=?,attempt=? WHERE guild=? AND account=? AND tweet=?",
                            (sent, attempt, row["guild"], row["account"], row["tweet"]))


class API:
    def __init__(self, session, key):
        self.session, self.key = session, key

    async def get(self, path, params):
        async with self.session.get("https://api.twitterapi.io" + path,
                                    params=params, headers={"x-api-key": self.key}) as response:
            if response.status != 200:
                # Do not log response bodies or headers: they might contain credentials.
                raise RuntimeError(f"TwitterAPI.io HTTP {response.status}")
            data = await response.json()
        if not isinstance(data, dict) or data.get("status") == "error" or data.get("error"):
            raise RuntimeError("TwitterAPI.io returned an API error")
        if not isinstance(data.get("tweets"), list):
            raise RuntimeError("TwitterAPI.io returned an unexpected response")
        return data

    async def search(self, account, since, until):
        params = {"query": f"from:{account} -filter:replies -filter:nativeretweets "
                           f"since_time:{int(since)} until_time:{int(until)}",
                  "queryType": "Latest", "cursor": ""}
        tweets, cursors = {}, set()
        for _ in range(100):
            data = await self.get("/twitter/tweet/advanced_search", params)
            for tweet in data["tweets"]:
                if original(tweet, account):
                    tweets[tweet["id"]] = tweet
            if not data.get("has_next_page"):
                return sorted(tweets.values(), key=lambda t: int(t["id"]))
            cursor = data.get("next_cursor")
            if not cursor or cursor in cursors:
                raise RuntimeError("Search pagination did not advance")
            cursors.add(cursor)
            params["cursor"] = cursor
        raise RuntimeError("Catch-up exceeded 100 pages; checkpoint preserved")

    async def detail(self, tweet):
        data = await self.get("/twitter/tweets", {"tweet_ids": tweet["id"]})
        for result in data["tweets"]:
            if result.get("id") == tweet["id"]:
                if units(full_text(result)) < units(full_text(tweet)):
                    result["fullText"] = full_text(tweet)
                return result
        raise RuntimeError("The provider did not return the requested post")

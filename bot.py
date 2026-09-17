"""
Telegram Market News Bot
-------------------------
Sends you:
  - ForexFactory "Red Folder" (High Impact) economic calendar releases
  - War / geopolitical conflict news
  - General economic / market-moving news
Pins the message in your Telegram chat if it's flagged as critical.

Designed to run as a tiny Flask web app on Render's free tier.
An external cron service (e.g. cron-job.org) hits /run-check every
10-15 minutes to trigger a news check (Render free web services do not
run background schedulers reliably on their own, so we use this
"ping to trigger" pattern).
"""

import os
import time
import hashlib
from datetime import datetime, timedelta, timezone

import requests
import feedparser
from flask import Flask, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------------------
# CONFIG (set these as environment variables on Render — see instructions)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CHECK_WINDOW_MINUTES = int(os.environ.get("CHECK_WINDOW_MINUTES", "15"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Public RSS feeds (no API key needed)
WAR_FEEDS = [
    "https://www.aljazeera.com/xml/rss/all.xml",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
]

ECONOMIC_FEEDS = [
    "http://feeds.bbci.co.uk/news/business/rss.xml",
    "https://www.investing.com/rss/news.rss",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]

# Unofficial but widely-used public JSON feed of the ForexFactory calendar.
# It can occasionally change format or go down — see the notes at the end
# of the setup instructions for a fallback plan.
FOREX_FACTORY_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

WAR_KEYWORDS = [
    "war", "invasion", "airstrike", "air strike", "missile", "troops",
    "ceasefire", "conflict", "military strike", "attack on", "strikes kill",
    "drone strike", "offensive", "shelling", "combat", "casualties",
]

ECON_KEYWORDS = [
    "inflation", "interest rate", "central bank", "federal reserve", " fed ",
    "recession", "gdp", "unemployment", "rate hike", "rate cut",
    "stock market", "oil price", "opec", "tariff", "trade deal",
    "default", "bankruptcy", "sanctions", "jobs report",
]

CRITICAL_WAR_KEYWORDS = [
    "nuclear", "declares war", "invasion of", "assassinat", "coup",
    "martial law", "ceasefire collapse", "major offensive",
]

# ---------------------------------------------------------------------------
# In-memory de-duplication (resets if the free instance restarts — fine,
# since we also only ever look at recently-published items anyway)
# ---------------------------------------------------------------------------
_sent_cache = {}
CACHE_TTL_MINUTES = 180


def already_sent(key):
    now = time.time()
    for k in list(_sent_cache.keys()):
        if now - _sent_cache[k] > CACHE_TTL_MINUTES * 60:
            del _sent_cache[k]
    if key in _sent_cache:
        return True
    _sent_cache[key] = now
    return False


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
def send_telegram_message(text, pin=False):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars")
        return None
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    data = resp.json()
    if pin and data.get("ok"):
        message_id = data["result"]["message_id"]
        requests.post(
            f"{TELEGRAM_API}/pinChatMessage",
            data={
                "chat_id": CHAT_ID,
                "message_id": message_id,
                "disable_notification": False,
            },
            timeout=15,
        )
    return data


# ---------------------------------------------------------------------------
# News checking
# ---------------------------------------------------------------------------
def entry_recent(published_struct, window_minutes):
    if not published_struct:
        return False
    published = datetime.fromtimestamp(time.mktime(published_struct), tz=timezone.utc)
    now = datetime.now(timezone.utc)
    return now - timedelta(minutes=window_minutes) <= published <= now + timedelta(minutes=1)


def check_rss_feeds(feeds, keywords, critical_keywords=None, label=""):
    messages = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
    for url in feeds:
        try:
            raw = requests.get(url, headers=headers, timeout=15)
            feed = feedparser.parse(raw.content)
        except Exception as e:
            print(f"Failed to fetch/parse {url}: {e}")
            continue
        for entry in feed.entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            text_blob = (title + " " + summary).lower()
            published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if not entry_recent(published_struct, CHECK_WINDOW_MINUTES):
                continue
            if not any(k in text_blob for k in keywords):
                continue

            dedupe_key = hashlib.md5(title.encode("utf-8")).hexdigest()
            if already_sent(dedupe_key):
                continue

            is_critical = bool(critical_keywords) and any(k in text_blob for k in critical_keywords)
            link = entry.get("link", "")
            emoji = "🚨" if is_critical else ("⚔️" if label == "WAR" else "💰")
            msg = f"{emoji} <b>{label} NEWS</b>\n{title}\n{link}"
            messages.append((msg, is_critical))
    return messages


def check_forex_factory():
    messages = []
    try:
        resp = requests.get(FOREX_FACTORY_JSON, timeout=15)
        events = resp.json()
    except Exception as e:
        print(f"Failed to fetch forex factory calendar: {e}")
        return messages

    now = datetime.now(timezone.utc)
    for event in events:
        if event.get("impact") != "High":
            continue
        try:
            event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        except Exception:
            continue

        # Only alert once the event has just been released (within the
        # check window), so it fires roughly once per real release.
        if timedelta(0) <= now - event_time <= timedelta(minutes=CHECK_WINDOW_MINUTES):
            title = event.get("title", "Unknown Event")
            country = event.get("country", "")
            dedupe_key = hashlib.md5(f"{title}{country}{event_time}".encode("utf-8")).hexdigest()
            if already_sent(dedupe_key):
                continue

            actual = event.get("actual") or "N/A"
            forecast = event.get("forecast") or "N/A"
            previous = event.get("previous") or "N/A"
            msg = (
                "🔴 <b>RED FOLDER — HIGH IMPACT</b>\n"
                f"{country}: {title}\n"
                f"Actual: {actual} | Forecast: {forecast} | Previous: {previous}"
            )
            messages.append((msg, True))
    return messages


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/run-check")
def run_check():
    all_messages = []
    all_messages += check_forex_factory()
    all_messages += check_rss_feeds(WAR_FEEDS, WAR_KEYWORDS, CRITICAL_WAR_KEYWORDS, label="WAR")
    all_messages += check_rss_feeds(ECONOMIC_FEEDS, ECON_KEYWORDS, label="ECONOMIC")

    sent = 0
    for msg, is_critical in all_messages:
        send_telegram_message(msg, pin=is_critical)
        sent += 1
        time.sleep(1)  # be gentle with Telegram's rate limits

    return jsonify({"status": "ok", "checked_at": datetime.now(timezone.utc).isoformat(), "sent": sent})


@app.route("/")
def home():
    return "News bot is alive. Hit /run-check to trigger a check."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

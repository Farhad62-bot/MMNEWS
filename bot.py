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
# How far ahead of a red-folder event to send the "coming up" heads-up alert.
ADVANCE_WARNING_MINUTES = int(os.environ.get("ADVANCE_WARNING_MINUTES", "60"))
# Your local UTC offset, so "start of day" / "end of day" match YOUR day,
# not UTC's. E.g. Gulf Standard Time (UAE) is UTC+4, so set this to 4.
TIMEZONE_OFFSET_HOURS = float(os.environ.get("TIMEZONE_OFFSET_HOURS", "4"))

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

# Tracks the last local calendar date we ran the daily pin-reset/digest for.
_last_reset_date = None


def already_sent(key):
    now = time.time()
    for k in list(_sent_cache.keys()):
        if now - _sent_cache[k] > CACHE_TTL_MINUTES * 60:
            del _sent_cache[k]
    if key in _sent_cache:
        return True
    _sent_cache[key] = now
    return False


def to_local(dt_utc):
    """Convert a UTC datetime to your local time using TIMEZONE_OFFSET_HOURS."""
    return dt_utc + timedelta(hours=TIMEZONE_OFFSET_HOURS)


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

def unpin_all_messages():
    if not BOT_TOKEN or not CHAT_ID:
        return None
    return requests.post(
        f"{TELEGRAM_API}/unpinAllChatMessages",
        data={"chat_id": CHAT_ID},
        timeout=15,
    ).json()


# ---------------------------------------------------------------------------
# News checking
# ---------------------------------------------------------------------------
def fetch_ff_events():
    """Returns (events, fetch_succeeded). fetch_succeeded is False only when
    we got neither fresh data nor a usable cache — callers should avoid
    locking in decisions (like the once-per-day reset) on that case."""
    global _cached_ff_events
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
    try:
        resp = requests.get(FOREX_FACTORY_JSON, timeout=15, headers=headers)
        text = resp.text.strip()
        content_type = resp.headers.get("Content-Type", "")

        # ForexFactory rate-limits this feed (2 requests / 5 min per IP).
        # When exceeded, it returns an HTML "Request Denied" page instead
        # of JSON — detect that instead of silently losing the day's data.
        if "json" not in content_type.lower() and not text.startswith("["):
            print("FF calendar returned non-JSON (likely rate-limited or blocked). Using cached data.")
            if _cached_ff_events is not None:
                return _cached_ff_events, True
            return [], False

        events = resp.json()
        _cached_ff_events = events
        return events, True
    except Exception as e:
        print(f"Failed to fetch forex factory calendar: {e}")
        if _cached_ff_events is not None:
            return _cached_ff_events, True
        return [], False


def maybe_run_daily_reset(events):
    """
    Once per local calendar day: unpin everything from the previous day,
    then pin a single digest listing every red-folder event coming up today.
    Returns True if it just ran the reset (useful for the /run-check response).
    """
    global _last_reset_date
    now_local = to_local(datetime.now(timezone.utc))
    today = now_local.date()

    if _last_reset_date == today:
        return False
    _last_reset_date = today

    unpin_all_messages()

    todays_high_impact = []
    for event in events:
        if event.get("impact") != "High":
            continue
        try:
            event_time_utc = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        event_time_local = to_local(event_time_utc)
        if event_time_local.date() == today:
            todays_high_impact.append((event_time_local, event.get("country", ""), event.get("title", "Unknown Event")))

    todays_high_impact.sort(key=lambda row: row[0])

    if todays_high_impact:
        lines = "\n".join(f"{t.strftime('%H:%M')} — {c}: {ti}" for t, c, ti in todays_high_impact)
        digest = f"📅 <b>TODAY'S RED FOLDER EVENTS</b>\n{lines}"
    else:
        digest = "📅 <b>TODAY'S RED FOLDER EVENTS</b>\nNo high-impact events scheduled today."

    send_telegram_message(digest, pin=True)
    return True


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


def check_forex_factory(events):
    messages = []
    now = datetime.now(timezone.utc)
    for event in events:
        if event.get("impact") != "High":
            continue
        try:
            event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        except Exception:
            continue

        title = event.get("title", "Unknown Event")
        country = event.get("country", "")
        minutes_until = (event_time - now).total_seconds() / 60

        # 1) HEADS-UP, before it happens — this is the one that mirrors
        #    what you see coming up on the ForexFactory calendar page.
        if 0 < minutes_until <= ADVANCE_WARNING_MINUTES:
            dedupe_key = hashlib.md5(f"warn-{title}{country}{event_time}".encode("utf-8")).hexdigest()
            if not already_sent(dedupe_key):
                forecast = event.get("forecast") or "N/A"
                previous = event.get("previous") or "N/A"
                msg = (
                    "🔔 <b>RED FOLDER COMING UP — in "
                    f"{int(round(minutes_until))} min</b>\n"
                    f"{country}: {title}\n"
                    f"Forecast: {forecast} | Previous: {previous}"
                )
                messages.append((msg, True))

        # 2) RESULT, right after it's released — actual vs forecast.
        elif timedelta(0) <= now - event_time <= timedelta(minutes=CHECK_WINDOW_MINUTES):
            dedupe_key = hashlib.md5(f"release-{title}{country}{event_time}".encode("utf-8")).hexdigest()
            if not already_sent(dedupe_key):
                actual = event.get("actual") or "N/A"
                forecast = event.get("forecast") or "N/A"
                previous = event.get("previous") or "N/A"
                msg = (
                    "🔴 <b>RED FOLDER RELEASED — HIGH IMPACT</b>\n"
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
    ff_events = fetch_ff_events()
    did_reset = maybe_run_daily_reset(ff_events)

    all_messages = []
    all_messages += check_forex_factory(ff_events)
    all_messages += check_rss_feeds(WAR_FEEDS, WAR_KEYWORDS, CRITICAL_WAR_KEYWORDS, label="WAR")
    all_messages += check_rss_feeds(ECONOMIC_FEEDS, ECON_KEYWORDS, label="ECONOMIC")

    sent = 0
    for msg, is_critical in all_messages:
        send_telegram_message(msg, pin=is_critical)
        sent += 1
        time.sleep(1)  # be gentle with Telegram's rate limits

    return jsonify({
        "status": "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "daily_reset_ran": did_reset,
        "sent": sent,
    })


@app.route("/")
def home():
    return "News bot is alive. Hit /run-check to trigger a check."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

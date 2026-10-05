#!/usr/bin/env python3
"""TLDL: Too Long, Didn't Listen.

Builds one personal podcast episode per day:
  1. gathers weather, TLDR newsletters and IBM newsroom items
  2. has Claude write a fresh, conversational script
  3. turns the script into audio with free neural TTS
  4. updates an RSS feed (served by GitHub Pages) that Apple Podcasts follows

Run `python generate.py --sources-only` to see what was gathered without
spending any API credit.
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# --------------------------------------------------------------------------
# Configuration (all overridable with environment variables)
# --------------------------------------------------------------------------


def env(name, default):
    value = os.getenv(name)
    return value if value not in (None, "") else default


ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
EPISODES = DOCS / "episodes"
DATA = ROOT / "data"
HISTORY_FILE = DATA / "history.json"

ET_TZ = ZoneInfo("America/New_York")
LISTENER = env("LISTENER_NAME", "Zach")
PLACE = env("PLACE", "Auburn Hills, Michigan")
LAT = float(env("LAT", "42.6875"))
LON = float(env("LON", "-83.2341"))
TLDR_TOPICS = [t.strip() for t in env("TLDR_TOPICS", "ai,tech").split(",") if t.strip()]
MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")
VOICE = env("TTS_VOICE", "en-US-AndrewNeural")
RATE = env("TTS_RATE", "+0%")
TARGET_MINUTES = int(env("TARGET_MINUTES", "12"))
KEEP_EPISODES = int(env("KEEP_EPISODES", "14"))
BASE_URL = env("BASE_URL", "").rstrip("/")

SHOW_TITLE = "TLDL: Too Long, Didn't Listen"
SHOW_DESCRIPTION = (
    "A daily morning briefing made for one listener: a fresh greeting, today's "
    "weather, the TLDR newsletters and the latest from IBM."
)

UA = "Mozilla/5.0 (compatible; TLDL-personal-podcast/1.0)"
IBM_HOME = "https://newsroom.ibm.com/"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url, **kwargs):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    headers.update(kwargs.pop("headers", {}))
    last = None
    for attempt in range(3):
        try:
            return requests.get(url, headers=headers, timeout=30, **kwargs)
        except requests.RequestException as exc:  # network hiccup, retry
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


# --------------------------------------------------------------------------
# Weather (Open-Meteo: free, no API key)
# --------------------------------------------------------------------------

WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light showers", 81: "showers",
    82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def _slot(hourly, start, end):
    idx = range(start, end)
    temps = [hourly["temperature_2m"][i] for i in idx]
    rain = [hourly["precipitation_probability"][i] or 0 for i in idx]
    codes = [hourly["weather_code"][i] for i in idx]
    code = max(set(codes), key=codes.count)
    return {
        "temp_f": f"{round(min(temps))} to {round(max(temps))}",
        "rain_chance_percent": max(rain),
        "sky": WMO.get(code, "mixed"),
    }


def fetch_weather():
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "weather_code,wind_speed_10m_max,sunrise,sunset,uv_index_max",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "America/New_York",
        "forecast_days": 1,
    }
    r = http_get("https://api.open-meteo.com/v1/forecast", params=params)
    r.raise_for_status()
    j = r.json()
    d, h = j["daily"], j["hourly"]
    return {
        "place": PLACE,
        "high_f": round(d["temperature_2m_max"][0]),
        "low_f": round(d["temperature_2m_min"][0]),
        "overall": WMO.get(d["weather_code"][0], "mixed"),
        "rain_chance_percent_max": d["precipitation_probability_max"][0],
        "wind_mph_max": round(d["wind_speed_10m_max"][0]),
        "uv_index_max": d["uv_index_max"][0],
        "sunrise": d["sunrise"][0][-5:],
        "sunset": d["sunset"][0][-5:],
        "morning_7_to_10": _slot(h, 7, 10),
        "midday_11_to_2": _slot(h, 11, 14),
        "afternoon_3_to_6": _slot(h, 15, 18),
        "evening_7_to_10": _slot(h, 19, 22),
    }


# --------------------------------------------------------------------------
# TLDR newsletters (archive pages at tldr.tech/<topic>/<YYYY-MM-DD>)
# --------------------------------------------------------------------------

TRAILING_META = re.compile(r"\s*\((?:\d+\s*minute read|[^)]*\brepo\b[^)]*)\)\s*$", re.I)
SPONSOR_TITLE = re.compile(r"\((?:sponsor|sponsored|ad)\)", re.I)


def _block_end(el):
    return isinstance(el, Tag) and (el.name in ("h2", "h3") or el.find(["h2", "h3"]) is not None)


def _summary_after(h3):
    start = h3.parent if (h3.parent is not None and h3.parent.name == "a") else h3
    parts = []
    for sib in start.next_siblings:
        if _block_end(sib):
            break
        if isinstance(sib, Tag):
            parts.append(sib.get_text(" ", strip=True))
        elif isinstance(sib, NavigableString) and str(sib).strip():
            parts.append(str(sib).strip())
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    if not text and start.parent is not None and len(start.parent.find_all("h3")) == 1:
        text = start.parent.get_text(" ", strip=True).replace(h3.get_text(" ", strip=True), "", 1)
        text = re.sub(r"\s+", " ", text).strip()
    return text


def is_promo(title, href):
    if SPONSOR_TITLE.search(title):
        return True
    low = href.lower()
    if "utm_medium=paid" in low or "utm_medium=newsletter" in low and "utm_source=tldr&" in low:
        return True
    if "jobs.ashbyhq.com/tldr" in low or "advertise.tldr" in low:
        return True
    return False


def parse_tldr(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    items, section = [], ""
    for h in soup.find_all(["h2", "h3"]):
        a = h.find("a", href=True) or h.find_parent("a", href=True)
        title = h.get_text(" ", strip=True)
        if not a:
            if h.name == "h3" and title:
                section = title
            continue
        if h.name != "h3":
            continue
        href = a["href"]
        if is_promo(title, href):
            continue
        summary = _summary_after(h)
        read = re.search(r"\((\d+)\s*minute read\)", title, re.I)
        items.append({
            "title": TRAILING_META.sub("", title).strip(),
            "url": href.split("?")[0],
            "summary": summary[:900],
            "section": section,
            "read_minutes": int(read.group(1)) if read else None,
        })
    return items


def fetch_tldr(now, history):
    out = {}
    used = history.get("tldr_used", {})
    for topic in TLDR_TOPICS:
        found = None
        for back in range(0, 5):
            day = (now - timedelta(days=back)).date()
            url = f"https://tldr.tech/{topic}/{day:%Y-%m-%d}"
            try:
                r = http_get(url)
            except requests.RequestException as exc:
                log(f"TLDR {topic} {day}: {exc}")
                continue
            if r.status_code != 200:
                continue
            items = parse_tldr(r.text)
            if items:
                found = {"topic": topic, "issue_date": f"{day:%Y-%m-%d}", "url": url, "items": items}
                break
        if not found:
            out[topic] = {"topic": topic, "status": "no issue found in the last few days"}
        elif used.get(topic) == found["issue_date"]:
            out[topic] = {"topic": topic, "status": f"no new issue yet; latest is {found['issue_date']} and was already covered"}
        else:
            found["status"] = "ok"
            out[topic] = found
    return out


# --------------------------------------------------------------------------
# IBM newsroom
# --------------------------------------------------------------------------

DATE_FIND = re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4}")
IBM_LINK = re.compile(r"newsroom\.ibm\.com/(?:20\d\d-|blog-)|ibm\.com/(?:new/announcements|blog)/")


def parse_ibm_home(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        href = urljoin(IBM_HOME, a["href"])
        if len(title) < 25 or not IBM_LINK.search(href) or href in seen:
            continue
        date = None
        m = re.search(r"newsroom\.ibm\.com/(20\d\d-\d\d-\d\d)-", href)
        if m:
            date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        else:
            prev = a.find_previous(string=DATE_FIND)
            if prev:
                try:
                    date = datetime.strptime(DATE_FIND.search(prev).group(0), "%b %d, %Y").date()
                except ValueError:
                    date = None
        if not date:
            continue
        seen.add(href)
        items.append({"title": title, "url": href, "date": date})
    items.sort(key=lambda i: i["date"], reverse=True)
    return items


def article_text(url, limit=1800):
    try:
        r = http_get(url)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        paras = [p for p in paras if len(p) > 60 and not p.lower().startswith(("about ibm", "contact"))]
        return " ".join(paras)[:limit]
    except requests.RequestException:
        return ""


def fetch_ibm(now):
    lookback = 4 if now.weekday() == 0 else 2  # Mondays cover the weekend and Friday
    cutoff = (now - timedelta(days=lookback)).date()
    r = http_get(IBM_HOME)
    r.raise_for_status()
    fresh = [i for i in parse_ibm_home(r.text) if i["date"] >= cutoff][:4]
    for item in fresh[:3]:
        item["details"] = article_text(item["url"])
    for item in fresh:
        item["date"] = item["date"].isoformat()
    return {"window_days": lookback, "items": fresh}


# --------------------------------------------------------------------------
# Script writing (Claude API)
# --------------------------------------------------------------------------

GREETING_STYLES = [
    "Open like a friend who has already had coffee and is easing you into the day.",
    "Open with a short, wry observation about the day of the week or the date, then say good morning.",
    "Open with the weather as the hook, then the greeting.",
    "Open like a morning radio host: warm and brisk.",
    "Open calm and understated, like a trusted chief of staff giving a quiet briefing.",
    "Open with a one-line teaser of the most interesting story, then greet him properly.",
    "Open playful, with a light, harmless running joke that you invent on the spot.",
    "Open with a short, sincere note of encouragement for the day ahead.",
    "Open as if you are just walking into the room with the news under your arm.",
]
TONES = [
    "upbeat and warm", "dry and witty", "calm and focused",
    "curious and enthusiastic", "conversational and gently sarcastic",
    "energetic, like a sports desk covering tech",
]
TRANSITIONS = [
    "Move between stories the way a person would, with natural connective phrases, never a numbered list.",
    "Group related stories into themes and connect them, then handle one-offs quickly.",
    "Use short reactions between stories (a quick opinion, a question, a raised eyebrow) as transitions.",
    "Lead each story with why it matters, then the facts.",
]
SIGNOFFS = [
    "End with a short, warm send-off and one small wish for the day.",
    "End with a one-line recap of the single thing worth remembering today.",
    "End with a light joke and a sign-off.",
    "End quietly and briefly, like closing a laptop.",
]

SYSTEM_PROMPT = f"""You are the host of "TLDL", short for "Too Long, Didn't Listen", a private daily podcast made for exactly one listener, {LISTENER}. {LISTENER} lives in {PLACE} and works as an AI Adoption Leader at IBM Consulting, so AI news matters to his work and IBM news matters to his employer. You are like a smart, friendly person who wakes up with him and talks him through the morning.

Write the full spoken script for today's episode.

Hard rules for the spoken text:
- Plain spoken prose only. No headings, bullet points, markdown, stage directions, sound effects, emojis, or URLs.
- Never use em dashes or en dashes. Use commas, periods, or the word "to" instead.
- Write for the ear: short sentences, contractions, numbers the way people say them, and say "T L D L" when you say the show name.
- Address him directly as {LISTENER}. Greet him by name in the opening.
- Required segments, in this order: (1) a greeting, (2) the weather forecast for {PLACE} for today, (3) the TLDR news, (4) the IBM news, (5) a sign-off.
- Weather: cover high and low, the shape of the day (morning, afternoon, evening), rain or snow chances, wind if notable, and one practical suggestion (a jacket, an umbrella, leaving early). Do not read raw data like a table.
- TLDR news: cover EVERY story you are given, grouped sensibly. Stay faithful to the facts in the summaries and never invent details, numbers, or quotes. Skip anything that is an advertisement, sponsorship, job posting, or a promotion for TLDR itself. If a newsletter has no new issue, say so briefly in a natural way and move on. Give the bigger stories a little more room and the small ones one or two sentences.
- IBM news: cover the items given, using only the provided details. If there are no items, say there is no news on the IBM front today, in a fresh and natural way each time.
- Do not invent holidays, events, or facts about {LISTENER}. Mention a holiday only if you are certain of it.
- Do not read source names or links aloud unless the source itself is the story.
- Target about {TARGET_MINUTES} minutes of speech, which is roughly {TARGET_MINUTES * 150} words. Shorter is fine when there is little news (for example weekends, when TLDR does not publish).

Variety is the point. Every episode must feel written fresh. Follow the style directions you are given, and never reuse the opening lines, jokes, or sign-offs from recent episodes that are listed for you.

Output format, exactly:
TITLE: <a short episode title of three to eight words, no colons>
<blank line>
<the spoken script>"""


def pick_fresh(options, recent):
    pool = [o for o in options if o not in recent] or options
    return random.SystemRandom().choice(pool)


def build_user_prompt(now, weather, tldr, ibm, picks, history):
    recent_openings = [e.get("opening", "") for e in history.get("episodes", [])[:7]]
    recent_signoffs = [e.get("closing", "") for e in history.get("episodes", [])[:7]]
    parts = [
        f"Today is {now:%A, %B %d, %Y}. The episode lands around 7:30 AM Eastern.",
        "",
        "STYLE DIRECTIONS FOR TODAY",
        f"- Opening: {picks['greeting']}",
        f"- Tone: {picks['tone']}",
        f"- Transitions: {picks['transition']}",
        f"- Ending: {picks['signoff']}",
        "",
        "RECENT OPENINGS TO AVOID REPEATING:",
        *(f"- {o}" for o in recent_openings if o),
        "RECENT ENDINGS TO AVOID REPEATING:",
        *(f"- {c}" for c in recent_signoffs if c),
        "",
        "WEATHER DATA (" + PLACE + ")",
        json.dumps(weather, indent=2) if weather else "Weather data was unavailable today. Say so briefly and move on.",
        "",
        "TLDR DATA",
        json.dumps(tldr, indent=2),
        "",
        "IBM NEWS DATA",
        json.dumps(ibm, indent=2) if ibm is not None else "The IBM newsroom could not be reached. Say you could not check IBM news this morning.",
    ]
    return "\n".join(parts)


EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def clean_for_speech(text):
    text = text.replace("—", ", ").replace("–", " to ")
    text = EMOJI.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ,", ",", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def write_script(prompt):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        temperature=1.0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    m = re.match(r"TITLE:\s*(.+?)\n+(.*)", raw, re.S)
    title, body = (m.group(1).strip(), m.group(2)) if m else ("Your morning briefing", raw)
    return clean_for_speech(title).rstrip("."), clean_for_speech(body)


# --------------------------------------------------------------------------
# Audio (edge-tts is free; gTTS is the emergency fallback)
# --------------------------------------------------------------------------


async def _edge(text, path):
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))


def synthesize(text, path):
    for attempt in range(3):
        try:
            asyncio.run(_edge(text, path))
            if path.exists() and path.stat().st_size > 20000:
                return "edge-tts"
        except Exception as exc:  # noqa: BLE001
            log(f"edge-tts attempt {attempt + 1} failed: {exc}")
        time.sleep(5 * (attempt + 1))
    log("Falling back to gTTS (less natural voice).")
    from gtts import gTTS
    gTTS(text, lang="en", tld="com").save(str(path))
    return "gtts"


def mp3_seconds(path):
    from mutagen.mp3 import MP3
    return int(MP3(str(path)).info.length)


# --------------------------------------------------------------------------
# History, show notes and the RSS feed
# --------------------------------------------------------------------------


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {"episodes": [], "tldr_used": {}}


def save_history(h):
    DATA.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(h, indent=2))


def show_notes(weather, tldr, ibm):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    html = []
    if weather:
        html.append(f"<p><b>Weather:</b> high {weather['high_f']}F, low {weather['low_f']}F, {esc(weather['overall'])}.</p>")
    for topic, block in tldr.items():
        if block.get("status") != "ok":
            continue
        html.append(f"<h3>TLDR {esc(topic.upper())} ({block['issue_date']})</h3><ul>")
        html += [f'<li><a href="{esc(i["url"])}">{esc(i["title"])}</a></li>' for i in block["items"]]
        html.append("</ul>")
    if ibm and ibm["items"]:
        html.append("<h3>IBM</h3><ul>")
        html += [f'<li><a href="{esc(i["url"])}">{esc(i["title"])}</a></li>' for i in ibm["items"]]
        html.append("</ul>")
    return "".join(html)


def build_feed(history):
    ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
    CONTENT = "http://purl.org/rss/1.0/modules/content/"
    ATOM = "http://www.w3.org/2005/Atom"
    ET.register_namespace("itunes", ITUNES)
    ET.register_namespace("content", CONTENT)
    ET.register_namespace("atom", ATOM)
    q = lambda ns, tag: f"{{{ns}}}{tag}"  # noqa: E731

    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")

    def add(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, tag, attrs)
        if text is not None:
            el.text = text
        return el

    add(ch, "title", SHOW_TITLE)
    add(ch, "link", BASE_URL + "/")
    add(ch, "description", SHOW_DESCRIPTION)
    add(ch, "language", "en-us")
    add(ch, q(ATOM, "link"), href=f"{BASE_URL}/feed.xml", rel="self", type="application/rss+xml")
    add(ch, q(ITUNES, "author"), "TLDL")
    add(ch, q(ITUNES, "image"), href=f"{BASE_URL}/cover.png")
    add(ch, q(ITUNES, "explicit"), "false")
    add(ch, q(ITUNES, "block"), "Yes")  # private show: keep it out of directories
    ET.SubElement(ch, q(ITUNES, "category"), {"text": "News"})

    for ep in sorted(history["episodes"], key=lambda e: e["date"], reverse=True)[:KEEP_EPISODES]:
        item = add(ch, "item")
        add(item, "title", f"{ep['date']}: {ep['title']}")
        add(item, "description", ep["summary"])
        add(item, q(CONTENT, "encoded"), ep["notes_html"])
        add(item, "pubDate", format_datetime(datetime.fromisoformat(ep["published"])))
        add(item, "guid", f"tldl-{ep['date']}", isPermaLink="false")
        add(item, "enclosure", url=f"{BASE_URL}/{ep['file']}", length=str(ep["bytes"]), type="audio/mpeg")
        add(item, q(ITUNES, "duration"), str(ep["seconds"]))
        add(item, q(ITUNES, "episodeType"), "full")
        add(item, q(ITUNES, "explicit"), "false")

    tree = ET.ElementTree(rss)
    ET.indent(tree)
    DOCS.mkdir(exist_ok=True)
    tree.write(DOCS / "feed.xml", encoding="utf-8", xml_declaration=True)


def prune(history):
    history["episodes"].sort(key=lambda e: e["date"], reverse=True)
    for old in history["episodes"][KEEP_EPISODES:]:
        (DOCS / old["file"]).unlink(missing_ok=True)
    history["episodes"] = history["episodes"][:KEEP_EPISODES]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def gather(now, history):
    try:
        weather = fetch_weather()
    except Exception as exc:  # noqa: BLE001
        log(f"Weather failed: {exc}")
        weather = None
    tldr = fetch_tldr(now, history)
    try:
        ibm = fetch_ibm(now)
    except Exception as exc:  # noqa: BLE001
        log(f"IBM failed: {exc}")
        ibm = None
    return weather, tldr, ibm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="build even if today's episode exists")
    ap.add_argument("--sources-only", action="store_true", help="print gathered data and stop")
    ap.add_argument("--no-audio", action="store_true", help="write the script but skip audio and feed")
    args = ap.parse_args()

    now = datetime.now(ET_TZ)
    date_str = f"{now:%Y-%m-%d}"
    history = load_history()

    if not (args.force or args.sources_only):
        if any(e["date"] == date_str for e in history["episodes"]):
            log("Today's episode already exists. Nothing to do.")
            return
        if os.getenv("GITHUB_EVENT_NAME") == "schedule" and now.hour < 6:
            log("Too early in Eastern time (daylight saving double-cron). Skipping.")
            return

    weather, tldr, ibm = gather(now, history)
    if args.sources_only:
        print(json.dumps({"weather": weather, "tldr": tldr, "ibm": ibm}, indent=2, default=str))
        return

    recent = history["episodes"][:5]
    picks = {
        "greeting": pick_fresh(GREETING_STYLES, [e.get("picks", {}).get("greeting") for e in recent]),
        "tone": pick_fresh(TONES, [e.get("picks", {}).get("tone") for e in recent]),
        "transition": pick_fresh(TRANSITIONS, [e.get("picks", {}).get("transition") for e in recent]),
        "signoff": pick_fresh(SIGNOFFS, [e.get("picks", {}).get("signoff") for e in recent]),
    }
    prompt = build_user_prompt(now, weather, tldr, ibm, picks, history)
    title, script = write_script(prompt)
    log(f"Script: {len(script.split())} words. Title: {title}")

    EPISODES.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(exist_ok=True)
    (DATA / f"script-{date_str}.txt").write_text(f"{title}\n\n{script}\n")
    if args.no_audio:
        print(f"{title}\n\n{script}")
        return
    if not BASE_URL:
        sys.exit("BASE_URL is not set (for example https://yourname.github.io/tldl).")

    mp3 = EPISODES / f"{date_str}.mp3"
    engine = synthesize(script, mp3)
    log(f"Audio via {engine}: {mp3.stat().st_size / 1e6:.1f} MB")

    paragraphs = [p for p in script.split("\n\n") if p.strip()]
    history["episodes"] = [e for e in history["episodes"] if e["date"] != date_str]
    history["episodes"].append({
        "date": date_str,
        "title": title,
        "summary": script[:300].rsplit(" ", 1)[0] + "...",
        "notes_html": show_notes(weather, tldr, ibm),
        "file": f"episodes/{date_str}.mp3",
        "bytes": mp3.stat().st_size,
        "seconds": mp3_seconds(mp3),
        "published": now.isoformat(),
        "opening": paragraphs[0][:200],
        "closing": paragraphs[-1][:200],
        "picks": picks,
    })
    for topic, block in tldr.items():
        if block.get("status") == "ok":
            history.setdefault("tldr_used", {})[topic] = block["issue_date"]
    prune(history)
    save_history(history)
    build_feed(history)
    log("Feed updated.")


if __name__ == "__main__":
    main()

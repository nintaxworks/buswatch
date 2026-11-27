"""Telegram bus-ticket watcher for rathimeena.in, designed to run from GitHub Actions.

MODE=commands : read new Telegram messages and act on them (add/list/stop/check watches).
MODE=full     : commands + check every watch (alert if open, else "open till" once a day).
State (watch list + Telegram offset) lives in state.json, committed back by the workflow.
"""
import difflib
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

BASE = "https://www.rathimeena.in"
IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
HDR = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}

HELP = (
    "Commands:\n"
    "/watch Chennai to Kumbakonam 13-01-2027  – start watching a route + date\n"
    "   (also works: /watch Chennai Kumbakonam 13 Jan)\n"
    "/list – show active watches\n"
    "/check 2 (or /check all) – check now\n"
    "/stop 2 (or /stop all) – remove a watch\n\n"
    "Every 3 hours each watch is checked: you get an alert if buses are open, "
    "otherwise the 'booking open till' date once a day."
)


# ----------------------------- Telegram --------------------------------------
def tg(method: str, **payload):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
        json=payload, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def say(text: str):
    tg("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text,
       disable_web_page_preview=True)


# ----------------------------- State -----------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    s.setdefault("offset", 0)
    s.setdefault("next_id", 1)
    s.setdefault("watches", [])
    return s


def save_state(s: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


# ----------------------------- Site ------------------------------------------
_stations = None


def stations() -> dict:
    """code -> {name, code, toRoutesList}; the homepage embeds the full list."""
    global _stations
    if _stations is None:
        h = requests.get(BASE + "/", headers=HDR, timeout=30).text
        m = re.search(r'<input[^>]*id="routes-data"[^>]*data-value="([^"]*)"', h)
        _stations = json.loads(html.unescape(m.group(1)))["route"]
    return _stations


def resolve(name: str) -> list:
    """Station codes matching a typed name (exact > prefix > substring > fuzzy)."""
    by = {v["name"].lower(): k for k, v in stations().items()}
    n = name.strip().lower()
    if n in by:
        return [by[n]]
    hits = [c for nm, c in by.items() if nm.startswith(n)] or [c for nm, c in by.items() if n in nm]
    if not hits:
        hits = [by[m] for m in difflib.get_close_matches(n, by, n=3, cutoff=0.7)]
    return hits


def station_name(code: str) -> str:
    return stations()[code]["name"]


def trips_on(from_code: str, to_code: str, d: date) -> int:
    r = requests.post(
        f"{BASE}/search/search-list",
        data={"fromStationCode": from_code, "toStationCode": to_code,
              "onwardDate": d.isoformat(), "returnDate": "", "searchType": "",
              "reschedule": 0, "tripCode": ""},
        headers=HDR, timeout=30,
    )
    r.raise_for_status()
    data = r.json().get("data")
    if isinstance(data, dict) and data.get("reg"):
        return data["reg"][0].get("tripLength", 1)
    return 0


def last_open_date(from_code, to_code, closed_target: date, today: date):
    """Latest bookable date before closed_target (window is contiguous from today)."""
    lo, hi = today + timedelta(days=1), closed_target  # hi known closed
    if lo >= hi or not trips_on(from_code, to_code, lo):
        return None
    while (hi - lo).days > 1:
        mid = lo + (hi - lo) // 2
        time.sleep(0.5)
        if trips_on(from_code, to_code, mid):
            lo = mid
        else:
            hi = mid
    return lo


# ----------------------------- Parsing ---------------------------------------
def parse_date(s: str, today: date):
    s = s.strip().replace(",", "")
    for f in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    for f in ("%d %b", "%d %B"):
        try:
            d = datetime.strptime(f"{s} {today.year}", f + " %Y").date()
            return d if d >= today else d.replace(year=today.year + 1)
        except ValueError:
            pass
    return None


def parse_watch(arg: str, today: date):
    """'Chennai to Kumbakonam 13-01-2027' -> (from, to, date) or an error string."""
    toks = arg.split()
    d, route = None, None
    for k in (3, 2, 1):
        if len(toks) > k:
            d = parse_date(" ".join(toks[-k:]), today)
            if d:
                route = " ".join(toks[:-k])
                break
    if not d:
        return "I couldn't read the date. Try 13-01-2027 or 13 Jan."
    parts = [p.strip() for p in re.split(r"\s+to\s+|\s*->\s*|\s*→\s*|\s*>\s*|\s*,\s*", route, flags=re.I) if p.strip()]
    if len(parts) == 1 and len(parts[0].split()) == 2:
        parts = parts[0].split()
    if len(parts) != 2:
        return "I couldn't tell the two places apart. Use: /watch Chennai to Kumbakonam 13-01-2027"
    return parts[0], parts[1], d


# ----------------------------- Watch logic -----------------------------------
def label(w) -> str:
    return f"#{w['id']} {w['from']} → {w['to']} on {date.fromisoformat(w['date']).strftime('%d %b %Y')}"


def link(w) -> str:
    slug = lambda x: x.lower().replace(" ", "-")
    return (f"{BASE}/search/{slug(w['from'])}-to-{slug(w['to'])}?fromStationCode={w['from_code']}"
            f"&toStationCode={w['to_code']}&onwardDate={w['date']}")


def check_watch(w, today: date, always_summary: bool):
    """Alert if open; otherwise send the 'open till' note (once a day unless always_summary)."""
    target = date.fromisoformat(w["date"])
    n = trips_on(w["from_code"], w["to_code"], target)
    if n:
        say(f"🚌 OPEN: {label(w)} – {n} bus(es) available.\n{link(w)}")
        return
    if always_summary or w.get("last_summary") != today.isoformat():
        last = last_open_date(w["from_code"], w["to_code"], target, today)
        if last:
            say(f"ℹ️ {label(w)} is not open yet. Booking is open till {last.strftime('%a, %d %b %Y')}.")
        else:
            say(f"ℹ️ {label(w)} is not open yet, and no earlier dates are open for this route either.")
        w["last_summary"] = today.isoformat()


def pick(name: str, what: str):
    codes = resolve(name)
    if len(codes) == 1:
        return codes[0], None
    if not codes:
        return None, f"I don't know a {what} place called '{name}'."
    return None, f"'{name}' matches several places: " + ", ".join(station_name(c) for c in codes[:6]) + ". Please be more specific."


def handle(text: str, state: dict, today: date):
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    watches = state["watches"]

    if cmd in ("/start", "/help"):
        say(HELP)
    elif cmd == "/list":
        say("\n".join(label(w) for w in watches) if watches else "No active watches. Add one with /watch.")
    elif cmd == "/watch":
        parsed = parse_watch(arg, today)
        if isinstance(parsed, str):
            return say(parsed)
        a, b, d = parsed
        if d < today:
            return say("That date is in the past.")
        fc, err = pick(a, "departure")
        if err:
            return say(err)
        tc, err = pick(b, "destination")
        if err:
            return say(err)
        if any(w["from_code"] == fc and w["to_code"] == tc and w["date"] == d.isoformat() for w in watches):
            return say("You're already watching that route and date.")
        w = {"id": state["next_id"], "from": station_name(fc), "to": station_name(tc),
             "from_code": fc, "to_code": tc, "date": d.isoformat()}
        state["next_id"] += 1
        watches.append(w)
        warn = "" if tc in stations()[fc].get("toRoutesList", {}) else "\n⚠️ The site doesn't list this exact route, so it may never open."
        say(f"✅ Watching {label(w)}.{warn}\nChecking now…")
        check_watch(w, today, always_summary=True)
    elif cmd in ("/stop", "/check"):
        if arg.lower() == "all":
            chosen = list(watches)
        elif arg.isdigit():
            chosen = [w for w in watches if w["id"] == int(arg)]
        else:
            return say(f"Usage: {cmd} <number> or {cmd} all  (see /list)")
        if not chosen:
            return say("No such watch. See /list.")
        for w in chosen:
            if cmd == "/stop":
                watches.remove(w)
                say(f"🛑 Stopped {label(w)}.")
            else:
                check_watch(w, today, always_summary=True)
    else:
        say(HELP)


def process_commands(state: dict, today: date):
    payload = {"timeout": 0, "allowed_updates": ["message"]}
    if state["offset"]:
        payload["offset"] = state["offset"] + 1
    for u in tg("getUpdates", **payload).get("result", []):
        state["offset"] = max(state["offset"], u["update_id"])
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(os.environ["TELEGRAM_CHAT_ID"]):
            continue  # ignore anyone but you
        try:
            handle((msg.get("text") or "").strip(), state, today)
        except Exception as e:  # keep going; tell the user
            print("command error:", repr(e))
            say(f"⚠️ Something went wrong with that command: {e}")


def run_watchlist(state: dict, today: date, force: bool):
    for w in list(state["watches"]):
        if date.fromisoformat(w["date"]) < today:
            state["watches"].remove(w)
            say(f"⌛ {label(w)} has passed, so I removed it.")
    errors = []
    for w in state["watches"]:
        try:
            check_watch(w, today, always_summary=force)
        except Exception as e:
            print("check error:", w["id"], repr(e))
            errors.append(repr(e))
        time.sleep(1)
    if errors and state.get("last_error_day") != today.isoformat():
        state["last_error_day"] = today.isoformat()
        say(f"⚠️ {len(errors)} check(s) failed (the site may be blocking GitHub or be down). Last error: {errors[-1]}")


def main():
    mode = os.environ.get("MODE", "commands")
    force = os.environ.get("FORCE_SUMMARY") == "1"
    today = datetime.now(IST).date()
    state = load_state()
    process_commands(state, today)
    save_state(state)  # keep the Telegram offset even if the checks below fail
    if mode == "full":
        run_watchlist(state, today, force)
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

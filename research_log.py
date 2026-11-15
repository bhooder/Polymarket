"""Research logs (added 2026-11-15): history for testing future strategy ideas.

Runs once at the very end of each forward-test run, after all betting, saving and reporting.
It makes NO web requests: it only writes down things the run had already downloaded or
decided. It writes only inside the research/ folder and never touches a trading file.
No strategy and no real bet reads anything in research/.

Files (plain CSV, append-only, one small file per UTC hour: research/<log>/<day>/<hour>.csv; times are UTC):
  research/graded/      every trader position the report card graded: wallet, market, entry, close, CLV
  research/positions/   a row when a tracked trader position's SHARES or COST is seen to change
  research/markets/     what each market code "m" in the position log stands for (token, game, outcome, start)
  research/decisions/   per strategy and pick: bet, or the rule that stopped it, with the venue prices seen
  research/snapshots/   a pick's counts and price whenever they change (not only the first and last look)
  research/price_paths/ prices at fixed times before the start, from the closing-price request
  research/leaderboard/ the leaderboard once a day, and every change in whether a trader could be scanned
  research/rules.csv    each strategy's settings whenever they change (decisions carry the version)
  research/runs.csv     one row per run: how long it took, how much was logged, folder size, problems
  research/state.json   what was last seen, so only changes are written (not history; safe to lose)

Reading these honestly:
- "observed_at" is when WE looked. A change in shares happened some time between "prev_observed_at"
  and "observed_at". A trade time is only given when the trader's own trade list (already downloaded
  for the bot check) shows trades in that market in that gap: "trade_evidence" = confirmed.
- A position we stop seeing is "not_observed", never "sold": it may have fallen under the $100
  cut-off, the trader may have hedged (those markets are dropped), or the trader wasn't scanned.
- A trader's record "as of" a moment must use only rows graded before it: see record_as_of().
- Several markets on one game are not independent: every row carries the game, count per game.
"""
import copy
import csv
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pick_log

FOLDER = "research"
MAX_ROWS = 50_000         # most rows one log may add in one run (a runaway bug must not fill the repo)
MAX_FILE = 20_000_000     # never add to a file already this big (GitHub refuses pushes with huge files,
                          # and a refused push would lose that run's trading results too)
TRACK_DAYS = 3            # position changes: games starting within this many days (the bet window)
PRICE_STEP = 0.01         # a pick snapshot is written when its price or entry moves this much...
PCT_STEP = 2.0            # ...or its average bankroll % moves this much, or any count changes
PATH_MINUTES = (360, 180, 60, 30, 15, 5)  # price path: minutes before the start
SHARE_TOL, COST_TOL = 0.01, 0.01          # smaller differences are rounding, not a change

# "oid" = a stable observation id. If a run's rows are ever written twice (see read_log), the copies
# carry the same oid, so they can be recognised and dropped.
GRADED_COLS = ["oid", "graded_at", "wallet", "name", "token_id", "game", "sport", "market", "pick", "start",
               "first_seen", "entry", "close", "clv", "close_source"]
# To keep the files small, rows name a trader by "w" (the first 12 characters of the wallet; the full
# wallet is in the leaderboard log) and a market outcome by "m" (a 12-character code; research/markets/
# and each snapshot's first_seen row give the full token, market, game and start for that code).
POSITION_COLS = ["oid", "observed_at", "prev_observed_at", "w", "bot_speed", "m", "change", "shares", "shares_delta", "cost",
                 "cost_delta", "avg_price", "value", "price", "trader_open_value", "pct_of_open_value",
                 "position_first_entry_at", "not_observed_reason", "last_value", "trade_evidence",
                 "trades_in_gap", "trade_first_at", "trade_last_at", "trade_shares"]
MARKET_COLS = ["oid", "observed_at", "m", "token_id", "condition_id", "game", "outcome", "start"]
DECISION_COLS = ["oid", "observed_at", "strategy", "rules", "m", "token_id", "game", "market", "pick",
                 "code", "prev_code", "stake", "dollars", "bettors", "against", "bankroll_pct", "entry", "price", "venue",
                 "quotes"]
SNAPSHOT_COLS = (["oid", "observed_at", "scope", "m", "reason", "token_id", "condition_id", "game", "sport", "market",
                  "pick", "start"] + list(pick_log.WIDE_SNAP.values()))
PATH_COLS = (["oid", "observed_at", "token_id", "start", "points", "first_point_at"]
             + [f"p_{m}m" for m in PATH_MINUTES] + ["close", "source"])
BOARD_COLS = ["oid", "observed_at", "reason", "w", "wallet", "name", "rank", "pnl", "volume", "status",
              "prev_status", "status_detail", "crypto_share", "trades_per_hour", "bot_speed", "open_value", "positions", "hedged_markets"]
RULES_COLS = ["oid", "observed_at", "strategy", "rules", "settings"]
RUN_COLS = ["observed_at", "run_seconds", "research_seconds", "board", "scanned", "skipped_bot", "errors",
            "picks", "wide_picks", "graded", "positions", "markets", "decisions", "snapshots", "price_paths",
            "leaderboard", "rules", "tracked_positions", "research_bytes", "state_bytes", "trade_fields", "notes"]


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_unix(ts):
    """Unix time (seconds, or milliseconds) -> UTC ISO text, or '' if it isn't a usable number."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if ts > 1e11:
        ts /= 1000
    return iso(datetime.fromtimestamp(ts, timezone.utc)) if ts > 0 else ""


def parse(text):
    if not text:
        return None
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def game_of(slug_or_link):
    return (slug_or_link or "").rstrip("/").rsplit("/", 1)[-1].replace("-more-markets", "")


def short(token):
    return hashlib.sha1(str(token).encode()).hexdigest()[:12]


def oid(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def append(here, kind, when, cols, rows):
    """Add rows to research/<kind>/<UTC day>/<UTC hour>.csv (or research/<kind>.csv when `when` is None).
    One small file per hour: a finished hour's file never changes again, which Git stores cheaply.
    Refuses to write absurd amounts, and starts on a fresh line if an earlier write was cut short."""
    if not rows:
        return 0
    if len(rows) > MAX_ROWS:
        raise ValueError(f"{len(rows)} rows in one run is over the {MAX_ROWS} limit; nothing written")
    path = os.path.join(here, FOLDER, f"{kind}.csv" if when is None else os.path.join(
        kind, when.strftime("%Y-%m-%d"), when.strftime("%H") + ".csv"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    torn = False
    if not new:
        if os.path.getsize(path) > MAX_FILE:
            raise ValueError(f"{os.path.relpath(path, here)} is over {MAX_FILE} bytes; nothing written")
        with open(path, "rb") as f:
            if os.path.getsize(path):
                f.seek(-1, os.SEEK_END)
                torn = f.read(1) != b"\n"  # an earlier write stopped mid-row
    with open(path, "a", newline="") as f:
        if torn:
            f.write("\r\n")
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


def read_log(here, kind):
    """Every row of a log, oldest first, without repeats. Rows cut short by an interrupted write are
    skipped, and a row whose oid was already seen (the same observation written twice) is dropped."""
    base = os.path.join(here, FOLDER)
    single = os.path.join(base, kind + ".csv")
    paths = [single] if os.path.exists(single) else sorted(
        os.path.join(root, f) for root, _, files in os.walk(os.path.join(base, kind)) for f in files
        if f.endswith(".csv"))
    out, seen = [], set()
    for path in paths:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if None in row or None in row.values() or not row.get("oid") or row["oid"] in seen:
                    continue  # wrong number of fields (a torn row), or a repeat
                seen.add(row["oid"])
                out.append(row)
    return out


SECTIONS = {"positions": dict, "looked": dict, "decisions": dict, "snapshots": dict, "paths": dict,
            "traders": dict, "rules": dict, "ready": dict, "board_day": str, "epoch": str}


def fresh_state(now):
    return {k: t() for k, t in SECTIONS.items()} | {"epoch": iso(now)}


def load_state(here, now):
    """(state, problem). A missing file is a normal first run. An unreadable or wrongly shaped file
    starts again from nothing (the CSV history is untouched); malformed entries are dropped.
    "epoch" marks when this state began: it is part of every oid, so ids never repeat across a restart.
    "ready" lists the logs that have been written at least once since then (see "baseline")."""
    path = os.path.join(here, FOLDER, "state.json")
    if not os.path.exists(path):
        return fresh_state(now), ""
    try:
        with open(path) as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict) or not loaded.get("epoch"):
            raise ValueError("not a research state")
        state = fresh_state(now)
        for key, kind in SECTIONS.items():
            if isinstance(loaded.get(key), kind):
                state[key] = loaded[key]
        ok = lambda v: isinstance(v, dict) and all(isinstance(v.get(k), (int, float)) for k in ("s", "c", "t"))
        state["positions"] = {k: v for k, v in state["positions"].items() if "|" in k and ok(v)}
        state["decisions"] = {k: v for k, v in state["decisions"].items() if isinstance(v, list) and len(v) == 3}
        state["snapshots"] = {k: v for k, v in state["snapshots"].items()
                              if isinstance(v, dict) and isinstance(v.get("v"), list) and len(v["v"]) == 11}
        state["traders"] = {k: v for k, v in state["traders"].items() if isinstance(v, list) and len(v) == 2}
        return state, ""
    except Exception as e:
        return fresh_state(now), f"state reset ({type(e).__name__})"


def save_state(here, state):
    path = os.path.join(here, FOLDER, "state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(state, f, separators=(",", ":"), sort_keys=True)
    os.replace(path + ".tmp", path)


# ---------------- 1. graded positions ----------------

def graded_rows(graded):
    rows = []
    for g in graded:
        game = game_of(g.get("game"))
        rows.append({"oid": oid("graded", g["wallet"], g["token_id"], g.get("start"), g.get("first_seen")),
                     "graded_at": iso(parse(g["graded_at"])), "wallet": g["wallet"], "name": g.get("name"),
                     "token_id": g["token_id"], "game": game, "sport": game.split("-")[0],
                     "market": g.get("market"), "pick": g.get("pick"), "start": g.get("start"),
                     "first_seen": g.get("first_seen"), "entry": g.get("entry"),
                     "close": "" if g.get("close") is None else round(g["close"], 4),
                     "clv": "" if g.get("clv") is None else round(g["clv"], 4),
                     "close_source": g.get("close_source")})
    return rows


def record_as_of(rows, wallet, when, sport=None):
    """A trader's closing-price record using ONLY positions graded before `when` (no future data).
    rows: graded-log rows (dicts). Each game counts once (the average CLV of the trader's positions
    in it), because several markets on one game are not independent.
    Returns {"games", "avg_clv", "t", "days"}; avg_clv and t are None when there is too little."""
    when = parse(when) if isinstance(when, str) else when
    games = {}
    for r in rows:
        if r["wallet"] != wallet or r.get("clv") in ("", None) or parse(r["graded_at"]) >= when:
            continue
        if sport and r.get("sport") != sport:
            continue
        games.setdefault(r["game"], []).append(float(r["clv"]))
    vals = [sum(v) / len(v) for v in games.values()]
    n = len(vals)
    days = len({parse(r["start"]).date() for r in rows if r["wallet"] == wallet and r["game"] in games
                and r.get("start")})
    if not n:
        return {"games": 0, "avg_clv": None, "t": None, "days": 0}
    mean = sum(vals) / n
    t = None
    if n > 1:
        sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5
        t = mean / (sd / n ** 0.5) if sd else None
    return {"games": n, "avg_clv": mean, "t": t, "days": days}


# ---------------- 2. position changes ----------------

TRADE_TOKEN_KEYS = ("token_id", "asset", "asset_id", "tokenId")  # possible names of the market field
TRADE_SIZE_KEYS = ("size", "shares", "amount")


def trade_evidence(trades, token, after, until):
    """Trades in this market from the trader's own trade list, made after `after` (None = any time) and
    no later than `until`. Returns (evidence, count, first time, last time, signed shares).
    evidence: confirmed = such trades are in the list; none_in_list = the list was read but shows none
    (it only holds the latest 500 trades); unavailable = no list, or it doesn't name the market."""
    if not trades:
        return "unavailable", "", "", "", ""
    key = next((k for k in TRADE_TOKEN_KEYS if any(k in t for t in trades)), None)
    if key is None:
        return "unavailable", "", "", "", ""
    hits = []
    for t in trades:
        when = parse(from_unix(t.get("timestamp")))
        if str(t.get(key)) != str(token) or when is None or when > until or (after and when <= after):
            continue
        size = next((t[k] for k in TRADE_SIZE_KEYS if t.get(k) is not None), None)
        side = str(t.get("side") or "").upper()
        try:
            signed = float(size) * (-1 if side.startswith("S") else 1)
        except (TypeError, ValueError):
            signed = None
        hits.append((when, signed))
    if not hits:
        return "none_in_list", 0, "", "", ""
    known = [s for _, s in hits if s is not None]
    return ("confirmed", len(hits), iso(min(h[0] for h in hits)), iso(max(h[0] for h in hits)),
            round(sum(known), 2) if len(known) == len(hits) else "")


def wshort(wallet):
    return str(wallet)[:12]


def bot_flag(obs, wallet):
    """1 = trades at bot speed (over the report card's limit), 0 = slower, '' = pace unknown."""
    pace, limit = (obs.get("pace") or {}).get(wallet), obs.get("max_pace")
    return "" if pace is None or not limit else int(pace > limit)


def position_rows(state, obs, now):
    """(position rows, market rows): tracked positions whose shares or cost changed, positions no longer
    seen, and a market row the first time any trader is seen holding that outcome.
    Only traders scanned successfully this run are compared: for anyone else nothing is concluded.
    prev_observed_at = the previous time that trader's positions were read."""
    markets = (obs.get("card") or {}).get("markets") or {}
    traders, trades = obs["scan"].get("traders") or {}, obs["scan"].get("trades") or {}
    seen, looked, epoch = state["positions"], state["looked"], state["epoch"]
    first = "first_seen" if state["ready"].get("positions") else "baseline"  # baseline = already held when
    # tracking began (first run, or after a state restart): not evidence that it was just bought
    rows, market_rows, stamp, horizon = [], [], iso(now), now + timedelta(days=TRACK_DAYS)
    for key in [k for k, v in seen.items() if v["t"] <= now.timestamp()]:
        del seen[key]  # the game has started: nothing more to track
    known = {k.split("|")[1] for k in seen}
    for wallet, (name, positions) in (obs.get("positions") or {}).items():
        info = traders.get(wallet) or {}
        if info.get("status") != "scanned":
            continue
        w, prev, bot = wshort(wallet), looked.get(wshort(wallet), ""), bot_flag(obs, wallet)
        current = set()
        for p in positions:
            start = parse((markets.get(p.get("condition_id")) or {}).get("start"))
            token = p.get("token_id")
            if not token or not start or not now < start <= horizon:
                continue
            m = short(token)
            key = f"{w}|{m}"
            current.add(key)
            if m not in known:
                known.add(m)
                market_rows.append({"oid": oid("markets", m, iso(start)), "observed_at": stamp, "m": m,
                                    "token_id": token,
                                    "condition_id": p.get("condition_id"), "game": game_of(p.get("event_slug")),
                                    "outcome": p.get("outcome"), "start": iso(start)})
            shares, cost = float(p.get("current_size") or 0), float(p.get("entry_cost_usdc") or 0)
            value, open_value = p.get("current_value"), info.get("bankroll")
            old = seen.get(key)
            if old and old.get("g"):
                change = "reappeared"
            elif not old:
                change = first
            elif abs(shares - old["s"]) > SHARE_TOL:
                change = "shares_up" if shares > old["s"] else "shares_down"
            elif abs(cost - old["c"]) > COST_TOL:
                change = "cost_changed"
            else:
                change = None  # same shares and cost: any change in value is the price moving, not a trade
            n = (old or {}).get("n", 0) + (1 if change else 0)
            if change:
                ev = trade_evidence(trades.get(wallet), token, parse(prev), now)
                rows.append({
                    "oid": oid("positions", key, epoch, n), "observed_at": stamp, "prev_observed_at": prev,
                    "w": w, "bot_speed": bot, "m": m, "change": change,
                    "shares": round(shares, 2), "shares_delta": round(shares - old["s"], 2) if old else "",
                    "cost": round(cost, 2), "cost_delta": round(cost - old["c"], 2) if old else "",
                    "avg_price": p.get("avg_price"), "value": value, "price": p.get("current_price"),
                    "trader_open_value": open_value,
                    "pct_of_open_value": round(100 * value / open_value, 2) if value and open_value else "",
                    "position_first_entry_at": p.get("first_entry_at") or "", "trade_evidence": ev[0],
                    "trades_in_gap": ev[1], "trade_first_at": ev[2], "trade_last_at": ev[3],
                    "trade_shares": ev[4]})
            seen[key] = {"s": round(shares, 2), "c": round(cost, 2), "v": value, "t": int(start.timestamp()),
                         "h": short(p.get("condition_id")), "n": n}
        hedged = {short(c) for c in info.get("hedged") or ()}
        for key, old in seen.items():  # tracked before, not in this trader's list now
            if not key.startswith(w + "|") or key in current or old.get("g"):
                continue
            m = key.split("|")[1]
            old["n"] = old.get("n", 0) + 1
            rows.append({
                "oid": oid("positions", key, epoch, old["n"]), "observed_at": stamp, "prev_observed_at": prev,
                "w": w, "bot_speed": bot, "m": m, "change": "not_observed",
                "trader_open_value": info.get("bankroll"), "last_value": old.get("v"),
                "not_observed_reason": ("market_dropped_as_hedged" if old.get("h") in hedged
                                        else "not_in_positions_over_min_value"),
                "trade_evidence": "unavailable"})  # the full token isn't kept once a position is gone
            old["g"] = 1
        looked[w] = stamp
    return rows, market_rows


# ---------------- 3. decisions ----------------

def rule_versions(state, rules, now):
    """Short version stamp per strategy; a row in rules.csv whenever a strategy's settings change."""
    stamps, rows = {}, []
    for key, settings in sorted((rules or {}).items()):
        text = json.dumps(settings, sort_keys=True, default=str)
        stamps[key] = hashlib.sha1(text.encode()).hexdigest()[:8]
        if state["rules"].get(key) != stamps[key]:
            rows.append({"oid": oid("rules", key, stamps[key], state["rules"].get(key), state["epoch"]),
                         "observed_at": iso(now), "strategy": key, "rules": stamps[key], "settings": text})
            state["rules"][key] = stamps[key]
    return stamps, rows


def decision_rows(state, decisions, stamps, now):
    """One row when a strategy's verdict on a pick is first seen or changes, or when the best venue
    price it was offered moves a cent. Picks no longer listed are forgotten."""
    rows, live = [], set()
    for d in decisions or []:
        if not d.get("token_id") or not d.get("strategy") or d.get("code") == "no_tier":
            continue  # "no_tier" (too few bettors / too little conviction) is not logged: it follows
            # directly from the pick's counts in the snapshot log and the tiers in rules.csv
        key = f'{d["strategy"]}|{short(d["token_id"])}'
        live.add(key)
        quotes = d.get("quotes") or []
        best = min((q[1] for q in quotes), default=None)
        last = state["decisions"].get(key)
        moved = best is not None and (not last or last[1] is None or abs(best - last[1]) >= PRICE_STEP - 1e-9)
        if last and last[0] == d["code"] and not moved:
            continue
        n = (last[2] if last else 0) + 1
        state["decisions"][key] = [d["code"], best if best is not None else (last[1] if last else None), n]
        rows.append({**d, "oid": oid("decisions", key, state["epoch"], n), "prev_code": last[0] if last else "",
                     "observed_at": iso(now), "rules": stamps.get(d["strategy"], ""), "m": short(d["token_id"]),
                     "game": game_of(d.get("link")),
                     "quotes": ";".join(f"{v}:{p}:{c}" for v, p, c in quotes)})
    for key in [k for k in state["decisions"] if k not in live]:
        del state["decisions"][key]
    return rows


# ---------------- 4. price paths ----------------

def path_rows(state, history, now):
    rows, day = [], now.date().isoformat()
    for token, (start_ts, points) in (history or {}).items():
        key = short(token)
        if key in state["paths"]:
            continue
        state["paths"][key] = day
        row = {"oid": oid("price_paths", token, start_ts), "observed_at": iso(now), "token_id": token,
               "start": from_unix(start_ts), "points": len(points),
               "first_point_at": from_unix(points[0][0]) if points else "",
               "close": points[-1][1] if points else "", "source": "clob /prices-history, 1-minute, 6h before start"}
        for m in PATH_MINUTES:
            before = [p for t, p in points if t <= start_ts - 60 * m]
            row[f"p_{m}m"] = before[-1] if before else ""
        rows.append(row)
    cutoff = (now - timedelta(days=4)).date().isoformat()
    for key in [k for k, d in state["paths"].items() if not isinstance(d, str) or d < cutoff]:
        del state["paths"][key]
    return rows


# ---------------- 5. pick snapshots ----------------

COUNTS = ("bettors", "against", "conv_bettors", "conv_against", "sharp_bettors", "sharp_against",
          "bettors_top100", "against_top100")


def snapshot_rows(state, obs, now):
    """A row for each game pick when first seen and whenever its counts, price, entry or bankroll %
    change. scope: top100 = the picks the strategies use; top200 = the wider research picks."""
    markets = (obs.get("card") or {}).get("markets") or {}
    rows, live, skipped = [], set(), 0
    first = "first_seen" if state["ready"].get("snapshots") else "baseline"  # see position_rows
    for scope, picks, only in (("top100", obs.get("picks") or [], obs.get("top_wallets")),
                               ("top200", obs.get("wide_picks") or [], None)):
        try:
            picks = pick_log.add_card(picks, obs.get("card") or {}, obs.get("sides") or {}, only)
        except Exception:
            pass  # report-card columns unavailable this run: log the picks without them
        for p in picks:
            start = parse((markets.get(p.get("condition_id")) or {}).get("start"))
            if not p.get("token_id") or not start:
                skipped += 1  # no game start known: not a game, or not looked up
                continue
            if start <= now:
                continue
            snap = {v: p.get(k) for k, v in pick_log.WIDE_SNAP.items()}
            key = f'{scope}|{short(p["token_id"])}'
            live.add(key)
            had = state["snapshots"].get(key)
            last = had["v"] if had else None
            now_vals = [snap.get(c) for c in COUNTS] + [snap.get("price"), snap.get("entry"),
                                                       snap.get("bankroll_pct")]
            if not last:
                reason = first
            elif last[:len(COUNTS)] != now_vals[:len(COUNTS)]:
                reason = "counts"
            elif _moved(last[-3], now_vals[-3], PRICE_STEP):
                reason = "price"
            elif _moved(last[-2], now_vals[-2], PRICE_STEP):
                reason = "entry"
            elif _moved(last[-1], now_vals[-1], PCT_STEP):
                reason = "bankroll_pct"
            else:
                continue
            n = (had["n"] if had else 0) + 1
            state["snapshots"][key] = {"v": now_vals, "n": n}
            row = {**snap, "oid": oid("snapshots", key, state["epoch"], n), "observed_at": iso(now),
                   "scope": scope, "m": short(p["token_id"]), "reason": reason}
            if not last:  # the full description once; later rows carry only the code "m"
                game = game_of(p.get("Link"))
                row.update(token_id=p["token_id"], condition_id=p.get("condition_id"), game=game,
                           sport=game.split("-")[0], market=p.get("Market"), pick=p.get("Pick"), start=iso(start))
            rows.append(row)
    for key in [k for k in state["snapshots"] if k not in live]:
        del state["snapshots"][key]
    return rows, skipped


def _moved(old, new, step):
    if old is None or new is None:
        return old != new
    return abs(new - old) >= step - 1e-9


# ---------------- 6. leaderboard ----------------

def clean(text):
    """An error message without any web address details (nothing after a '?'), kept short."""
    words = [w.split("?")[0] + ("?..." if "?" in w else "") for w in str(text or "").split()]
    return " ".join(words)[:120]


def board_rows(state, obs, now):
    """The whole leaderboard on the first run of each UTC day; after that, only traders whose status
    changed (scanned / skipped_bot / error / off_board). 'error' = their data could not be read.
    bot_speed marks traders over the report card's trades-per-hour limit, so research can leave them out."""
    scan, day, stamp, epoch = obs["scan"], now.date().isoformat(), iso(now), state["epoch"]
    traders, rows, full = scan.get("traders") or {}, [], state.get("board_day") != day
    known = dict(state["traders"])
    on_board = {}
    for i, t in enumerate(scan.get("board") or [], 1):
        wallet = t.get("user_id") or t.get("proxy_wallet")
        info = traders.get(wallet) or {}
        status = info.get("status") or "not_reached"
        was = known.get(wallet) or ["", 0]
        changed = was[0] != status
        on_board[wallet] = [status, was[1] + (1 if changed else 0)]
        if full or changed:
            pace = (obs.get("pace") or {}).get(wallet)
            rows.append({"oid": oid("leaderboard", wallet, epoch, on_board[wallet][1], day if full else ""),
                         "observed_at": stamp, "reason": "daily" if full else "status_change",
                         "w": wshort(wallet), "wallet": wallet,
                         "name": t.get("user_name") or wallet, "rank": t.get("rank", i), "pnl": t.get("pnl"),
                         "volume": t.get("volume"), "status": status, "prev_status": was[0],
                         "status_detail": clean(info.get("reason")),
                         "crypto_share": info.get("crypto_share", ""),
                         "trades_per_hour": round(pace, 1) if pace is not None else "",
                         "bot_speed": bot_flag(obs, wallet),
                         "open_value": info.get("bankroll", ""), "positions": info.get("raw_positions", ""),
                         "hedged_markets": len(info["hedged"]) if "hedged" in info else ""})
    if not on_board:
        return rows  # no leaderboard this run: conclude nothing about anyone
    for wallet, was in known.items():
        if wallet in on_board:
            continue
        if was[0] != "off_board":
            on_board[wallet] = ["off_board", was[1] + 1]
            rows.append({"oid": oid("leaderboard", wallet, epoch, was[1] + 1, ""), "observed_at": stamp,
                         "reason": "status_change", "w": wshort(wallet), "wallet": wallet,
                         "status": "off_board", "prev_status": was[0]})
        else:
            on_board[wallet] = was
    state["traders"], state["board_day"] = on_board, day
    return rows


# ---------------- the run ----------------

def folder_bytes(here):
    total = 0
    for root, _, files in os.walk(os.path.join(here, FOLDER)):
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    return total


def run(here, obs, now=None):
    """Write this run's research rows. Returns a short summary.
    Each log stands alone: its rows are worked out on a COPY of its part of the state, and that part is
    only kept if the rows were written. So a failed write is simply tried again next run (with the same
    oids), a problem in one log never loses or repeats another, and nothing here can raise."""
    began = time.time()
    now = now or datetime.now(timezone.utc)
    state, problem = load_state(here, now)
    notes, counts, stamps = ([problem] if problem else []), {}, {}

    def log(name, sections, build):
        """build(view) -> [(kind, when, cols, rows), ...]; `view` is the state with copies of `sections`."""
        try:
            view = {**state, **{k: copy.deepcopy(state[k]) for k in sections}}
            for kind, when, cols, rows in build(view):
                counts[kind] = append(here, kind, when, cols, rows)
            for k in sections:
                state[k] = view[k]
            state["ready"][name] = 1
        except Exception as e:
            counts.setdefault(name, "")
            notes.append(f"{name}: {type(e).__name__}: {clean(e)}")

    def rules(view):
        found, rows = rule_versions(view, obs.get("rules"), now)
        stamps.update(found)
        return [("rules", None, RULES_COLS, rows)]

    def positions(view):
        rows, market_rows = position_rows(view, obs, now)
        return [("markets", now, MARKET_COLS, market_rows), ("positions", now, POSITION_COLS, rows)]

    def snapshots(view):
        rows, skipped = snapshot_rows(view, obs, now)
        if skipped:
            notes.append(f"{skipped} picks without a known game start")
        return [("snapshots", now, SNAPSHOT_COLS, rows)]

    log("rules", ["rules"], rules)
    log("graded", [], lambda v: [("graded", now, GRADED_COLS, graded_rows(obs.get("graded") or []))])
    log("positions", ["positions", "looked"], positions)
    log("decisions", ["decisions"], lambda v: [
        ("decisions", now, DECISION_COLS, decision_rows(v, obs.get("decisions"), stamps, now))])
    log("snapshots", ["snapshots"], snapshots)
    log("price_paths", ["paths"], lambda v: [("price_paths", now, PATH_COLS, path_rows(v, obs.get("history"), now))])
    log("leaderboard", ["traders", "board_day"], lambda v: [
        ("leaderboard", now, BOARD_COLS, board_rows(v, obs, now))])
    try:
        save_state(here, state)
    except Exception as e:
        notes.append(f"state not saved: {type(e).__name__}")  # next run repeats this run's rows, same oids

    try:
        scan = obs.get("scan") or {}
        statuses = [t.get("status") for t in (scan.get("traders") or {}).values()]
        state_path = os.path.join(here, FOLDER, "state.json")
        append(here, "runs", None, RUN_COLS, [{
            "observed_at": iso(now),
            "run_seconds": round(began - obs["run_started"], 1) if obs.get("run_started") else "",
            "research_seconds": round(time.time() - began, 2), "board": len(scan.get("board") or []),
            "scanned": statuses.count("scanned"), "skipped_bot": statuses.count("skipped_bot"),
            "errors": statuses.count("error"), "picks": len(obs.get("picks") or []),
            "wide_picks": len(obs.get("wide_picks") or []), **counts,
            "tracked_positions": sum(not v.get("g") for v in state["positions"].values()),
            "research_bytes": folder_bytes(here),
            "state_bytes": os.path.getsize(state_path) if os.path.exists(state_path) else 0,
            "trade_fields": " ".join(scan.get("trade_fields") or []), "notes": " | ".join(notes)}])
    except Exception as e:
        notes.append(f"runs: {type(e).__name__}")
    done = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    return (done or "nothing new") + (f" ({'; '.join(notes)})" if notes else "")

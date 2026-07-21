#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════════
#  ChadoTrade — Bot de signaux Chapitre 15 (portage Python)
#  Reprend EXACTEMENT la même logique que analyzeSymV2 dans l'app :
#  Structure (BOS H4) → Manipulation (sweep H1) → Premium/Discount
#  (OTE 61.8-79%) → PD Array (OB/FVG/Breaker/IFVG) → SL/TP (IRL to
#  ERL) → Horaire (Kill Zone Paris) → email Gmail si signal trouvé.
#
#  Aucune logique supplémentaire par rapport à l'ebook, à une
#  exception près assumée : le filtre "calendrier économique"
#  (Ch.1.4) n'est pas porté ici (pas de base de données de news
#  dans ce script) — voir README_SETUP.md.
# ════════════════════════════════════════════════════════════════

import asyncio
import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets

# ── Instruments (identiques à l'app) ──
FOREX = [
    {"sym": "frxEURUSD", "name": "EUR/USD", "type": "Forex"},
    {"sym": "frxGBPUSD", "name": "GBP/USD", "type": "Forex"},
    {"sym": "frxUSDJPY", "name": "USD/JPY", "type": "Forex"},
    {"sym": "frxUSDCHF", "name": "USD/CHF", "type": "Forex"},
    {"sym": "frxAUDUSD", "name": "AUD/USD", "type": "Forex"},
    {"sym": "frxEURJPY", "name": "EUR/JPY", "type": "Forex"},
    {"sym": "frxGBPJPY", "name": "GBP/JPY", "type": "Forex"},
    {"sym": "frxXAUUSD", "name": "XAU/USD", "type": "Forex"},
    {"sym": "frxXAGUSD", "name": "XAG/USD", "type": "Forex"},
]
SYNTH = [
    {"sym": "R_100", "name": "Volatility 100", "type": "Synthetic"},
    {"sym": "R_75", "name": "Volatility 75", "type": "Synthetic"},
    {"sym": "R_50", "name": "Volatility 50", "type": "Synthetic"},
    {"sym": "R_25", "name": "Volatility 25", "type": "Synthetic"},
    {"sym": "R_10", "name": "Volatility 10", "type": "Synthetic"},
    {"sym": "stpRNG", "name": "Step Index", "type": "Synthetic"},
]
ALL = FOREX + SYNTH

DERIV_WS = "wss://ws.derivws.com/websockets/v3?app_id=1089"
STATE_FILE = Path(__file__).parent / "state.json"
DEDUPE_HOURS = 4  # ne pas ré-alerter le même signal avant N heures

TF_LIST = [
    {"tf": 14400, "label": "H4", "count": 100},
    {"tf": 3600, "label": "H1", "count": 100},
    {"tf": 900, "label": "M15", "count": 100},
]


def dp(sym):
    if sym == "frxXAUUSD":
        return 2
    if sym == "frxXAGUSD":
        return 3
    if "JPY" in sym:
        return 3
    if sym.startswith("R_"):
        return 2
    if sym == "stpRNG":
        return 3
    return 5


def fmt(sym, v):
    return f"{v:.{dp(sym)}f}"


# ════════════════════════════════════════════════════════════════
#  Détection ICT/SMC — portage 1:1 des fonctions JS de l'app
# ════════════════════════════════════════════════════════════════

def get_swings(cs, lb=None):
    if lb is None:
        lb = max(2, min(5, len(cs) // 20))
    lb = max(1, min(lb, len(cs) // 4))
    highs, lows = [], []
    for i in range(lb, len(cs) - lb):
        hi, lo = cs[i]["high"], cs[i]["low"]
        is_h = is_l = True
        for j in range(i - lb, i + lb + 1):
            if j == i:
                continue
            if cs[j]["high"] >= hi:
                is_h = False
            if cs[j]["low"] <= lo:
                is_l = False
        if is_h:
            highs.append({"i": i, "price": hi})
        if is_l:
            lows.append({"i": i, "price": lo})
    return {"highs": highs, "lows": lows}


def get_trend(cs, sw):
    if not sw["highs"] or not sw["lows"]:
        return "bull" if cs[-1]["close"] > cs[0]["close"] else "bear"
    bull = bear = 0
    rh, rl = sw["highs"][-4:], sw["lows"][-4:]
    for i in range(1, len(rh)):
        bull += 1 if rh[i]["price"] > rh[i - 1]["price"] else 0
        bear += 1 if rh[i]["price"] <= rh[i - 1]["price"] else 0
    for i in range(1, len(rl)):
        bull += 1 if rl[i]["price"] > rl[i - 1]["price"] else 0
        bear += 1 if rl[i]["price"] <= rl[i - 1]["price"] else 0
    return "bull" if bull > bear else "bear"


def detect_bos(cs, sw):
    if len(cs) < 3 or not sw["highs"] or not sw["lows"]:
        return None
    lc, pc = cs[-1]["close"], cs[-2]["close"]
    lh, ll = sw["highs"][-1]["price"], sw["lows"][-1]["price"]
    if lc > lh and pc <= lh:
        return {"dir": "bull", "level": lh}
    if lc < ll and pc >= ll:
        return {"dir": "bear", "level": ll}
    return None


def detect_fvg(cs):
    out = []
    for i in range(2, len(cs)):
        hi0, lo0 = cs[i - 2]["high"], cs[i - 2]["low"]
        hi2, lo2 = cs[i]["high"], cs[i]["low"]
        if lo2 > hi0:
            filled = any(c["low"] <= hi0 + (lo2 - hi0) * 0.5 for c in cs[i + 1:])
            if not filled:
                out.append({"dir": "bull", "top": lo2, "bot": hi0, "i": i, "fresh": i >= len(cs) - 10})
        if hi2 < lo0:
            filled = any(c["high"] >= lo0 - (lo0 - hi2) * 0.5 for c in cs[i + 1:])
            if not filled:
                out.append({"dir": "bear", "top": lo0, "bot": hi2, "i": i, "fresh": i >= len(cs) - 10})
    return out[-6:]


def detect_ob(cs, direction):
    if len(cs) < 10:
        return None
    avg_body = sum(abs(c["close"] - c["open"]) for c in cs[-10:]) / 10
    for i in range(len(cs) - 3, max(-1, len(cs) - 30) - 1, -1):
        op, cl = cs[i]["open"], cs[i]["close"]
        hi, lo = cs[i]["high"], cs[i]["low"]
        body = abs(cl - op)
        rng = (hi - lo) or 1
        if body < rng * 0.4 or body < avg_body * 0.3:
            continue
        if direction == "bull" and cl < op:
            impulse = 0
            for j in range(i + 1, min(i + 4, len(cs))):
                mv = cs[j]["close"] - cs[j]["open"]
                if mv > 0:
                    impulse = max(impulse, mv)
            if impulse < avg_body * 1.5:
                continue
            if any(c["close"] < cl * 0.999 for c in cs[i + 1:]):
                continue
            return {"dir": "bull", "top": op, "bot": cl, "i": i}
        if direction == "bear" and cl > op:
            impulse = 0
            for j in range(i + 1, min(i + 4, len(cs))):
                mv = cs[j]["open"] - cs[j]["close"]
                if mv > 0:
                    impulse = max(impulse, mv)
            if impulse < avg_body * 1.5:
                continue
            if any(c["close"] > cl * 1.001 for c in cs[i + 1:]):
                continue
            return {"dir": "bear", "top": cl, "bot": op, "i": i}
    return None


def check_ob_mitigation(cs, ob):
    if not ob:
        return {"valid": False, "fresh": False}
    ob_range = ob["top"] - ob["bot"]
    if ob_range <= 0:
        return {"valid": False, "fresh": False}
    max_pen = 0
    for c in cs[ob["i"] + 1:]:
        if ob["dir"] == "bull":
            if c["low"] < ob["top"]:
                max_pen = max(max_pen, (ob["top"] - c["low"]) / ob_range)
            if c["close"] < ob["bot"]:
                return {"valid": False, "fresh": False}
        else:
            if c["high"] > ob["bot"]:
                max_pen = max(max_pen, (c["high"] - ob["bot"]) / ob_range)
            if c["close"] > ob["top"]:
                return {"valid": False, "fresh": False}
    return {"valid": True, "fresh": max_pen < 0.5}


def detect_breaker_block(cs, direction):
    if len(cs) < 10:
        return []
    last_close = cs[-1]["close"]
    out = []
    for i in range(3, len(cs) - 2):
        op, cl = cs[i]["open"], cs[i]["close"]
        body = abs(cl - op)
        if body < (cs[i]["high"] - cs[i]["low"]) * 0.1:
            continue
        if cl < op:  # bougie bear = OB bull cassé -> BB bear
            had_impulse = any(cs[j]["close"] > op * 1.001 for j in range(i + 1, min(i + 5, len(cs))))
            if not had_impulse:
                continue
            broken = any(c["close"] < cl * 0.999 for c in cs[i + 1:])
            if not broken:
                continue
            if direction == "bear":
                rebroken = any(c["close"] > op * 1.001 for c in cs[i + 1:])
                if rebroken:
                    continue
                if last_close < op:
                    out.append({"dir": "bear", "top": op, "bot": cl, "i": i})
        if cl > op:  # bougie bull = OB bear cassé -> BB bull
            had_impulse = any(cs[j]["close"] < op * 0.999 for j in range(i + 1, min(i + 5, len(cs))))
            if not had_impulse:
                continue
            broken = any(c["close"] > cl * 1.001 for c in cs[i + 1:])
            if not broken:
                continue
            if direction == "bull":
                rebroken = any(c["close"] < op * 0.999 for c in cs[i + 1:])
                if rebroken:
                    continue
                if last_close > op:
                    out.append({"dir": "bull", "top": cl, "bot": op, "i": i})
    return out[-2:]


def detect_ifvg(cs):
    out = []
    last_close = cs[-1]["close"]
    for i in range(2, len(cs) - 2):
        p0h, p0l = cs[i - 2]["high"], cs[i - 2]["low"]
        p2h, p2l = cs[i]["high"], cs[i]["low"]
        if p2h < p0l:  # FVG bear initial -> IFVG bull
            top, bot = p0l, p2h
            mitigated = any(c["high"] >= bot and c["low"] <= top for c in cs[i + 1:])
            if mitigated:
                gap = top - bot
                dist = min(abs(last_close - top), abs(last_close - bot))
                if gap > 0 and dist < gap * 10:
                    out.append({"dir": "bull", "top": top, "bot": bot, "i": i})
        if p2l > p0h:  # FVG bull initial -> IFVG bear
            top, bot = p2l, p0h
            mitigated = any(c["high"] >= bot and c["low"] <= top for c in cs[i + 1:])
            if mitigated:
                gap = top - bot
                dist = min(abs(last_close - top), abs(last_close - bot))
                if gap > 0 and dist < gap * 10:
                    out.append({"dir": "bear", "top": top, "bot": bot, "i": i})
    return out[-4:]


def is_forex_open():
    now = datetime.now(timezone.utc)
    wd = now.weekday()  # 0=lundi ... 6=dimanche
    total_min = now.hour * 60 + now.minute
    if wd == 5:  # samedi
        return False
    if wd == 6:  # dimanche
        return False
    if wd == 4 and total_min >= 22 * 60:  # vendredi après 22h UTC
        return False
    return True


def book_kill_zone():
    paris_hour = datetime.now(ZoneInfo("Europe/Paris")).hour
    in_london = 8 <= paris_hour < 10
    in_ny = 13 <= paris_hour < 15
    if in_london:
        label = "Kill Zone Londres (08h-10h Paris)"
    elif in_ny:
        label = "Kill Zone New York (13h-15h Paris)"
    elif 0 <= paris_hour < 8:
        label = "Session asiatique — accumulation (hors Kill Zone)"
    else:
        label = "Hors Kill Zone"
    return {"in_kz": in_london or in_ny, "label": label}


# ── Étape 2 : Premium/Discount + OTE 61.8-79% (Ch.6.2/6.3) ──
def pd_ote_zone(sw, bias):
    if not sw["highs"] or not sw["lows"]:
        return None
    lh, ll = sw["highs"][-1]["price"], sw["lows"][-1]["price"]
    if lh <= ll:
        return None
    rng = lh - ll
    if bias == "bull":
        return {"top": lh - rng * 0.618, "bot": lh - rng * 0.79, "swing_high": lh, "swing_low": ll}
    return {"top": ll + rng * 0.79, "bot": ll + rng * 0.618, "swing_high": lh, "swing_low": ll}


# ── Étape 2 : prise de liquidité confirmée (Ch.4.3) ──
def find_confirmed_sweep(cs, sw, bias, lookback=12):
    start = max(0, len(cs) - lookback)
    if bias == "bull":
        for k in range(len(cs) - 1, start - 1, -1):
            lo, cl = cs[k]["low"], cs[k]["close"]
            for s in sw["lows"][-4:]:
                if s["i"] >= k:
                    continue
                if lo < s["price"] and cl > s["price"]:
                    return {"level": s["price"], "label": "SSL (Sell Side Liquidity) prise"}
    else:
        for k in range(len(cs) - 1, start - 1, -1):
            hi, cl = cs[k]["high"], cs[k]["close"]
            for s in sw["highs"][-4:]:
                if s["i"] >= k:
                    continue
                if hi > s["price"] and cl < s["price"]:
                    return {"level": s["price"], "label": "BSL (Buy Side Liquidity) prise"}
    return None


# ── Étape 3 : PD Array — les 4 zones du Chapitre 9 ──
def find_best_pd_array(cs, bias, price, ote):
    candidates = []
    ob = detect_ob(cs, bias)
    if ob:
        mit = check_ob_mitigation(cs, ob)
        if mit["valid"]:
            candidates.append({"type": "Order Block", "top": ob["top"], "bot": ob["bot"], "fresh": mit["fresh"]})
    fvgs = [f for f in detect_fvg(cs) if f["dir"] == bias]
    if fvgs:
        f = fvgs[-1]
        candidates.append({"type": "Fair Value Gap", "top": f["top"], "bot": f["bot"], "fresh": f["fresh"]})
    bbs = detect_breaker_block(cs, bias)
    if bbs:
        bb = bbs[-1]
        candidates.append({"type": "Breaker Block", "top": bb["top"], "bot": bb["bot"], "fresh": True})
    ifvgs = [f for f in detect_ifvg(cs) if f["dir"] == bias]
    if ifvgs:
        f = ifvgs[-1]
        candidates.append({"type": "IFVG", "top": f["top"], "bot": f["bot"], "fresh": True})
    if not candidates:
        return None
    in_ote = [c for c in candidates if ote and c["top"] >= ote["bot"] and c["bot"] <= ote["top"]]
    pool = in_ote if in_ote else candidates
    pool.sort(key=lambda c: min(abs(price - c["top"]), abs(price - c["bot"])))
    best = pool[0]
    best["confluence"] = [c["type"] for c in candidates if c is not best and c["top"] >= best["bot"] and c["bot"] <= best["top"]]
    return best


# ── Étape 4 : TP = prochaine liquidité externe (IRL to ERL, Ch.10) ──
def next_external_liquidity(sw_h1, sw_h4, bias, price):
    if bias == "bull":
        pool = [h["price"] for h in sw_h1["highs"] + sw_h4["highs"] if h["price"] > price]
        return min(pool) if pool else None
    pool = [l["price"] for l in sw_h1["lows"] + sw_h4["lows"] if l["price"] < price]
    return max(pool) if pool else None


# ════════════════════════════════════════════════════════════════
#  Moteur principal — les 5 étapes exactes du Chapitre 15
# ════════════════════════════════════════════════════════════════
def analyze_symbol(s, tf_data, price):
    kz = book_kill_zone()
    if s["type"] == "Forex" and not is_forex_open():
        return None
    if s["type"] == "Forex" and not kz["in_kz"]:
        return None

    cs_h4, cs_h1, cs_m15 = tf_data.get("H4"), tf_data.get("H1"), tf_data.get("M15")
    if not cs_h4 or not cs_h1 or not cs_m15 or len(cs_h4) < 20 or len(cs_h1) < 20:
        return None

    d = dp(s["sym"])

    # Étape 1 — Structure de marché (Ch.5)
    sw_h4 = get_swings(cs_h4, 4)
    bias = get_trend(cs_h4, sw_h4)
    bos_h4 = detect_bos(cs_h4, sw_h4)
    if not bos_h4 or bos_h4["dir"] != bias:
        return None

    # Étape 2 — Manipulation + Premium/Discount (Ch.4/Ch.6)
    sw_h1 = get_swings(cs_h1, 3)
    sweep = find_confirmed_sweep(cs_h1, sw_h1, bias, 12)
    if not sweep:
        return None
    ote = pd_ote_zone(sw_h1, bias)
    if not ote:
        return None
    if not (ote["bot"] <= price <= ote["top"]):
        return None

    # Étape 3 — PD Array (Ch.9)
    pd_array = find_best_pd_array(cs_h1, bias, price, ote) or find_best_pd_array(cs_m15, bias, price, ote)
    if not pd_array:
        return None

    # Étape 4 — SL / TP (Ch.10) — SL exactement au bord du PD Array
    sl = pd_array["bot"] if bias == "bull" else pd_array["top"]
    sl_dist = abs(price - sl)
    if not sl_dist:
        return None
    if bias == "bull" and sl >= price:
        return None
    if bias == "bear" and sl <= price:
        return None
    tp = next_external_liquidity(sw_h1, sw_h4, bias, price)
    if tp is None:
        return None
    rr = abs(tp - price) / sl_dist

    # Étape 5 — Horaire & risque (Ch.13.2/Ch.12.2) — déjà gaté plus haut pour le Forex

    grade = "A+" if (pd_array["confluence"] or pd_array["fresh"]) and kz["in_kz"] else ("A" if kz["in_kz"] else "B+")

    return {
        "sym": s["sym"], "name": s["name"], "type": s["type"],
        "side": "buy" if bias == "bull" else "sell",
        "price": price, "sl": sl, "tp": tp, "rr": round(rr, 1), "grade": grade,
        "pd_array_type": pd_array["type"], "confluence": pd_array["confluence"],
        "sweep_label": sweep["label"], "kz_label": kz["label"], "d": d,
    }


def format_email(signals):
    lines = []
    for s in signals:
        lines.append(
            f"{'🟢' if s['side']=='buy' else '🔴'} {s['name']} — {s['side'].upper()} ({s['grade']})\n"
            f"   Entrée : {fmt(s['sym'], s['price'])}\n"
            f"   Stop Loss : {fmt(s['sym'], s['sl'])}\n"
            f"   Take Profit : {fmt(s['sym'], s['tp'])}  (RR 1:{s['rr']})\n"
            f"   PD Array : {s['pd_array_type']}" + (f" + confluence {', '.join(s['confluence'])}" if s['confluence'] else "") + "\n"
            f"   {s['sweep_label']} · {s['kz_label']}\n"
        )
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  Récupération des bougies sur l'API Deriv (WebSocket)
# ════════════════════════════════════════════════════════════════
async def fetch_all_candles():
    tf_data = {s["sym"]: {} for s in ALL}
    prices = {}

    async with websockets.connect(DERIV_WS, ping_interval=20) as ws:
        req_id = 1
        pending = {}
        for s in ALL:
            for tf in TF_LIST:
                await ws.send(json.dumps({
                    "ticks_history": s["sym"], "adjust_start_time": 1,
                    "count": tf["count"], "end": "latest",
                    "granularity": tf["tf"], "style": "candles", "req_id": req_id,
                }))
                pending[req_id] = (s["sym"], tf["label"])
                req_id += 1

        total = len(pending)
        received = 0
        while received < total:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=25)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            rid = msg.get("req_id")
            if rid in pending and "candles" in msg:
                sym, label = pending.pop(rid)
                candles = [
                    {"open": float(c["open"]), "high": float(c["high"]),
                     "low": float(c["low"]), "close": float(c["close"])}
                    for c in msg["candles"]
                ]
                tf_data[sym][label] = candles
                if candles:
                    prices[sym] = candles[-1]["close"]
                received += 1

    return tf_data, prices


# ════════════════════════════════════════════════════════════════
#  Déduplication (évite de ré-envoyer le même signal en boucle)
# ════════════════════════════════════════════════════════════════
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def filter_new_signals(signals, state):
    now = datetime.now(timezone.utc)
    new = []
    for s in signals:
        key = f"{s['sym']}_{s['side']}"
        last = state.get(key)
        if last:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).total_seconds() < DEDUPE_HOURS * 3600:
                continue
        new.append(s)
        state[key] = now.isoformat()
    return new


# ════════════════════════════════════════════════════════════════
#  Envoi email Gmail (SMTP + mot de passe d'application)
# ════════════════════════════════════════════════════════════════
def send_email(body, count):
    user = os.environ["GMAIL_USER"]
    app_pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("GMAIL_TO", user)

    msg = MIMEText(body)
    msg["Subject"] = f"⚡ ChadoTrade — {count} nouveau(x) signal(aux) Chapitre 15"
    msg["From"] = user
    msg["To"] = to

    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=ctx)
        server.login(user, app_pw)
        server.sendmail(user, [to], msg.as_string())


async def main():
    tf_data, prices = await fetch_all_candles()

    signals = []
    for s in ALL:
        price = prices.get(s["sym"])
        if price is None:
            continue
        sig = analyze_symbol(s, tf_data[s["sym"]], price)
        if sig:
            signals.append(sig)

    state = load_state()
    new_signals = filter_new_signals(signals, state)
    save_state(state)

    print(f"{len(signals)} signal(aux) détecté(s), {len(new_signals)} nouveau(x).")

    if new_signals:
        body = format_email(new_signals)
        send_email(body, len(new_signals))
        print("Email envoyé.")
    else:
        print("Rien de nouveau à notifier.")


if __name__ == "__main__":
    asyncio.run(main())

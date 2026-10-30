#!/usr/bin/env python3
"""Alert RSI e livelli di prezzo per BTC e oro, gestiti da chat Telegram.

BTC: Binance (data-api.binance.vision), con ripiego su Kraken.
Oro: XAU/USD reale (spot) da Twelve Data, chiave nel secret TWELVEDATA_KEY.
Il mercato dell'oro e' chiuso nel weekend: in quel periodo non viene controllato.
Strategia: RSI a chiusura candela (long <=35, short >=65) + trend EMA e forza MACD.
Comandi Telegram: /alert, /rsi, /adx, /lista, /stato, /cancella, /aiuto.
"""
import calendar
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "alerts.json")
STATE = os.path.join(BASE, "state.json")

BINANCE = {"BTC": "BTCUSDT"}
KRAKEN = {"BTC": "XBTUSD"}
SUPPORTED = ("BTC", "XAU")
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
TD_INT = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
          "1h": "1h", "4h": "4h", "1d": "1day"}
KRAKEN_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
ALIASES = {"BTCUSD": "BTC", "BTCUSDT": "BTC", "BITCOIN": "BTC",
           "XAUUSD": "XAU", "GOLD": "XAU", "ORO": "XAU"}
DIRS = {"sopra": "above", "su": "above", "above": "above",
        "sotto": "below", "giu": "below", "giù": "below", "below": "below"}

HELP = (
    "Strategia attiva: segnale quando la candela chiude con RSI <= 35 (long) o >= 65 (short), "
    "con trend EMA200 e forza MACD. Le soglie sono in alerts.json.\n\n"
    "Comandi:\n"
    "/alert btc sopra 95000\n"
    "/alert oro sotto 4000\n"
    "/rsi btc sopra 75\n"
    "/rsi oro sotto 25 1h\n"
    "/adx btc sopra 25\n"
    "/adx oro sopra 30 1h\n"
    "/lista  (mostra gli alert)\n"
    "/stato  (prezzo, ADX e RSI adesso)\n"
    "/cancella 2  (cancella l'alert numero 2)\n\n"
    "Timeframe RSI e ADX: 5m, 15m, 30m, 1h, 4h, 1d.\n"
    "Le modifiche vengono applicate entro circa 5 minuti."
)


# ---------- utilita ----------
def fmt(v):
    return f"{float(v):.2f}".rstrip("0").rstrip(".")


def norm(sym):
    s = sym.upper()
    return ALIASES.get(s, s)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def parse_num(s):
    s = s.replace("$", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    elif re.fullmatch(r"\d{1,3}(,\d{3})+", s):
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    return float(s)


# ---------- dati di mercato ----------
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def candles_binance(sym, tf):
    q = urllib.parse.urlencode({"symbol": BINANCE[sym], "interval": tf, "limit": 1000})
    rows = get_json(f"https://data-api.binance.vision/api/v3/klines?{q}")
    return [(float(r[2]), float(r[3]), float(r[4]), r[0] / 1000) for r in rows]


def candles_kraken(sym, tf):
    q = urllib.parse.urlencode({"pair": KRAKEN[sym], "interval": KRAKEN_MIN[tf]})
    data = get_json(f"https://api.kraken.com/0/public/OHLC?{q}")
    if data.get("error"):
        raise RuntimeError(data["error"])
    rows = next(v for k, v in data["result"].items() if k != "last")
    return [(float(r[2]), float(r[3]), float(r[4]), float(r[0])) for r in rows][-1000:]


class MarketClosed(Exception):
    """Mercato dell'oro chiuso (weekend)."""


def gold_market_closed():
    n = datetime.datetime.now(datetime.timezone.utc)
    wd, h = n.weekday(), n.hour  # lunedi=0 ... domenica=6
    return wd == 5 or (wd == 4 and h >= 22) or (wd == 6 and h < 21)


def td_epoch(txt):
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return float(calendar.timegm(time.strptime(txt, f)))
        except ValueError:
            pass
    raise ValueError(f"data non valida: {txt}")


def candles_twelvedata(tf):
    key = (os.environ.get("TWELVEDATA_KEY") or "").strip()
    if not key:
        raise RuntimeError("TWELVEDATA_KEY mancante")
    q = urllib.parse.urlencode({"symbol": "XAU/USD", "interval": TD_INT[tf],
                                "outputsize": 1000, "timezone": "UTC", "apikey": key})
    d = get_json(f"https://api.twelvedata.com/time_series?{q}")
    if d.get("status") == "error" or "values" not in d:
        raise RuntimeError(d.get("message", "errore Twelve Data"))
    return [(float(v["high"]), float(v["low"]), float(v["close"]), td_epoch(v["datetime"]))
            for v in reversed(d["values"])]  # dal piu' vecchio


def get_candles(sym, tf):
    if sym == "XAU":
        return candles_twelvedata(tf)
    for fn in (candles_binance, candles_kraken):
        try:
            return fn(sym, tf)
        except Exception as e:  # prova la fonte successiva
            print(f"{fn.__name__} {sym} {tf}: {e}")
    raise RuntimeError(f"nessun dato per {sym} {tf}")


_cache = {}


def data(sym, tf):
    if sym == "XAU" and gold_market_closed():
        raise MarketClosed()
    if (sym, tf) not in _cache:
        _cache[(sym, tf)] = get_candles(sym, tf)
    return _cache[(sym, tf)]


def rsi(values, period=14):
    """RSI di Wilder. L'ultimo valore della lista e' l'ultima candela chiusa."""
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def adx(candles, period=14):
    """ADX di Wilder. candles = [(high, low, close)] dal piu' vecchio.
    Ritorna (ADX, +DI, -DI) oppure None se i dati non bastano."""
    n = len(candles)
    if n < 2 * period + 1:
        return None
    tr, pdm, mdm = [], [], []
    for i in range(1, n):
        h, l = candles[i][0], candles[i][1]
        ph, pl, pc = candles[i - 1][0], candles[i - 1][1], candles[i - 1][2]
        up, down = h - ph, pl - l
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))

    def di_dx(atr, p, m):
        if atr == 0:
            return 0.0, 0.0, 0.0
        pdi, mdi = 100 * p / atr, 100 * m / atr
        tot = pdi + mdi
        return pdi, mdi, (100 * abs(pdi - mdi) / tot if tot else 0.0)

    atr, p, m = sum(tr[:period]), sum(pdm[:period]), sum(mdm[:period])
    pdi, mdi, dx = di_dx(atr, p, m)
    dxs = [dx]
    for i in range(period, len(tr)):
        atr = atr - atr / period + tr[i]
        p = p - p / period + pdm[i]
        m = m - m / period + mdm[i]
        pdi, mdi, dx = di_dx(atr, p, m)
        dxs.append(dx)
    if len(dxs) < period:
        return None
    a = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        a = (a * (period - 1) + dx) / period
    return a, pdi, mdi


# ---------- alert ----------
def alert_tf(a, cfg):
    if a["type"] in ("rsi", "adx"):
        return a.get("timeframe", cfg.get("timeframe", "15m"))
    # prezzo: BTC a 1 minuto; oro sullo stesso timeframe degli altri (risparmia richieste)
    return "1m" if norm(a["symbol"]) == "BTC" else cfg.get("timeframe", "15m")


def alert_key(a, cfg):
    return (f"{norm(a['symbol'])}|{a['type']}|{a['condition']}|"
            f"{fmt(a['value'])}|{alert_tf(a, cfg)}")


def adx_info(a, cfg):
    """(ADX, +DI, -DI) sulla candela in corso, o None se i dati non bastano."""
    c = data(norm(a["symbol"]), alert_tf(a, cfg))
    return adx(c, int(cfg.get("adx_period", 14)))


def current_value(a, cfg):
    """Ritorna (valore da confrontare, prezzo attuale)."""
    c = data(norm(a["symbol"]), alert_tf(a, cfg))
    closes = [x[2] for x in c]
    price = closes[-1]
    if a["type"] == "rsi":
        return rsi(closes[:-1], int(cfg.get("rsi_period", 14))), price  # esclude candela aperta
    if a["type"] == "adx":
        r = adx(c, int(cfg.get("adx_period", 14)))  # include la candela in corso: avviso subito
        return (r[0] if r else None), price
    return price, price


def is_hit(a, cur):
    v = float(a["value"])
    return cur >= v if a["condition"] == "above" else cur <= v


def describe(a, cfg):
    name = "Oro" if norm(a["symbol"]) == "XAU" else "BTC"
    verso = "sopra" if a["condition"] == "above" else "sotto"
    if a["type"] == "adx":
        return f"{name} ADX({int(cfg.get('adx_period', 14))}, {alert_tf(a, cfg)}) {verso} {fmt(a['value'])}"
    if a["type"] == "rsi":
        return f"{name} RSI({int(cfg.get('rsi_period', 14))}, {alert_tf(a, cfg)}) {verso} {fmt(a['value'])}"
    return f"{name} prezzo {verso} {fmt(a['value'])}"


def list_text(cfg):
    if not cfg["alerts"]:
        return "Nessun alert attivo."
    righe = [f"{i}. {describe(a, cfg)}" for i, a in enumerate(cfg["alerts"], 1)]
    return "📋 Alert attivi:\n" + "\n".join(righe)


# ---------- Telegram ----------
def tg_creds():
    return ((os.environ.get("TELEGRAM_TOKEN") or "").strip(),
            (os.environ.get("TELEGRAM_CHAT_ID") or "").strip())


def telegram(text):
    token, chat = tg_creds()
    if not token or not chat:
        print("Telegram non configurato. Messaggio:", text)
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=20).read()


def tg_api(method, **params):
    token, _ = tg_creds()
    return get_json(f"https://api.telegram.org/bot{token}/{method}?{urllib.parse.urlencode(params)}")


def add_alert(cmd, args, cfg, state):
    sym = norm(args[0])
    if sym not in SUPPORTED:
        return f"Asset non supportato: {args[0]}. Usa btc o oro.", False
    cond = DIRS.get(args[1])
    if cond is None:
        raise ValueError("direzione")
    val = parse_num(args[2])
    val = int(val) if val == int(val) else val
    a = {"symbol": sym, "type": {"alert": "price"}.get(cmd, cmd),
         "condition": cond, "value": val}
    if cmd in ("rsi", "adx"):
        if not 0 < val < 100:
            return "RSI e ADX vanno da 0 a 100.", False
        if len(args) > 3:
            if args[3] not in KRAKEN_MIN:
                return "Timeframe valido: 5m, 15m, 30m, 1h, 4h, 1d.", False
            a["timeframe"] = args[3]
    elif val <= 0:
        return "Il prezzo deve essere maggiore di zero.", False

    key = alert_key(a, cfg)
    if any(alert_key(x, cfg) == key for x in cfg["alerts"]):
        return "Questo alert esiste già.", False
    cfg["alerts"].append(a)
    text = f"✅ Aggiunto: {describe(a, cfg)}"
    try:
        cur, _ = current_value(a, cfg)
        if cur is not None:
            if is_hit(a, cur):
                state[key] = True  # evita l'avviso immediato
                verso = "sopra" if cond == "above" else "sotto"
                text += (f"\n⚠️ Adesso è già {verso} ({fmt(cur)}): scatterà solo "
                         "quando tornerà indietro e lo ripasserà.")
            else:
                text += f"\nValore attuale: {fmt(cur)}"
    except Exception as e:
        print(f"valore attuale non disponibile: {e}")
    return text, True


def status_text(cfg):
    sc = cfg.get("strategy") or {}
    tf = sc.get("timeframe", cfg.get("timeframe", "15m"))
    n_ema = int(sc.get("ema", 200))
    period = int(cfg.get("rsi_period", 14))
    ap = int(cfg.get("adx_period", 14))
    righe = [f"📊 Situazione ({tf}, indicatori sull'ultima candela chiusa)"]
    for sym, name in (("BTC", "BTC"), ("XAU", "Oro (XAUUSD)")):
        try:
            cand = data(sym, tf)
            live = cand[-1][2]
            d = analyze(sym, tf, cfg, sc)
            if d is None:
                righe.append(f"\n{name}: storico insufficiente")
                continue
            righe.append(f"\n{name}: prezzo {live:,.2f}")
            righe.append(f"RSI({period}) {d['rsi']:.1f}")
            ax = adx(cand, ap)
            if ax:
                trend = "rialzista" if ax[1] > ax[2] else "ribassista"
                righe.append(f"ADX({ap}) {ax[0]:.1f} ({trend}, candela in corso)")
            righe.extend(context_lines(d, n_ema, ap, with_adx=False))
        except MarketClosed:
            righe.append(f"\n{name}: mercato chiuso")
        except Exception as e:
            print(f"stato {sym}: {e}")
            righe.append(f"\n{name}: dati non disponibili")
    return "\n".join(righe)


def heartbeat(cfg, state):
    """Un messaggio al giorno (dopo le 07 UTC) per sapere che il sistema e' vivo."""
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour < 7 or state.get("_hb") == today:
        return
    try:
        telegram("💚 Sistema attivo.\n" + status_text(cfg))
        state["_hb"] = today
    except Exception as e:
        print(f"heartbeat non inviato: {e}")


def run_command(text, cfg, state):
    parts = text.lower().replace(">", " sopra ").replace("<", " sotto ").split()
    if not parts:
        return None, False
    slash = parts[0].startswith("/")
    cmd = parts[0].lstrip("/").split("@")[0]
    args = parts[1:]
    if cmd in ("start", "aiuto", "help"):
        return HELP, False
    if cmd == "lista":
        return list_text(cfg), False
    if cmd == "stato":
        return status_text(cfg), False
    try:
        if cmd == "cancella":
            n = int(args[0])
            if not 1 <= n <= len(cfg["alerts"]):
                return "Numero non valido. Usa /lista.", False
            removed = cfg["alerts"].pop(n - 1)
            return f"🗑 Cancellato: {describe(removed, cfg)}", True
        if cmd in ("alert", "rsi", "adx"):
            return add_alert(cmd, args, cfg, state)
    except (ValueError, IndexError, KeyError):
        pass
    if slash:
        return "Non ho capito.\n\n" + HELP, False
    return None, False


def handle_commands(cfg, state):
    token, chat = tg_creds()
    if not token or not chat:
        return
    try:
        updates = tg_api("getUpdates", timeout=0).get("result", [])
    except Exception as e:
        print(f"getUpdates fallito: {e}")
        return
    if not updates:
        return

    replies, changed = [], False
    for u in updates:
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat:
            continue  # ignora chi non sei tu
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        reply, mod = run_command(text, cfg, state)
        if reply:
            replies.append(reply)
        changed = changed or mod

    if changed:
        save_json(CONFIG, cfg)
    try:  # conferma i messaggi letti, cosi' non vengono riletti
        tg_api("getUpdates", offset=max(u["update_id"] for u in updates) + 1, timeout=0)
    except Exception as e:
        print(f"conferma update fallita: {e}")
    for r in replies:
        try:
            telegram(r)
        except Exception as e:
            print(f"risposta non inviata: {e}")


def ema_series(values, n):
    k = 2 / (n + 1)
    e = values[0]
    out = [e]
    for v in values[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def macd_calc(values, fast=12, slow=26, sig=9):
    ef, es = ema_series(values, fast), ema_series(values, slow)
    macd = [a - b for a, b in zip(ef, es)]
    sg = ema_series(macd, sig)
    return macd, sg, [m - x for m, x in zip(macd, sg)]


def closed_candles(c, tf):
    """Toglie l'ultima candela se e' ancora in corso."""
    if c and c[-1][3] + TF_SEC[tf] > time.time():
        return c[:-1]
    return c


def analyze(sym, tf, cfg, sc):
    """RSI, EMA e MACD sull'ultima candela CHIUSA. None se lo storico non basta."""
    c = closed_candles(data(sym, tf), tf)
    closes = [x[2] for x in c]
    n_ema = int(sc.get("ema", 200))
    if len(closes) < n_ema + 20:
        return None
    period = int(cfg.get("rsi_period", 14))
    fast, slow, sig = sc.get("macd", [12, 26, 9])
    macd, sg, hist = macd_calc(closes, int(fast), int(slow), int(sig))
    ax = adx(c, int(cfg.get("adx_period", 14)))
    return {
        "adx": ax[0] if ax else None, "pdi": ax[1] if ax else None, "mdi": ax[2] if ax else None,
        "t": int(c[-1][3]), "price": closes[-1],
        "rsi": rsi(closes, period), "rsi_prev": rsi(closes[:-1], period),
        "ema": ema_series(closes, n_ema)[-1],
        "macd": macd[-1], "signal": sg[-1], "hist": hist[-1], "hist_prev": hist[-2],
    }


def context_lines(d, n_ema, adx_p=14, with_adx=True):
    pct = (d["price"] - d["ema"]) / d["ema"] * 100
    trend = "rialzista" if d["price"] > d["ema"] else "ribassista"
    verso = "rialzista" if d["hist"] > 0 else "ribassista"
    forza = "in rafforzamento" if abs(d["hist"]) > abs(d["hist_prev"]) else "in indebolimento"
    righe = [
        f"Trend EMA{n_ema}: {trend} (prezzo {pct:+.2f}% rispetto a {d['ema']:,.2f})",
        f"MACD {d['macd']:+.2f} | segnale {d['signal']:+.2f} | "
        f"istogramma {d['hist']:+.2f} ({verso}, {forza})",
    ]
    if with_adx and d.get("adx") is not None:
        zona = "sopra 30" if d["adx"] >= 30 else "sopra 25" if d["adx"] >= 25 else "sotto 25"
        dirz = "rialzista" if d["pdi"] > d["mdi"] else "ribassista"
        righe.append(f"ADX({adx_p}) {d['adx']:.1f} ({zona}, direzione {dirz})")
    return righe


def check_strategy(cfg, state):
    """Alert a chiusura candela: RSI <= soglia long, RSI >= soglia short.
    Ritorna gli asset con errore sui dati."""
    sc = cfg.get("strategy") or {}
    failed = set()
    if not sc or not sc.get("enabled", True):
        return failed
    tf = sc.get("timeframe", "15m")
    lo, hi = float(sc.get("rsi_long", 35)), float(sc.get("rsi_short", 65))
    n_ema = int(sc.get("ema", 200))
    period = int(cfg.get("rsi_period", 14))
    adx_p = int(cfg.get("adx_period", 14))
    for sym in sc.get("symbols", ["BTC", "XAU"]):
        sym = norm(sym)
        name = "Oro (XAUUSD)" if sym == "XAU" else "BTC"
        try:
            d = analyze(sym, tf, cfg, sc)
        except MarketClosed:
            continue
        except Exception as e:
            print(f"strategia {sym}: {e}")
            failed.add(sym)
            continue
        if d is None or d["rsi"] is None:
            print(f"strategia {sym}: storico insufficiente")
            failed.add(sym)
            continue
        print(f"strategia {sym} {tf}: RSI {d['rsi']:.1f}, prezzo {d['price']:.2f}, EMA{n_ema} {d['ema']:.2f}")

        if d["rsi"] <= lo:
            side = "long"
        elif d["rsi"] >= hi:
            side = "short"
        else:
            continue
        prev = d["rsi_prev"]
        if sc.get("solo_prima_candela") and prev is not None and \
                ((side == "long" and prev <= lo) or (side == "short" and prev >= hi)):
            continue  # la candela precedente aveva gia' dato il segnale
        key = f"_sig|{sym}|{side}"
        if state.get(key) == d["t"]:
            continue  # segnale gia' inviato per questa candela

        if side == "long":
            testa = f"🟢 LONG {name} ({tf})"
            cond = f"RSI({period}) = {d['rsi']:.1f}, uguale o sotto {fmt(lo)}"
        else:
            testa = f"🔴 SHORT {name} ({tf})"
            cond = f"RSI({period}) = {d['rsi']:.1f}, uguale o sopra {fmt(hi)}"
        in_linea = (d["price"] > d["ema"]) == (side == "long")
        coda = (f"✅ In linea col trend EMA{n_ema}" if in_linea
                else f"⚠️ Contro il trend EMA{n_ema}")
        msg = "\n".join([testa, cond + " alla chiusura della candela",
                         f"Prezzo {d['price']:,.2f}"] + context_lines(d, n_ema, adx_p) + [coda])
        try:
            telegram(msg)
            state[key] = d["t"]
        except Exception as e:
            print(f"invio fallito, riprovo al prossimo giro: {e}")
    return failed


def check_alerts(cfg, state, failed=None):
    new_state = {}
    failed = set(failed or ())
    for a in cfg["alerts"]:
        key = alert_key(a, cfg)
        was = state.get(key, False)
        try:
            cur, price = current_value(a, cfg)
        except MarketClosed:
            new_state[key] = was
            continue
        except Exception as e:
            print(f"salto {key}: {e}")
            failed.add(norm(a["symbol"]))
            new_state[key] = was
            continue
        if cur is None:
            new_state[key] = was
            continue

        hit = is_hit(a, cur)
        keep = hit
        if a["type"] == "adx" and was and not hit:
            v = float(a["value"])  # isteresi di 1 punto: niente avvisi ripetuti vicino alla soglia
            keep = cur > v - 1 if a["condition"] == "above" else cur < v + 1
        print(f"{key}: valore {cur:.2f} -> {'SCATTATO' if hit else 'ok'}")
        if hit and not was:
            name = "Oro (XAUUSD)" if norm(a["symbol"]) == "XAU" else "BTC"
            verso = "sopra" if a["condition"] == "above" else "sotto"
            if a["type"] == "adx":
                info = adx_info(a, cfg)
                direz = ""
                if info:
                    direz = " | trend " + ("rialzista" if info[1] > info[2] else "ribassista")
                msg = (f"🔔 {name} ADX({int(cfg.get('adx_period', 14))}, {alert_tf(a, cfg)}) = "
                       f"{cur:.1f}, {verso} {fmt(a['value'])} | prezzo {price:,.2f}{direz}")
            elif a["type"] == "rsi":
                msg = (f"🔔 {name} RSI({int(cfg.get('rsi_period', 14))}, {alert_tf(a, cfg)}) = "
                       f"{cur:.1f}, {verso} {fmt(a['value'])} | prezzo {price:,.2f}")
            else:
                msg = f"🔔 {name} prezzo {price:,.2f}, {verso} il livello {fmt(a['value'])}"
            try:
                telegram(msg)
            except Exception as e:
                print(f"invio fallito, riprovo al prossimo giro: {e}")
                new_state[key] = was
                continue
        new_state[key] = keep  # si riarma quando la condizione torna falsa

    # avviso se i dati di un asset non sono leggibili per 3 controlli di fila
    for sym in ("BTC", "XAU"):
        k = f"_err|{sym}"
        prima = state.get(k, 0)
        try:
            if sym in failed:
                new_state[k] = prima + 1
                if prima + 1 == 3:
                    telegram(f"⚠️ Non riesco a leggere i prezzi di {sym} da 3 controlli di fila. "
                             f"Gli alert su {sym} non funzionano finché i dati non tornano.")
            elif prima >= 3:
                telegram(f"✅ I dati di {sym} sono di nuovo disponibili.")
        except Exception as e:
            print(f"avviso errore non inviato: {e}")

    for k, v in state.items():  # conserva valori di servizio (es. heartbeat)
        if k.startswith("_") and not k.startswith("_err|"):
            new_state.setdefault(k, v)
    return new_state


def main():
    if "--test" in sys.argv:
        telegram("✅ Test alert: tutto funziona. Scrivimi /aiuto per i comandi.")
        return
    cfg = load_json(CONFIG, {"alerts": []})
    cfg.setdefault("alerts", [])
    state = load_json(STATE, {})
    if state.get("_src_xau") != "twelvedata":  # nuova fonte oro: azzera i vecchi stati dell'oro
        state = {k: v for k, v in state.items() if not k.startswith("XAU|")}
        state["_src_xau"] = "twelvedata"
    handle_commands(cfg, state)
    failed = check_strategy(cfg, state)
    state = check_alerts(cfg, state, failed)
    if cfg.get("heartbeat", True):
        heartbeat(cfg, state)
    save_json(STATE, state)


if __name__ == "__main__":
    main()

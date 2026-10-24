#!/usr/bin/env python3
"""Alert RSI e livelli di prezzo per BTC e XAU, notifiche su Telegram.

Dati: Binance (data-api.binance.vision), con ripiego su Kraken.
Oro: si usa PAXG (token legato all'oro), molto vicino all'XAUUSD ma non identico.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "alerts.json")
STATE = os.path.join(BASE, "state.json")

BINANCE = {"BTC": "BTCUSDT", "XAU": "PAXGUSDT"}
KRAKEN = {"BTC": "XBTUSD", "XAU": "PAXGUSD"}
KRAKEN_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
ALIASES = {"BTCUSD": "BTC", "BTCUSDT": "BTC", "XAUUSD": "XAU", "GOLD": "XAU"}


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "alert-bot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def closes_binance(sym, tf):
    q = urllib.parse.urlencode({"symbol": BINANCE[sym], "interval": tf, "limit": 200})
    rows = get_json(f"https://data-api.binance.vision/api/v3/klines?{q}")
    return [float(r[4]) for r in rows]


def closes_kraken(sym, tf):
    q = urllib.parse.urlencode({"pair": KRAKEN[sym], "interval": KRAKEN_MIN[tf]})
    data = get_json(f"https://api.kraken.com/0/public/OHLC?{q}")
    if data.get("error"):
        raise RuntimeError(data["error"])
    rows = next(v for k, v in data["result"].items() if k != "last")
    return [float(r[4]) for r in rows][-200:]


def get_closes(sym, tf):
    for fn in (closes_binance, closes_kraken):
        try:
            return fn(sym, tf)
        except Exception as e:  # prova la fonte successiva
            print(f"{fn.__name__} {sym} {tf}: {e}")
    raise RuntimeError(f"nessun dato per {sym} {tf}")


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


def telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram non configurato. Messaggio:", text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read()


def main():
    if "--test" in sys.argv:
        telegram("✅ Test alert: tutto funziona.")
        return

    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    state = {}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)

    default_tf = cfg.get("timeframe", "15m")
    period = int(cfg.get("rsi_period", 14))
    cache = {}

    def data(sym, tf):
        if (sym, tf) not in cache:
            cache[(sym, tf)] = get_closes(sym, tf)
        return cache[(sym, tf)]

    for a in cfg["alerts"]:
        sym = a["symbol"].upper()
        sym = ALIASES.get(sym, sym)
        kind, cond, val = a["type"], a["condition"], float(a["value"])
        tf = a.get("timeframe", default_tf) if kind == "rsi" else "1m"
        key = f"{sym}|{kind}|{cond}|{val:g}|{tf}"
        name = "XAU (PAXG)" if sym == "XAU" else sym

        try:
            c = data(sym, tf)
        except Exception as e:
            print(f"salto {key}: {e}")
            continue

        price = c[-1]
        if kind == "rsi":
            current = rsi(c[:-1], period)  # esclude la candela ancora aperta
            if current is None:
                continue
        else:
            current = price

        hit = current >= val if cond == "above" else current <= val
        was = state.get(key, False)

        if hit and not was:
            verso = "sopra" if cond == "above" else "sotto"
            if kind == "rsi":
                msg = (f"🔔 {name} RSI({period}, {tf}) = {current:.1f}, "
                       f"{verso} {val:g} | prezzo {price:,.2f}")
            else:
                msg = f"🔔 {name} prezzo {price:,.2f}, {verso} il livello {val:g}"
            try:
                telegram(msg)
            except Exception as e:
                print(f"invio fallito, riprovo al prossimo giro: {e}")
                continue  # non aggiorna lo stato: ritenta

        state[key] = hit  # si riarma quando la condizione torna falsa

    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()

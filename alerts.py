#!/usr/bin/env python3
"""Alert RSI e livelli di prezzo per BTC e oro, gestiti da chat Telegram.

Dati: Binance (data-api.binance.vision), con ripiego su Kraken.
Oro: si usa PAXG (token legato all'oro), vicino all'XAUUSD ma non identico.
Comandi Telegram: /alert, /rsi, /lista, /cancella, /aiuto.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "alerts.json")
STATE = os.path.join(BASE, "state.json")

BINANCE = {"BTC": "BTCUSDT", "XAU": "PAXGUSDT"}
KRAKEN = {"BTC": "XBTUSD", "XAU": "PAXGUSD"}
KRAKEN_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
ALIASES = {"BTCUSD": "BTC", "BTCUSDT": "BTC", "BITCOIN": "BTC",
           "XAUUSD": "XAU", "GOLD": "XAU", "ORO": "XAU"}
DIRS = {"sopra": "above", "su": "above", "above": "above",
        "sotto": "below", "giu": "below", "giù": "below", "below": "below"}

HELP = (
    "Comandi:\n"
    "/alert btc sopra 95000\n"
    "/alert oro sotto 4000\n"
    "/rsi btc sopra 75\n"
    "/rsi oro sotto 25 1h\n"
    "/lista  (mostra gli alert)\n"
    "/cancella 2  (cancella l'alert numero 2)\n\n"
    "Timeframe RSI: 5m, 15m, 30m, 1h, 4h, 1d.\n"
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


_cache = {}


def data(sym, tf):
    if (sym, tf) not in _cache:
        _cache[(sym, tf)] = get_closes(sym, tf)
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


# ---------- alert ----------
def alert_tf(a, cfg):
    if a["type"] == "rsi":
        return a.get("timeframe", cfg.get("timeframe", "15m"))
    return "1m"


def alert_key(a, cfg):
    return (f"{norm(a['symbol'])}|{a['type']}|{a['condition']}|"
            f"{fmt(a['value'])}|{alert_tf(a, cfg)}")


def current_value(a, cfg):
    """Ritorna (valore da confrontare, prezzo attuale)."""
    c = data(norm(a["symbol"]), alert_tf(a, cfg))
    price = c[-1]
    if a["type"] == "rsi":
        return rsi(c[:-1], int(cfg.get("rsi_period", 14))), price  # esclude candela aperta
    return price, price


def is_hit(a, cur):
    v = float(a["value"])
    return cur >= v if a["condition"] == "above" else cur <= v


def describe(a, cfg):
    name = "Oro" if norm(a["symbol"]) == "XAU" else "BTC"
    verso = "sopra" if a["condition"] == "above" else "sotto"
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
    if sym not in BINANCE:
        return f"Asset non supportato: {args[0]}. Usa btc o oro.", False
    cond = DIRS.get(args[1])
    if cond is None:
        raise ValueError("direzione")
    val = parse_num(args[2])
    val = int(val) if val == int(val) else val
    a = {"symbol": sym, "type": "rsi" if cmd == "rsi" else "price",
         "condition": cond, "value": val}
    if cmd == "rsi":
        if not 0 < val < 100:
            return "L'RSI deve essere tra 0 e 100.", False
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
    try:
        if cmd == "cancella":
            n = int(args[0])
            if not 1 <= n <= len(cfg["alerts"]):
                return "Numero non valido. Usa /lista.", False
            removed = cfg["alerts"].pop(n - 1)
            return f"🗑 Cancellato: {describe(removed, cfg)}", True
        if cmd in ("alert", "rsi"):
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


def check_alerts(cfg, state):
    new_state = {}
    for a in cfg["alerts"]:
        key = alert_key(a, cfg)
        was = state.get(key, False)
        try:
            cur, price = current_value(a, cfg)
        except Exception as e:
            print(f"salto {key}: {e}")
            new_state[key] = was
            continue
        if cur is None:
            new_state[key] = was
            continue

        hit = is_hit(a, cur)
        if hit and not was:
            name = "XAU (PAXG)" if norm(a["symbol"]) == "XAU" else "BTC"
            verso = "sopra" if a["condition"] == "above" else "sotto"
            if a["type"] == "rsi":
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
        new_state[key] = hit  # si riarma quando la condizione torna falsa
    return new_state


def main():
    if "--test" in sys.argv:
        telegram("✅ Test alert: tutto funziona. Scrivimi /aiuto per i comandi.")
        return
    cfg = load_json(CONFIG, {"alerts": []})
    cfg.setdefault("alerts", [])
    state = load_json(STATE, {})
    handle_commands(cfg, state)
    state = check_alerts(cfg, state)
    save_json(STATE, state)


if __name__ == "__main__":
    main()

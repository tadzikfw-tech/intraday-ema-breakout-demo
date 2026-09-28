#!/usr/bin/env python3
"""Demo: strategia intraday EMA(20) breakout, S&P 500 (^GSPC), aktywna TYLKO w
oknie sesji New York AM (9:30-12:00 czasu nowojorskiego), pozycja zawsze
zamykana na koniec okna - nigdy nie trzyma przez noc. Raz dziennie, tylko
biblioteka standardowa.

WAŻNE: to symulacja na WIRTUALNYCH pieniądzach. Bot nie składa żadnych
prawdziwych zleceń.

GENEZA: backtest na 2 latach danych godzinowych (60m) pokazał BRAK przewagi
tej reguły nawet bez kosztów transakcyjnych (-1,44% zwrotu brutto, win rate
35% vs 29% strat, reszta płasko). Zamiast szukać "lepszych" danych historycznych
pod ten sam wniosek (ryzyko naciągania wyniku - p-hacking), robimy coś
uczciwszego: zbieramy WŁASNĄ historię świec 5-minutowych dzień po dniu (Yahoo
Finance nie daje więcej niż ~60-85 dni historii 5m za jednym razem, więc
musimy ją budować sami, tak jak z głównym demo na S&P 500). Dopiero po kilku
miesiącach realnych danych będzie sens wyciągać wnioski.

REGUŁA (identyczna logicznie do tej przetestowanej na 60m, przeniesiona na 5m
bez zmian parametrów - nie dostrajamy pod nową ramę czasową):
  - EMA(20) liczona na CIĄGŁEJ serii świec 5-minutowych (nie resetowana
    codziennie)
  - w oknie sesji New York AM (9:30-12:00 ET): jeśli cena > EMA(20) -> target
    ekspozycji 50% kapitału, inaczej 0%
  - OSTATNIA świeca okna (tuż przed 12:00 ET) zawsze wymusza FLAT (0%) -
    pozycja nigdy nie przechodzi poza okno sesji
  - bez dźwigni, bez shortów (te same zasady co główne demo)

BRAK "zasad challenge'u prop firmy" na razie - to świadoma decyzja. Owijanie
tego w reguły prop firmy miałoby sens dopiero, gdyby strategia pokazała
jakąkolwiek przewagę na realnych danych. Na razie to czysty tracker equity
samej reguły.
"""
import os, json, csv, time, datetime, urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
DOCS = os.path.join(ROOT, "docs")
STATE = os.path.join(DATA, "state.json")
CANDLES = os.path.join(DATA, "candles_5m.csv")
TRADES = os.path.join(DATA, "trades.csv")
HIST = os.path.join(DATA, "history.csv")

NY = ZoneInfo("America/New_York")
CAPITAL = 10_000.0
EXPOSURE_CAP = 0.50
EMA_LEN = 20
FEE, SLIP = 0.0, 0.0001   # ten sam model kosztów co reszta projektu (spread ~0,01%, zero prowizji)


# ------------------------------------------------------------------ dane
def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (intraday-ema-breakout-demo)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_5m():
    d = _get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=60d&interval=5m")
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    return [(t, c) for t, c in zip(ts, closes) if c is not None]


def load_archive():
    if not os.path.exists(CANDLES):
        return {}
    out = {}
    for row in csv.reader(open(CANDLES, encoding="utf-8")):
        if row and row[0] != "ts":
            out[int(row[0])] = float(row[1])
    return out


def save_archive(archive):
    tmp = CANDLES + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "close"])
        for ts in sorted(archive):
            w.writerow([ts, "%.4f" % archive[ts]])
    os.replace(tmp, CANDLES)


# ------------------------------------------------------------------ strategia
def ema(values, span):
    k, out = 2.0 / (span + 1), [values[0]]
    for x in values[1:]:
        out.append(k * x + (1 - k) * out[-1])
    return out


def am_window_bars(ts_l):
    """Zwraca indeksy świec w oknie sesji New York AM (9:30-12:00 ET), pogrupowane
    po dniu, w kolejności chronologicznej. Ostatnia świeca w każdym dniu = wymuszony flat."""
    by_day = {}
    for i, ts in enumerate(ts_l):
        dt = datetime.datetime.fromtimestamp(ts, tz=NY)
        if (dt.hour == 9 and dt.minute >= 30) or dt.hour == 10 or (dt.hour == 11 and dt.minute <= 55):
            by_day.setdefault(dt.date(), []).append(i)
    return by_day


# ------------------------------------------------------------------ symulacja konta
def load_state():
    return json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else None


def save_state(state):
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, STATE)


def append_csv(path, header, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def read_csv(path):
    return list(csv.DictReader(open(path, encoding="utf-8"))) if os.path.exists(path) else []


# ------------------------------------------------------------------ raport
def write_report(state, ts_l, closes, src_days, hist_rows):
    eq = state["cash"] + state["qty"] * closes[-1]
    n_sessions = state.get("sessions_done", 0)
    wins, losses, flats = state.get("wins", 0), state.get("losses", 0), state.get("flats", 0)
    win_rate = wins / n_sessions * 100 if n_sessions else 0.0
    day0 = state.get("first_session_date", "-")
    day1 = state.get("last_session_date", "-")
    ret_pct = (eq / CAPITAL - 1) * 100

    rows_hist = "".join(
        "<tr><td>%s</td><td>%.2f</td><td style='color:%s'>%+.2f%%</td></tr>" % (
            h["date"], float(h["equity"]), "#4cc38a" if float(h["session_pnl"]) >= 0 else "#e5534b", float(h["session_pnl"]) * 100)
        for h in reversed(hist_rows[-60:])) or "<tr><td colspan='3' class='muted'>Jeszcze żadna pełna sesja.</td></tr>"

    html = """<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Demo: EMA breakout intraday (NY AM)</title>
<style>body{background:#0e1116;color:#e6e9ef;font:15px/1.5 Segoe UI,Arial,sans-serif;margin:0;padding:16px;max-width:980px;margin:auto}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 8px;color:#9fb0c8}
.big{font-size:40px;font-weight:700;margin:4px 0}.card{background:#161b22;border:1px solid #262d38;border-radius:10px;padding:14px 16px;margin:10px 0}
table{width:100%%;border-collapse:collapse;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid #232a34;text-align:left}th{color:#8b98ab}.muted{color:#7d8899}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}.k{color:#8b98ab;font-size:12px}.v{font-size:20px;font-weight:600}
.warn{background:#3a2a10;border:1px solid #7a5a1a;padding:10px 14px;border-radius:8px;color:#f0c674;margin:10px 0}</style></head><body>
<h1>Demo: EMA(20) breakout intraday, sesja New York AM</h1>
<div class="warn">To symulacja na WIRTUALNYCH pieniądzach. Świece 5-minutowe (^GSPC) budowane WŁASNORĘCZNIE
dzień po dniu od %(d0)s (Yahoo Finance nie daje głębszej historii 5m niż ~60-85 dni na raz).
Backtest na 2 latach danych 60-minutowych pokazał BRAK przewagi tej reguły nawet bez kosztów -
ten tracker sprawdza, czy realne dane 5-minutowe pokażą coś innego. Za wcześnie na wnioski.</div>
<div class="muted">Stan na koniec dnia %(d1)s (NY). Zebranych świec 5m: %(nc)d.</div>
<div class="card"><div class="big">%(eq).2f USD</div>
<div class="muted">start: %(cap).0f USD · zwrot od startu: %(ret)+.2f%%</div></div>
<div class="grid"><div class="card"><div class="k">Sesji AM ukończonych</div><div class="v">%(ns)d</div></div>
<div class="card"><div class="k">Wygrane / przegrane / płaskie</div><div class="v">%(w)d / %(l)d / %(f)d</div></div>
<div class="card"><div class="k">Win rate</div><div class="v">%(wr).0f%%</div></div></div>
<h2>Historia sesji (ostatnie 60)</h2><div class="card"><table><tr><th>Data</th><th>Equity</th><th>Wynik sesji</th></tr>%(rows)s</table></div>
<div class="muted" style="margin-top:20px">Reguła: EMA(20) na ciągłej serii 5m, sygnał tylko 9:30-12:00 ET, wymuszony flat na koniec okna. Koszty: spread ~0,01%%, zero prowizji.</div>
</body></html>""" % dict(d0=day0, d1=day1, nc=len(ts_l), eq=eq, cap=CAPITAL, ret=ret_pct,
                          ns=n_sessions, w=wins, l=losses, f=flats, wr=win_rate, rows=rows_hist)
    os.makedirs(DOCS, exist_ok=True)
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(html)
    open(os.path.join(DOCS, ".nojekyll"), "w").write("")


# ------------------------------------------------------------------ główny przebieg
def run():
    os.makedirs(DATA, exist_ok=True)
    fresh = fetch_5m()
    archive = load_archive()
    for ts, c in fresh:
        archive[ts] = c
    save_archive(archive)

    ts_l = sorted(archive)
    closes = [archive[t] for t in ts_l]
    now = int(time.time())
    # nie liczymy sygnalu na jeszcze-formujacej sie ostatniej swiecy (mniej niz 5 min od teraz)
    while ts_l and ts_l[-1] + 300 > now:
        ts_l.pop()
        closes.pop()
    if len(closes) < EMA_LEN + 1:
        print("Za mało świec do policzenia EMA(%d) - czekam na więcej danych." % EMA_LEN)
        return

    ema20 = ema(closes, EMA_LEN)
    by_day = am_window_bars(ts_l)

    state = load_state() or dict(cash=CAPITAL, qty=0.0, processed_days=[], sessions_done=0,
                                  wins=0, losses=0, flats=0)
    processed = set(state["processed_days"])
    hist_rows = read_csv(HIST)
    new_hist, new_trades = [], []

    for day in sorted(by_day):
        key = day.isoformat()
        if key in processed:
            continue
        idxs = by_day[day]
        if len(idxs) < 2:
            continue  # niepelny dzien (np. wciaz w toku) - poczekamy na kolejny run
        eq_before = state["cash"] + state["qty"] * closes[idxs[0]]
        for pos, i in enumerate(idxs):
            price = closes[i]
            is_last = (pos == len(idxs) - 1)
            target = 0.0 if is_last else (EXPOSURE_CAP if price > ema20[i] else 0.0)
            eq = state["cash"] + state["qty"] * price
            delta = target * eq - state["qty"] * price
            if abs(delta) >= 1.0:
                if delta > 0:
                    px = price * (1 + SLIP)
                    q = delta / px
                    state["cash"] -= delta
                    state["qty"] += q
                else:
                    px = price * (1 - SLIP)
                    q = min(-delta / px, state["qty"])
                    state["cash"] += q * px
                    state["qty"] -= q
                new_trades.append([ts_l[i], key, target, price])
        eq_after = state["cash"] + state["qty"] * closes[idxs[-1]]
        session_pnl = eq_after / eq_before - 1
        state["sessions_done"] += 1
        if session_pnl > 1e-9:
            state["wins"] += 1
        elif session_pnl < -1e-9:
            state["losses"] += 1
        else:
            state["flats"] += 1
        new_hist.append([key, "%.4f" % eq_after, "%.6f" % session_pnl])
        processed.add(key)
        if "first_session_date" not in state:
            state["first_session_date"] = key
        state["last_session_date"] = key

    state["processed_days"] = sorted(processed)
    append_csv(HIST, ["date", "equity", "session_pnl"], new_hist)
    append_csv(TRADES, ["ts", "date", "target", "price"], new_trades)
    save_state(state)

    hist_rows = read_csv(HIST)
    write_report(state, ts_l, closes, sorted(by_day), hist_rows)
    print("Swiec w archiwum: %d. Sesji AM ukonczonych: %d. Equity: %.2f USD" %
          (len(ts_l), state["sessions_done"], state["cash"] + state["qty"] * closes[-1]))


if __name__ == "__main__":
    run()

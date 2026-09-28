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

ZASADY "CHALLENGE'U" (2026-09-28, ujednolicone z głównym demo prop-challenge-demo -
identyczne co do liczby, żeby oba tory były bezpośrednio porównywalne):
  - wirtualny kapitał konta: 50 000 USD (FTMO 2-Step, potwierdzona cena 365 USD)
  - cel zysku (faza ewaluacji): +8%
  - limit straty dziennej: 5% - tu liczony PER SESJA AM (nie per dzień kalendarzowy,
    bo poza sesją nie handlujemy), od equity na koniec poprzedniej sesji
  - limit straty całkowitej: 10% od startu próby (ewaluacja) / 10% od szczytu
    (sfinansowane, trailing)
  - limity sprawdzane PO KAŻDYM barze w sesji, nie tylko na koniec - bardziej
    realistyczne niż raz dziennie, bo w tej ramie czasowej sesja ma kilka-
    kilkanaście barów
  - złamanie limitu = koniec próby, nowa próba startuje NATYCHMIAST (jeszcze
    w tej samej sesji, na pozostałych barach) - inaczej niż w głównym demo
    (tam nowa próba czeka do następnego dnia), bo tu "dzień" to sesja, a
    sesja i tak kończy się za chwilę
  - podział zysku na koncie sfinansowanym: 80% dla tradera
"""
import os, json, csv, time, datetime, urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
DOCS = os.path.join(ROOT, "docs")
STATE = os.path.join(DATA, "state.json")
CANDLES = os.path.join(DATA, "candles_5m.csv")
TRADES = os.path.join(DATA, "trades.csv")
ATTEMPTS = os.path.join(DATA, "attempts.csv")
HIST = os.path.join(DATA, "history.csv")

NY = ZoneInfo("America/New_York")
EXPOSURE_CAP = 0.50
EMA_LEN = 20
FEE, SLIP = 0.0, 0.0001   # ten sam model kosztow co reszta projektu (spread ~0,01%, zero prowizji)

CAPITAL = 50_000.0
TARGET_PROFIT = 0.08
DAILY_LOSS_LIMIT = 0.05
EVAL_MAX_LOSS = 0.10
FUNDED_MAX_LOSS = 0.10          # trailing od szczytu
PROFIT_SPLIT = 0.80
FEE_USD = 365.0                  # realna, potwierdzona cena FTMO 2-Step 50k


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


# ------------------------------------------------------------------ konto (rebalans jak w glownym demo)
def equity(acc, price):
    return acc["cash"] + acc["qty"] * price


def rebalance(acc, price, target, log_key, log):
    eq = equity(acc, price)
    delta = target * eq - acc["qty"] * price
    acc["target"] = target
    if abs(delta) < 1.0:
        return
    if delta > 0:
        n = min(delta, acc["cash"] / (1 + FEE))
        px = price * (1 + SLIP)
        q, fee = n / px, n * FEE
        acc["cash"] -= n + fee
        acc["qty"] += q
    else:
        px = price * (1 - SLIP)
        q = min(-delta / px, acc["qty"])
        n = q * px
        fee = n * FEE
        acc["cash"] += n - fee
        acc["qty"] -= q
    log.append((log_key, target, price))


def new_account(capital, price, target, log_key, log):
    acc = dict(cash=capital, qty=0.0, target=0.0)
    rebalance(acc, price, target, log_key, log)
    return acc


def new_attempt(no, phase, start_equity, price, log_key, log):
    acc = new_account(start_equity, price, 0.0, log_key, log)
    return dict(no=no, phase=phase, start_equity=start_equity, peak_equity=start_equity,
                prev_close_equity=start_equity, acc=acc, start_date=log_key)


def close_attempt(att, price, date_key, result, attempts_log, funded_stats):
    eq = equity(att["acc"], price)
    pnl = eq / att["start_equity"] - 1
    attempts_log.append(dict(no=att["no"], phase=att["phase"], start=att["start_date"], end=date_key,
                              result=result, start_eq=att["start_equity"], end_eq=eq, pnl=pnl))
    if att["phase"] == "SFINANSOWANE":
        profit_usd = max(eq - att["start_equity"], 0.0)
        funded_stats["payout_usd"] += profit_usd * PROFIT_SPLIT
        funded_stats["funded_sessions"] += 1
    return eq


# ------------------------------------------------------------------ pliki
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
def write_report(state, price_now, src_n, funded_stats):
    att = state["att"]
    eq = equity(att["acc"], price_now)
    passed, failed = state["attempts_passed"], state["attempts_failed"]
    finished = passed + failed
    rate = (passed / finished * 100) if finished else 0.0
    n_total = state["n"]
    est_fees = FEE_USD * n_total
    payout = funded_stats["payout_usd"]
    net = payout - est_fees
    phase_lbl = ("FAZA EWALUACJI — próba #%d" % att["no"]) if att["phase"] == "EWALUACJA" else ("KONTO SFINANSOWANE — od próby #%d" % att["no"])
    phase_col = "#6ea8ff" if att["phase"] == "EWALUACJA" else "#4cc38a"

    attempts = read_csv(ATTEMPTS)
    rows_att = "".join(
        "<tr><td>#%s</td><td>%s</td><td>%s</td><td>%s</td><td style='color:%s'>%s</td><td style='color:%s'>%+.1f%%</td></tr>" % (
            a["no"], a["phase"], a["start"], a["end"],
            "#4cc38a" if a["result"] == "ZDANE" else "#e5534b", a["result"],
            "#4cc38a" if float(a["pnl"]) >= 0 else "#e5534b", float(a["pnl"]) * 100)
        for a in reversed(attempts[-30:])) or "<tr><td colspan='6' class='muted'>Jeszcze żadna próba się nie zakończyła.</td></tr>"

    html = """<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Demo: EMA breakout intraday (NY AM) vs prop-konto</title>
<style>body{background:#0e1116;color:#e6e9ef;font:15px/1.5 Segoe UI,Arial,sans-serif;margin:0;padding:16px;max-width:980px;margin:auto}h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:26px 0 8px;color:#9fb0c8}
.big{font-size:40px;font-weight:700;margin:4px 0}.card{background:#161b22;border:1px solid #262d38;border-radius:10px;padding:14px 16px;margin:10px 0}
table{width:100%%;border-collapse:collapse;font-size:13px}th,td{padding:6px 8px;border-bottom:1px solid #232a34;text-align:left}th{color:#8b98ab}.muted{color:#7d8899}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}.k{color:#8b98ab;font-size:12px}.v{font-size:20px;font-weight:600}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:13px;font-weight:600}
.warn{background:#3a2a10;border:1px solid #7a5a1a;padding:10px 14px;border-radius:8px;color:#f0c674;margin:10px 0}</style></head><body>
<h1>Demo: EMA(20) breakout intraday, sesja New York AM — pod zasadami prop-konta</h1>
<div class="warn">To symulacja na WIRTUALNYCH pieniądzach. Świece 5-minutowe (^GSPC) budowane WŁASNORĘCZNIE
dzień po dniu (Yahoo Finance nie daje głębszej historii 5m niż ~60-85 dni na raz).
Backtest na 2 latach danych 60-minutowych pokazał BRAK przewagi tej reguły nawet bez kosztów -
ten tracker sprawdza, czy realne dane 5-minutowe i reguły prop-konta pokażą coś innego. Za wcześnie na wnioski.</div>
<div class="muted">Zebranych świec 5m: %(nc)d.</div>
<div class="card"><span class="badge" style="background:%(pcol)s22;color:%(pcol)s">%(plbl)s</span>
<div class="big">%(eq).2f USD</div>
<div class="muted">start tej próby: %(start).0f USD</div></div>
<h2>Wynik od początku testu</h2>
<div class="grid"><div class="card"><div class="k">Prób ewaluacji (ukończonych)</div><div class="v">%(fin)d</div></div>
<div class="card"><div class="k">Zdanych</div><div class="v">%(passed)d (%(rate).0f%%)</div></div>
<div class="card"><div class="k">Niezdanych</div><div class="v">%(failed)d</div></div></div>
<div class="grid"><div class="card"><div class="k">Szacowany koszt prób (%(feeone).0f USD/próba)</div><div class="v">%(fees).0f USD</div></div>
<div class="card"><div class="k">Symulowana wypłata (80%% zysku)</div><div class="v">%(payout).2f USD</div></div>
<div class="card"><div class="k">Bilans: wypłata − koszt prób</div><div class="v" style="color:%(ncol)s">%(net)+.2f USD</div></div></div>
<h2>Historia prób</h2><div class="card"><table><tr><th>Nr</th><th>Faza</th><th>Start</th><th>Koniec</th><th>Wynik</th><th>Zmiana kapitału</th></tr>%(rows)s</table></div>
<div class="muted" style="margin-top:20px">Reguła: EMA(20) na ciągłej serii 5m, sygnał tylko 9:30-12:00 ET, wymuszony flat na koniec okna.
Zasady konta identyczne z głównym demo (prop-challenge-demo): 50k, +8%%/5%%/10%%, FTMO 2-Step 365 USD, split 80%%.</div>
</body></html>""" % dict(nc=src_n, pcol=phase_col, plbl=phase_lbl, eq=eq, start=att["start_equity"],
                          fin=finished, passed=passed, rate=rate, failed=failed,
                          fees=est_fees, feeone=FEE_USD, payout=payout, ncol=("#4cc38a" if net >= 0 else "#e5534b"), net=net,
                          rows=rows_att)
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
    while ts_l and ts_l[-1] + 300 > now:
        ts_l.pop()
        closes.pop()
    if len(closes) < EMA_LEN + 1:
        print("Za mało świec do policzenia EMA(%d) - czekam na więcej danych." % EMA_LEN)
        return

    ema20 = ema(closes, EMA_LEN)
    by_day = am_window_bars(ts_l)

    raw = load_state()
    trades_log, attempts_log = [], []
    funded_stats = {"payout_usd": 0.0, "funded_sessions": 0}

    days_sorted = sorted(by_day)
    if raw is None:
        first_day = None
        for day in days_sorted:
            if len(by_day[day]) >= 2:
                first_day = day
                break
        if first_day is None:
            print("Brak pelnej sesji AM w danych - czekam.")
            return
        idxs0 = by_day[first_day]
        state = dict(n=1, attempts_passed=0, attempts_failed=0, funded_losses=0,
                     processed_days=[], cum_payout_usd=0.0,
                     att=new_attempt(1, "EWALUACJA", CAPITAL, closes[idxs0[0]], first_day.isoformat(), trades_log))
        print("START demo: próba #1, kapitał %.0f USD, cel +%.0f%%" % (CAPITAL, TARGET_PROFIT * 100))
    else:
        state = raw

    processed = set(state["processed_days"])

    for day in days_sorted:
        key = day.isoformat()
        if key in processed:
            continue
        idxs = by_day[day]
        if len(idxs) < 2:
            continue  # niepelny dzien (wciaz w toku) - poczekamy na kolejny run

        for pos, i in enumerate(idxs):
            price = closes[i]
            is_last = (pos == len(idxs) - 1)
            target = 0.0 if is_last else (EXPOSURE_CAP if price > ema20[i] else 0.0)

            att = state["att"]
            acc = att["acc"]
            rebalance(acc, price, target, key, trades_log)
            eq = equity(acc, price)
            att["peak_equity"] = max(att["peak_equity"], eq)

            daily_dd = eq / att["prev_close_equity"] - 1
            if att["phase"] == "EWALUACJA":
                total_dd = eq / att["start_equity"] - 1
                loss_limit = EVAL_MAX_LOSS
            else:
                total_dd = eq / att["peak_equity"] - 1
                loss_limit = FUNDED_MAX_LOSS

            result = None
            if daily_dd <= -DAILY_LOSS_LIMIT:
                result = "PRZEKROCZONY LIMIT DZIENNY"
            elif total_dd <= -loss_limit:
                result = "PRZEKROCZONY LIMIT CAŁKOWITY"
            elif att["phase"] == "EWALUACJA" and eq / att["start_equity"] - 1 >= TARGET_PROFIT:
                result = "ZDANE"

            if result in ("PRZEKROCZONY LIMIT DZIENNY", "PRZEKROCZONY LIMIT CAŁKOWITY"):
                close_attempt(att, price, key, result, attempts_log, funded_stats)
                state["attempts_failed"] += 1
                if att["phase"] == "SFINANSOWANE":
                    state["funded_losses"] += 1
                state["n"] += 1
                state["att"] = new_attempt(state["n"], "EWALUACJA", CAPITAL, price, key, trades_log)
            elif result == "ZDANE":
                eq_closed = close_attempt(att, price, key, result, attempts_log, funded_stats)
                state["attempts_passed"] += 1
                state["att"] = new_attempt(state["n"], "SFINANSOWANE", eq_closed, price, key, trades_log)
            # jesli bez naruszenia - att["prev_close_equity"] zaktualizujemy po ostatnim barze sesji

        # koniec sesji (dzien przetworzony) - zaktualizuj prev_close_equity aktywnej proby
        att = state["att"]
        state["att"]["prev_close_equity"] = equity(att["acc"], closes[idxs[-1]])
        processed.add(key)

    state["processed_days"] = sorted(processed)
    append_csv(ATTEMPTS, ["no", "phase", "start", "end", "result", "start_eq", "end_eq", "pnl"],
               [[a["no"], a["phase"], a["start"], a["end"], a["result"],
                 "%.2f" % a["start_eq"], "%.2f" % a["end_eq"], "%.4f" % a["pnl"]] for a in attempts_log])
    append_csv(TRADES, ["date", "target", "price"], trades_log)

    state["cum_payout_usd"] = state.get("cum_payout_usd", 0.0) + funded_stats["payout_usd"]
    save_state(state)

    price_now = closes[-1]
    write_report(state, price_now, len(ts_l), dict(payout_usd=state["cum_payout_usd"]))
    print("Swiec w archiwum: %d. Faza: %s (proba #%d), equity: %.2f USD" %
          (len(ts_l), state["att"]["phase"], state["att"]["no"], equity(state["att"]["acc"], price_now)))


if __name__ == "__main__":
    run()

# Demo: EMA(20) breakout intraday, sesja New York AM

Osobny, równoległy tor obok głównego demo (`prop-challenge-demo`, S&P 500 dzienny).
Sprawdza, czy szybsze transakcje (intraday) dają jakąkolwiek przewagę, zamiast
zakładać, że "szybciej = więcej zysku".

**Reguła:** EMA(20) liczona na ciągłej serii świec 5-minutowych (^GSPC). W oknie
sesji New York AM (9:30-12:00 czasu nowojorskiego): jeśli cena > EMA(20) ->
ekspozycja 50% kapitału, inaczej 0%. Ostatnia świeca okna zawsze wymusza FLAT -
pozycja nigdy nie przechodzi poza okno sesji, nigdy nie trzyma przez noc.

## Geneza

Backtest tej samej reguły na 2 latach danych **godzinowych** (60m, Yahoo Finance)
pokazał BRAK przewagi nawet bez kosztów transakcyjnych: -1,44% zwrotu brutto,
win rate 35% (vs 29% strat, reszta płasko), średni dzienny zwrot sesji -0,0057%
(czysty szum).

Świece 5-minutowe dałyby bardziej "prawdziwy" sygnał breakoutu, ale Yahoo
Finance nie daje głębszej historii 5m niż ~60-85 dni na raz - za mało na
wiarygodny backtest. Sprawdzone alternatywy (Dukascopy, HistData.com, FXReplay)
albo nieosiągalne z tego środowiska, albo wymagają łamania zabezpieczeń
antybotowych (czego świadomie nie robimy), albo nie są w ogóle źródłem
danych (FXReplay to symulator do ręcznego treningu, nie API).

**Rozwiązanie: bot sam buduje własną historię 5-minutową dzień po dniu**, tak
jak główne demo budowało własną historię dzienną. Dopiero po kilku miesiącach
realnych danych będzie sens wyciągać wnioski, czy ta rama czasowa pokazuje coś
innego niż godzinowa.

**To symulacja na wirtualnych pieniądzach.** Bot nie składa żadnych prawdziwych
zleceń. Brak "zasad challenge'u prop firmy" na razie - to świadoma decyzja,
sensowna dopiero gdy strategia pokaże jakąkolwiek przewagę na realnych danych.

**Wyniki:** GitHub Pages → link w ustawieniach repozytorium (Settings → Pages),
aktualizacja codziennie pon-pt ok. 21:30 UTC (po zamknięciu sesji NY).

## Pliki

- `bot.py` — cała logika (biblioteka standardowa Pythona)
- `.github/workflows/daily.yml` — harmonogram (GitHub Actions)
- `data/candles_5m.csv` — własnoręcznie budowana historia świec 5-minutowych
- `data/` — stan konta, historia sesji (zapisywane automatycznie)
- `docs/index.html` — raport (generowany automatycznie)

## Wyłączenie

GitHub → zakładka **Actions** → workflow „dzienny-bot" → menu „…" → **Disable workflow**.

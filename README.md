# BigData – mecze rankingowe League of Legends w MongoDB

Aplikacja pobiera mecze rankingowe (Solo/Duo) z Riot Games API, importuje je do lokalnej bazy MongoDB,
czyści i analizuje statystycznie, a wyniki przedstawia na wykresach i w tabelach.

| plik | rola |
|---|---|
| `docker-compose.yml` | lokalna baza MongoDB 8.0 w Dockerze (port `127.0.0.1:27017`, wolumen `lol-mongo-data`) |
| `importer.py` | import surowych plików `*.jsonl.gz` do kolekcji `matches`, `timelines`, `ranks` |
| `BigData.ipynb` | sprawdzanie i czyszczenie danych, analizy statystyczne, wizualizacje |
| `import_stats.json` | statystyki ostatniego importu (rekordy, duplikaty, rozmiar po rozpakowaniu) |
| `requirements.txt` | biblioteki Pythona |
| `sprawozdanie/` | sprawozdanie: treść w `sprawozdanie.md`, PDF składany poleceniem `python build_pdf.py` (wymaga `markdown` i `weasyprint`) |

## 1. Dane

Najprościej skorzystać z gotowych danych udostępnionych przez autorów (link przekazany razem z projektem).
Folder `raw/` należy pobrać i rozpakować w dowolnym miejscu, zachowując strukturę katalogów:

```
raw/
  matches/platform=EUN1/part-0001.jsonl.gz ...
  timelines/platform=EUN1/part-0001.jsonl.gz ...
  ranks/platform=EUN1/part-0001.jsonl.gz ...
```

Plików `.jsonl.gz` nie trzeba rozpakowywać, importer czyta je bezpośrednio.

### Samodzielne pobranie z API

Źródło: oficjalne [Riot Games API](https://developer.riotgames.com/apis): `match-v5` (mecze i timeline'y)
oraz `league-v4` (rangi graczy). Dane pobiera osobna aplikacja
[League-Of-Legends-Data-Collector](https://github.com/Stifanox/League-Of-Legends-Data-Collector):

```bash
git clone https://github.com/Stifanox/League-Of-Legends-Data-Collector
cd League-Of-Legends-Data-Collector
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # wpisz klucz z https://developer.riotgames.com do RIOT_API_KEY
.venv/bin/python -m crawler crawl --duration 48h
```

Surowe odpowiedzi API trafiają do `data/raw/{matches,timelines,ranks}/platform=XXX/part-NNNN.jsonl.gz`.
Zbiór użyty w projekcie: ok. 203 tys. meczów z serwerów EUN1, EUW1, KR i NA1 (30.09–21.10.2026,
patche 16.19–16.21), 16,4 GB spakowane, **ok. 172 GB po rozpakowaniu**.

## 2. Baza MongoDB

```bash
cd bigdata
sudo docker compose up -d          # start
sudo docker compose down           # zatrzymanie (dane zostają w wolumenie)
sudo docker compose down -v        # usunięcie razem z danymi
```

## 3. Import

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python importer.py --raw-dir /ścieżka/do/raw --workers 8      # ok. 5 min dla pełnego zbioru
```

Zamiast `--raw-dir` można ustawić zmienną środowiskową `LOL_RAW_DIR`. Domyślna ścieżka to
`~/Projects/League-Of-Legends-Data-Collector/data/raw`. Import można powtarzać: duplikaty odrzuca unikalny `_id`.
Importer działa także na części plików (np. bez części timeline'ów); analiza przewagi złota obejmie wtedy
tylko mecze, dla których jest timeline.

## 4. Analiza

Otwórz `BigData.ipynb` i uruchom wszystkie komórki (ok. 2 min). Notebook buduje oczyszczoną kolekcję
`games` i wykonuje cztery analizy: przewaga strony mapy, znaczenie pierwszych celów, przewaga złota
w trakcie gry (regresja logistyczna, comebacki) oraz skuteczność championów.

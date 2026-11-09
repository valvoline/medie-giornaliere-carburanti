#!/usr/bin/env python3
"""Calcola le medie giornaliere dei prezzi dei carburanti dai dati MIMIT.

Ricostruzione completa dagli archivi trimestrali:
    python3 build_stats_archive.py --cache <cartella per i download>

Aggiornamento quotidiano, eseguito da GitHub Actions:
    python3 build_stats_archive.py --update --cache <cartella per i download>

Le regole devono restare uguali a quelle dell'app che usa questi dati, altrimenti
lo storico e i giorni calcolati sul telefono non sono confrontabili.
"""

import argparse
import datetime as dt
import json
import statistics
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ARCHIVE_URL = "https://opendatacarburanti.mise.gov.it/categorized/prezzo_alle_8/{year}/{year}_{quarter}_tr.tar.gz"
STATIONS_URL = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"
PRICES_URL = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"
OUTPUT = Path(__file__).resolve().parent / "site/stats.json"
USER_AGENT = "medie-giornaliere-carburanti"

QUARTERS = 8
FRESH_DAYS = 14
PLAUSIBLE_RATIOS = (0.55, 1.7)
MINIMUM_STATIONS = 3
MAXIMUM_REJECTED_ROWS = 0.01
MAXIMUM_MISSING_DAYS = 0.05
DOWNLOAD_ATTEMPTS = 4

# Controlli sul file del giorno: meglio saltare un aggiornamento che pubblicare numeri sbagliati.
MINIMUM_DAILY_ROWS = 50_000
MINIMUM_STATIONS_WITH_REGION = 15_000
MAXIMUM_DAILY_CHANGE = 0.25
COMPARISON_DAYS = 14
MAXIMUM_FILE_AGE_DAYS = 3

NATIONAL = "Italia"
FUELS = ("Diesel", "Benzina", "GPL", "Metano")
BASE_DESCRIPTIONS = {"Diesel": "gasolio", "Benzina": "benzina", "GPL": "gpl", "Metano": "metano"}
STRAY_COLUMN = "gestori.prezzibenzina.it"

REGIONS = {
    "Abruzzo": "AQ CH PE TE",
    "Basilicata": "MT PZ",
    "Calabria": "CS CZ KR RC VV",
    "Campania": "AV BN CE NA SA",
    "Emilia-Romagna": "BO FC FE MO PC PR RA RE RN",
    "Friuli-Venezia Giulia": "GO PN TS UD",
    "Lazio": "FR LT RI RM VT",
    "Liguria": "GE IM SP SV",
    "Lombardia": "BG BS CO CR LC LO MB MI MN PV SO VA",
    "Marche": "AN AP FM MC PU",
    "Molise": "CB IS",
    "Piemonte": "AL AT BI CN NO TO VB VC",
    "Puglia": "BA BR BT FG LE TA",
    "Sardegna": "CA NU OR SS SU",
    "Sicilia": "AG CL CT EN ME PA RG SR TP",
    "Toscana": "AR FI GR LI LU MS PI PO PT SI",
    "Trentino-Alto Adige": "BZ TN",
    "Umbria": "PG TR",
    "Valle d'Aosta": "AO",
    "Veneto": "BL PD RO TV VE VI VR",
}
REGION_OF_PROVINCE = {code: region for region, codes in REGIONS.items() for code in codes.split()}


def fuel_of(description):
    lowered = description.lower()
    if "gasolio" in lowered or "diesel" in lowered:
        return "Diesel"
    if "benzina" in lowered:
        return "Benzina"
    if "gpl" in lowered:
        return "GPL"
    if "metano" in lowered:
        return "Metano"
    return None


def download(url, destination):
    """Scarica `url` e restituisce False se non esiste. Il server MIMIT ha errori TLS temporanei."""
    if destination.exists():
        return True
    print(f"Scarico {url}", flush=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            partial.rename(destination)
            return True
        except (urllib.error.URLError, OSError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code == 404:
                return False
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            print(f"Tentativo {attempt} fallito: {error}", flush=True)
            time.sleep(20 * attempt)
    return False


def last_complete_quarters(today, cache):
    """Gli ultimi trimestri pubblicati: il MIMIT pubblica ciascuno dopo la sua fine."""
    year, quarter = today.year, (today.month - 1) // 3 + 1
    found = []
    while len(found) < QUARTERS:
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
        path = cache / f"{year}_{quarter}_tr.tar.gz"
        if download(ARCHIVE_URL.format(year=year, quarter=quarter), path):
            found.append(path)
        elif found:
            sys.exit(f"Manca il trimestre {year}_{quarter} in mezzo allo storico")
    return list(reversed(found))


def read_separator(lines):
    """Consuma le righe fino all'intestazione compresa e ne restituisce il separatore.

    Fino al 2025 era il punto e virgola, dal 2026 la barra verticale. Le righe
    "Estrazione del" non ne hanno, e il 30/12/2024 erano due.
    """
    for line in lines:
        if ";" in line and "|" not in line:
            return ";"
        if "|" in line:
            return "|"
    return "|"


def regions_by_station(path):
    regions = {}
    with path.open(encoding="utf-8", errors="replace") as lines:
        separator = read_separator(lines)
        for line in lines:
            fields = [field for field in line.rstrip("\n").split(separator) if field.strip() != STRAY_COLUMN]
            if len(fields) < 10:
                continue
            try:
                station = int(fields[0])
            except ValueError:
                continue
            region = REGION_OF_PROVINCE.get(fields[-3].strip())
            if region:
                regions[station] = region
    return regions


def parse_day(lines, separator):
    rows = []
    rejected = 0
    for line in lines:
        fields = line.rstrip("\n").split(separator)
        if len(fields) != 5:
            rejected += 1
            continue
        try:
            rows.append((
                int(fields[0]),
                fields[1].strip(),
                float(fields[2]),
                fields[3].strip() == "1",
                dt.datetime.strptime(fields[4].strip(), "%d/%m/%Y %H:%M:%S"),
            ))
        except ValueError:
            rejected += 1
    return rows, rejected


def check_rejected(name, rows, rejected):
    if rejected > (len(rows) + rejected) * MAXIMUM_REJECTED_ROWS:
        sys.exit(f"{name}: {rejected} righe illeggibili su {len(rows) + rejected}, formato cambiato?")


def daily_means(rows, day, regions):
    """Per ogni area e carburante, la media tra gli impianti del prezzo che l'app mostrerebbe."""
    limit = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min)
    dates = [row[4] for row in rows if row[4] <= limit]
    if not dates:
        return {}
    newest = max(dates)
    fresh_after = newest - dt.timedelta(days=FRESH_DAYS)
    fresh = [row for row in rows if fresh_after <= row[4] <= limit]

    samples = defaultdict(list)
    for _, description, price, _, _ in fresh:
        fuel = fuel_of(description)
        if fuel:
            samples[fuel].append(price)
    medians = {fuel: statistics.median(values) for fuel, values in samples.items()}

    # Per impianto e carburante vince il prodotto base, poi il self, poi il prezzo più basso.
    best = {}
    for station, description, price, is_self, _ in fresh:
        fuel = fuel_of(description)
        if not fuel:
            continue
        if not medians[fuel] * PLAUSIBLE_RATIOS[0] <= price <= medians[fuel] * PLAUSIBLE_RATIOS[1]:
            continue
        rank = (0 if description.lower() == BASE_DESCRIPTIONS[fuel] else 1, 0 if is_self else 1, price)
        if (station, fuel) not in best or rank < best[(station, fuel)]:
            best[(station, fuel)] = rank

    values = defaultdict(list)
    for (station, fuel), rank in best.items():
        values[(NATIONAL, fuel)].append(rank[2])
        if station in regions:
            values[(regions[station], fuel)].append(rank[2])

    return {key: round(sum(prices) / len(prices) * 1000) for key, prices in values.items() if len(prices) >= MINIMUM_STATIONS}


def read_archive(path, regions):
    results = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith(".csv"):
                continue
            stem = Path(member.name).stem.rsplit("-", 1)[-1]
            day = dt.datetime.strptime(stem, "%Y%m%d").date()
            with archive.extractfile(member) as raw:
                lines = (line.decode("utf-8", "replace") for line in raw)
                rows, rejected = parse_day(lines, read_separator(lines))
                check_rejected(member.name, rows, rejected)
                results[day] = daily_means(rows, day, regions)
    return results


def read_daily_file(path):
    """Il giorno vale quanto dice la riga "Estrazione del": è un giorno prima della pubblicazione."""
    with path.open(encoding="utf-8", errors="replace") as lines:
        first_line = next(lines, "")
        try:
            day = dt.date.fromisoformat(first_line.split()[-1])
        except (IndexError, ValueError):
            sys.exit(f"{path.name}: prima riga inattesa: {first_line[:60]!r}")
        rows, rejected = parse_day(lines, read_separator(lines))
    check_rejected(path.name, rows, rejected)
    if len(rows) < MINIMUM_DAILY_ROWS:
        sys.exit(f"{path.name}: solo {len(rows)} prezzi, file incompleto?")
    return day, rows


def load_archive(path):
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def save_archive(archive, path):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(archive, out, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def set_days(archive, days):
    """Scrive le medie dei giorni dati, allungando le serie se ce ne sono di nuovi."""
    first = dt.date.fromisoformat(archive["firstDay"])
    count = max(archive["dayCount"], (max(days) - first).days + 1)
    for fuels in archive["series"].values():
        for values in fuels.values():
            values.extend([None] * (count - len(values)))
    archive["dayCount"] = count
    for day, means in days.items():
        if not means:
            continue
        index = (day - first).days
        for area, fuels in archive["series"].items():
            for fuel, values in fuels.items():
                values[index] = means.get((area, fuel))


def check_daily_means(archive, day, means):
    """Blocca un file che sposterebbe di colpo le medie nazionali.

    Confronta solo con i giorni vicini: dopo un fermo lungo i prezzi possono essere cambiati davvero.
    """
    index = (day - dt.date.fromisoformat(archive["firstDay"])).days
    for fuel in FUELS:
        value = means.get((NATIONAL, fuel))
        if value is None:
            sys.exit(f"{day}: nessuna media nazionale per {fuel}")
        values = archive["series"][NATIONAL][fuel][max(0, index - COMPARISON_DAYS):index]
        previous = next((earlier for earlier in reversed(values) if earlier is not None), None)
        if previous is not None and abs(value - previous) / previous > MAXIMUM_DAILY_CHANGE:
            sys.exit(f"{day}: {fuel} da {previous} a {value} millesimi, dati sospetti")


def quarter_after(day):
    """Anno, trimestre e ultimo giorno del trimestre successivo a quello di `day`."""
    year, quarter = day.year, (day.month - 1) // 3 + 1
    year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return year, quarter, dt.date(year + (quarter == 4), quarter * 3 % 12 + 1, 1) - dt.timedelta(days=1)


def merge_new_quarters(archive, regions, cache):
    """Sostituisce i giorni raccolti con i trimestri ufficiali usciti nel frattempo."""
    while True:
        year, quarter, last = quarter_after(dt.date.fromisoformat(archive["archivedThrough"]))
        path = cache / f"{year}_{quarter}_tr.tar.gz"
        if not download(ARCHIVE_URL.format(year=year, quarter=quarter), path):
            return
        print(f"Elaboro {path.name}", flush=True)
        days = read_archive(path, regions)
        first = dt.date(year, quarter * 3 - 2, 1)
        expected = (last - first).days + 1
        if sum(1 for means in days.values() if means) < expected * (1 - MAXIMUM_MISSING_DAYS):
            sys.exit(f"{path.name}: troppi giorni mancanti su {expected}")
        set_days(archive, days)
        archive["archivedThrough"] = last.isoformat()


def update(options):
    archive = load_archive(options.output)
    options.cache.mkdir(parents=True, exist_ok=True)

    stations = options.stations or options.cache / "anagrafica_impianti_attivi.csv"
    prices = options.prices or options.cache / "prezzo_alle_8.csv"
    if not options.stations and not download(STATIONS_URL, stations):
        sys.exit("Anagrafica non disponibile")
    if not options.prices and not download(PRICES_URL, prices):
        sys.exit("Prezzi non disponibili")

    regions = regions_by_station(stations)
    if len(regions) < MINIMUM_STATIONS_WITH_REGION:
        sys.exit(f"Solo {len(regions)} impianti con regione, anagrafica incompleta?")

    day, rows = read_daily_file(prices)
    if not options.prices:
        age = (dt.date.today() - day).days
        if not 0 <= age <= MAXIMUM_FILE_AGE_DAYS:
            sys.exit(f"Il file dei prezzi è del {day}: il MIMIT ha smesso di aggiornarlo?")

    means = daily_means(rows, day, regions)
    check_daily_means(archive, day, means)
    if day > dt.date.fromisoformat(archive["archivedThrough"]):
        set_days(archive, {day: means})
        print(f"Giorno {day}: {len(means)} medie", flush=True)

    # Un trimestre illeggibile non deve far perdere il giorno appena calcolato.
    problem = None
    try:
        merge_new_quarters(archive, regions, options.cache)
    except (urllib.error.URLError, OSError, tarfile.TarError, SystemExit) as error:
        problem = error
    save_archive(archive, options.output)
    if problem:
        sys.exit(f"Trimestre non elaborato: {problem}")


def rebuild(options):
    options.cache.mkdir(parents=True, exist_ok=True)
    stations = options.cache / "anagrafica_impianti_attivi.csv"
    if not download(STATIONS_URL, stations):
        sys.exit("Anagrafica non disponibile")
    regions = regions_by_station(stations)
    print(f"{len(regions)} impianti con regione", flush=True)

    days = {}
    for archive in last_complete_quarters(dt.date.today(), options.cache):
        print(f"Elaboro {archive.name}", flush=True)
        days.update(read_archive(archive, regions))

    first, last = min(days), max(days)
    calendar = [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]
    missing = [day.isoformat() for day in calendar if not days.get(day)]
    if len(missing) > len(calendar) * MAXIMUM_MISSING_DAYS:
        sys.exit(f"Mancano {len(missing)} giorni su {len(calendar)}: archivio non attendibile")
    areas = [NATIONAL, *sorted(REGIONS)]
    series = {area: {fuel: [days.get(day, {}).get((area, fuel)) for day in calendar] for fuel in FUELS} for area in areas}

    options.output.parent.mkdir(parents=True, exist_ok=True)
    save_archive({"firstDay": first.isoformat(), "dayCount": len(calendar), "archivedThrough": last.isoformat(), "series": series}, options.output)
    print(f"Archivio: dal {first} al {last}, {len(calendar)} giorni, {options.output.stat().st_size / 1024:.0f} KB")
    print(f"Giorni mancanti ({len(missing)}): {' '.join(missing)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", type=Path, required=True, help="cartella dove tenere i file scaricati")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--update", action="store_true", help="aggiunge il giorno pubblicato e i trimestri nuovi")
    parser.add_argument("--prices", type=Path, help="con --update, usa questo file dei prezzi invece di scaricarlo")
    parser.add_argument("--stations", type=Path, help="con --update, usa questa anagrafica invece di scaricarla")
    options = parser.parse_args()
    update(options) if options.update else rebuild(options)


if __name__ == "__main__":
    main()

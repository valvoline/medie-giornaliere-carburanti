# medie-giornaliere-carburanti

Medie giornaliere dei prezzi dei carburanti in Italia, nazionali e per regione, calcolate dai dati pubblici del MIMIT.

Il file è su <https://valvoline.github.io/medie-giornaliere-carburanti/stats.json> e viene aggiornato ogni mattina.

## Formato

```json
{
  "firstDay": "2024-10-01",
  "dayCount": 770,
  "archivedThrough": "2026-09-30",
  "series": { "Italia": { "Diesel": [1650, 1652, null] } }
}
```

- `series`: per area (`Italia` e le 20 regioni) e carburante (`Diesel`, `Benzina`, `GPL`, `Metano`), un valore al giorno a partire da `firstDay`, in millesimi di euro al litro.
- `null`: giorno senza dati, o area con meno di 3 impianti per quel carburante.
- `archivedThrough`: fin qui i dati vengono dagli archivi trimestrali. I giorni successivi sono calcolati dal file quotidiano e vengono sostituiti quando esce il trimestre.

## Come sono calcolate

Per ogni impianto e carburante si tiene un solo prezzo: il prodotto base, poi il self, poi il più basso. Sono esclusi i prezzi più vecchi di 14 giorni rispetto al più recente e quelli fuori dall'intervallo tra 0,55 e 1,7 volte la mediana nazionale. La media è aritmetica tra gli impianti, senza pesi sui volumi, e comprende la rete autostradale.

Sono valori diversi dalle [medie regionali ufficiali](https://www.mimit.gov.it/it/prezzo-medio-carburanti/regioni), che seguono regole proprie.

## Fonte e licenza

Elaborazione su dati del Ministero delle Imprese e del Made in Italy (MIMIT), Osservaprezzi Carburanti, pubblicati con licenza [IODL 2.0](https://www.dati.gov.it/iodl/2.0/). Questo progetto non è collegato al Ministero.

## Rigenerare i dati

```sh
python3 build_stats_archive.py --cache <cartella per i download>
```

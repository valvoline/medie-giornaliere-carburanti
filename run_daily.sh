#!/bin/bash
# Aggiornamento quotidiano: calcola il giorno pubblicato e invia il file a GitHub, che lo pubblica.
# Gira da una connessione italiana: i server MIMIT non rispondono ai datacenter provati.
set -euo pipefail
cd "$(dirname "$0")"

echo "--- $(date '+%F %T')"
git pull -q --ff-only

cache=$(mktemp -d)
trap 'rm -rf "$cache"' EXIT

# Un trimestre illeggibile fa uscire con errore, ma il giorno calcolato va pubblicato lo stesso.
status=0
python3 -I build_stats_archive.py --update --cache "$cache" || status=$?

git add site/stats.json
if ! git diff --cached --quiet; then
    git commit -q -m "Dati aggiornati al $(date +%F)"
    git push -q
    echo "Pubblicato"
fi
exit "$status"

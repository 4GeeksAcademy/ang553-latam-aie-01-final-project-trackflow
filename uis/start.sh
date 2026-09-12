#!/bin/sh
set -e

# ------------------------------------------------------------------
# start.sh — Arranca website (puerto 3000) y backoffice (puerto 3001)
#            simultáneamente dentro del mismo contenedor.
#
#            Supervisión mutua: si uno de los procesos termina
#            inesperadamente, el otro también se detiene y el
#            contenedor finaliza.
# ------------------------------------------------------------------

cleanup() {
    echo ">>> start.sh: received stop signal, shutting down both processes..."
    kill "$website_pid" "$backoffice_pid" 2>/dev/null || true
    wait "$website_pid" "$backoffice_pid" 2>/dev/null || true
    echo ">>> start.sh: both processes stopped."
    exit 0
}

trap cleanup TERM INT

# ---- website ----
cd /app/uis/website
echo ">>> Starting website on 0.0.0.0:3000 ..."
npm run dev -- --hostname 0.0.0.0 --port 3000 &
website_pid=$!

# ---- backoffice ----
cd /app/uis/backoffice
echo ">>> Starting backoffice on 0.0.0.0:3001 ..."
npm run dev -- --hostname 0.0.0.0 --port 3001 &
backoffice_pid=$!

# ---- Monitor loop: detect exit of either process ----
# wait -n no está disponible en /bin/sh de Alpine, así que usamos
# kill -0 (POSIX) para comprobar si cada proceso sigue vivo.
echo ">>> Both processes are running. Monitoring..."
while true; do
    if ! kill -0 "$website_pid" 2>/dev/null; then
        echo ">>> FATAL: website (PID $website_pid) exited unexpectedly — stopping container." >&2
        kill "$backoffice_pid" 2>/dev/null || true
        wait "$backoffice_pid" 2>/dev/null || true
        exit 1
    fi
    if ! kill -0 "$backoffice_pid" 2>/dev/null; then
        echo ">>> FATAL: backoffice (PID $backoffice_pid) exited unexpectedly — stopping container." >&2
        kill "$website_pid" 2>/dev/null || true
        wait "$website_pid" 2>/dev/null || true
        exit 1
    fi
    sleep 1
done
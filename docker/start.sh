#!/bin/bash
# Start Plaud status poller in background, then run settings web UI in foreground
python -u clients/plaud_watcher.py &
exec python -u app.py

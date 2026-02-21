#!/bin/bash
# Start Plaud status poller in background, then run settings web UI in foreground
python -u plaud_watcher.py &
exec python -u settings_app.py

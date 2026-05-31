#!/usr/bin/env bash
set -euo pipefail
: "${SOURCE_TEXT_DB:?set SOURCE_TEXT_DB}"
python download_db.py
exec python app.py

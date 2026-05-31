# source-text-app

Runtime for the Source Text study app (bible.noahglynn.com). Pure-stdlib reader
server (`app.py`) that reads a SQLite DB downloaded from Cloudflare R2 on boot
(`download_db.py`). No data or secrets here; the DB and credentials are provided
at runtime via env vars. Source pipeline lives in a separate private repo.

#!/usr/bin/env python3
"""Download the canonical SQLite from Cloudflare R2 to SOURCE_TEXT_DB (deploy boot).

Compares the R2 object's size to the local copy and re-pulls when they differ, so a
corpus update (new translations, versification, etc.) is picked up automatically on
the next deploy. The stale local copy is removed BEFORE downloading so the fetch fits
a small persistent disk (old + new never coexist). When the local copy already matches
R2, the download is skipped. R2 is S3-compatible; creds come from env.

Env:
  SOURCE_TEXT_DB        local destination path (also what app.py reads)
  R2_ENDPOINT           https://<account_id>.r2.cloudflarestorage.com
  R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
  R2_BUCKET / R2_KEY    object location
"""
import os
import sys

dest = os.environ.get("SOURCE_TEXT_DB")
if not dest:
    sys.exit("SOURCE_TEXT_DB not set")

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    config=Config(signature_version="s3v4", region_name="auto"),
)
bucket, key = os.environ["R2_BUCKET"], os.environ["R2_KEY"]
local = os.path.getsize(dest) if os.path.exists(dest) else None

# Size of the object in R2 (HEAD). If that fails, keep any usable local copy rather
# than breaking a working deploy.
try:
    remote = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
except Exception as e:  # noqa: BLE001
    if local and local > 100_000_000:
        print(f"R2 HEAD failed ({e}); keeping existing DB at {dest} ({local // 1048576} MB)", flush=True)
        sys.exit(0)
    sys.exit(f"R2 HEAD failed and no usable local DB present: {e}")

if local == remote:
    print(f"DB up to date at {dest} ({local // 1048576} MB) - skipping download", flush=True)
    sys.exit(0)

# Stale or missing: free the old copy first so the new one fits a small disk.
if local is not None:
    print(f"DB changed (local {local // 1048576} MB != R2 {remote // 1048576} MB) - re-downloading", flush=True)
    os.remove(dest)

os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
tmp = dest + ".part"
if os.path.exists(tmp):
    os.remove(tmp)
print(f"downloading s3://{bucket}/{key} -> {dest} ({remote // 1048576} MB)", flush=True)
s3.download_file(bucket, key, tmp)
os.replace(tmp, dest)
print(f"done: {os.path.getsize(dest) // 1048576} MB", flush=True)

#!/usr/bin/env python3
"""Download the canonical SQLite from Cloudflare R2 to SOURCE_TEXT_DB (deploy boot).

Skips the download if a plausibly-complete copy is already present (so a Render
persistent disk only fetches once). R2 is S3-compatible; creds come from env.

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

# Already downloaded? (guard against partials with a size floor)
if os.path.exists(dest) and os.path.getsize(dest) > 100_000_000:
    print(f"DB already present at {dest} ({os.path.getsize(dest)//1048576} MB) - skipping download")
    sys.exit(0)

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    config=Config(signature_version="s3v4", region_name="auto"),
)
bucket, key = os.environ["R2_BUCKET"], os.environ["R2_KEY"]
tmp = dest + ".part"
print(f"downloading s3://{bucket}/{key} -> {dest}", flush=True)
s3.download_file(bucket, key, tmp)
os.replace(tmp, dest)
print(f"done: {os.path.getsize(dest)//1048576} MB", flush=True)

#!/usr/bin/env python3
"""Requeue library files so the current flow re-evaluates them.

Usage: requeue_outdated.py [--apply] [--limit N] [--match TEXT] [--url ...]

Tdarr runs a file through its flow once and keeps the verdict: changing the
flow does not re-evaluate anything. That is how 102 files above the bitrate
guard and 10 AV1 files stayed untouched — they were processed before the guard
existed. Run this after every flow revision deployed by deploy_tdarr.py.

Every file of the production libraries is a candidate: the flow itself decides
what to do, and a file already as required costs a health check and leaves
with "Not required". Files the flow will re-encode go first, so a bounded run
(--limit) spends its slots where it matters. Files queued, held or never
scanned are left alone.

Selection reads the server database read-only; the requeue goes through the
API (bulk-update-files), never through a database write.
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "tdarr/server/Tdarr/DB2/SQL/database.db")
PRODUCTION_LIBRARIES = ("Movies", "TV Shows")
SKIP_STATES = ("Queued", "Hold", None)
# Same thresholds as the flow's bitrate guard (build_flow_v6.py), in Mbps.
BITRATE_LIMITS = {"1080p": 20, "1440p": 35, "4KUHD": 35, "DCI4K": 35, "8KUHD": 35}
COMPATIBLE_CODECS = ("hevc", "h264")


def needs_reencode(record):
    if record.get("video_codec_name") not in COMPATIBLE_CODECS:
        return True
    limit = BITRATE_LIMITS.get(record.get("video_resolution"))
    return limit is not None and float(record.get("bit_rate") or 0) > limit * 1e6


def candidates(db_path, match):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        libraries = {}
        for (raw,) in con.execute("SELECT json_data FROM librarysettingsjsondb"):
            lib = json.loads(raw)
            libraries[lib["_id"]] = lib.get("name")
        rows = []
        for (raw,) in con.execute("SELECT json_data FROM filejsondb"):
            record = json.loads(raw)
            if libraries.get(record.get("DB")) not in PRODUCTION_LIBRARIES:
                continue
            if record.get("TranscodeDecisionMaker") in SKIP_STATES:
                continue
            if match and match not in record["_id"]:
                continue
            rows.append(record)
    finally:
        con.close()
    rows.sort(key=lambda r: (not needs_reencode(r), r["_id"]))
    return rows


def requeue(url, file_ids):
    body = {"data": {"fileIds": file_ids, "updatedObj": {"TranscodeDecisionMaker": "Queued"}}}
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/v2/bulk-update-files", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="requeue at most N files (0 = all)")
    parser.add_argument("--match", default="", help="only paths containing this text")
    parser.add_argument("--url", default="http://localhost:8266")
    parser.add_argument("--db", default=DB)
    args = parser.parse_args()

    rows = candidates(args.db, args.match)
    reencode = sum(1 for r in rows if needs_reencode(r))
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} file(s) selected ({reencode} candidate(s) for re-encoding overall)")
    for r in rows[:20]:
        print(f"  {'re-encode' if needs_reencode(r) else 'check    '}  {r['_id']}")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")
    if not args.apply:
        print("dry run: add --apply to requeue")
        return 0
    ids = [r["_id"] for r in rows]
    for start in range(0, len(ids), 500):
        requeue(args.url, ids[start:start + 500])
    print(f"requeued {len(ids)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

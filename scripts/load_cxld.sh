#!/bin/bash
# Add the most widely-read Policy and LD evidence alongside the full PF corpus.
# Policy+LD are ~4.3M rows (~100x PF) and cannot be stored or hosted whole, so
# they are admitted by the dataset's own duplicateCount — how many teams
# actually disclosed the card. High-duplicate rows collapse under dedup, so
# this yields a modest, high-value set: the meta, not the long tail.
cd "$(dirname "$0")/.."
echo "=== cx/ld load starting $(date) ==="
.venv/bin/python - <<'PY' 2>&1
import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
from carddb.config import load_config
from carddb.db import open_db
from carddb.ingest import IngestStats
from carddb.hf_remote import ingest_remote_pf, load_census
cfg = load_config()
conn = open_db('data/carddb.sqlite')
# every shard, not just the PF ones
shards = list(range(109))
s = IngestStats()
ingest_remote_pf(conn, cfg, s, shards=shards,
                 events=["cx", "ld"], min_dup=400,
                 source_tag="hf-shard-cxld")
print("CXLD DONE", s.summary())
PY
echo "=== cx/ld load finished $(date) ==="
.venv/bin/python -m carddb stats

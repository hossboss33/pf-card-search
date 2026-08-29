"""Re-ingest PF rows that were dropped for having no tag and no fulltext.

Five of them carry a real card in the `markup` field alone; hf_loader now
recovers those. Fetches each PF shard once, keeps only the affected row ids,
and runs them through the normal ingest path.
"""
import json, logging, tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")

from carddb.config import load_config
from carddb.db import open_db
from carddb.hf_loader import ingest_hf_rows
from carddb.hf_remote import _connect, _fetch_shard, BASE
from carddb.ingest import IngestStats

ids = [int(l) for l in open("/tmp/dropped_ids.txt") if l.strip()]
census = json.load(open("reports/shard_census.json"))
cfg = load_config()
conn = open_db("data/carddb.sqlite")
con = _connect()
stats = IngestStats()
placeholders = ",".join(str(i) for i in ids)

for n, shard in enumerate(census["pf_shards"], 1):
    tmp = Path(tempfile.gettempdir()) / f"recover-{shard:04d}.parquet"
    try:
        _fetch_shard(shard, tmp)
        cur = con.execute(
            f"SELECT * FROM read_parquet('{tmp}') WHERE event='pf' "
            f"AND id IN ({placeholders})")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        if tmp.exists():
            tmp.unlink()
    if rows:
        ingest_hf_rows(conn, rows, cfg, stats)
    print(f"[{n}/{len(census['pf_shards'])}] shard {shard}: {len(rows)} target rows "
          f"-> +{stats.new_cards} cards so far", flush=True)

print("RECOVERY DONE", stats.summary(), flush=True)

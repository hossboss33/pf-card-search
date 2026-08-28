"""Census which remote parquet shards hold PF rows.

Reads ONLY the `event` and `caselistName` columns of each shard over HTTP
range requests — no shard is downloaded in full. Deliberately gentle on
Hugging Face: low concurrency, retries with backoff, a descriptive
User-Agent, and a resumable per-shard result file.
"""
import json, os, sys, time
from pathlib import Path

import duckdb

RESOLVE = ("https://huggingface.co/datasets/Yusuf5/OpenCaselist/resolve/"
           "refs%2Fconvert%2Fparquet/default/train")
N = 109
OUT = Path(__file__).resolve().parent.parent / "reports" / "shard_census.json"
PARTIAL = OUT.with_suffix(".partial.json")

os.environ.setdefault(
    "HF_HUB_USER_AGENT",
    "pf-card-search (personal research index; contact: caravellojake504@gmail.com)")

con = duckdb.connect()
con.execute("LOAD httpfs;")
con.execute("SET threads TO 2;")          # be gentle
con.execute("SET http_retries TO 6;")
con.execute("SET http_retry_backoff TO 4;")
con.execute("SET http_keep_alive TO true;")

done = {}
if PARTIAL.exists():
    done = json.loads(PARTIAL.read_text())
    print(f"resuming: {len(done)} shards already censused", flush=True)

t0 = time.time()
for i in range(N):
    if str(i) in done:
        continue
    url = f"{RESOLVE}/{i:04d}.parquet"
    for attempt in range(6):
        try:
            rows = con.execute(
                "SELECT event, caselistName, count(*) FROM read_parquet(?) "
                "GROUP BY 1,2", [url]).fetchall()
            done[str(i)] = [{"event": e, "caselist": c, "rows": n}
                            for e, c, n in rows]
            break
        except Exception as exc:
            wait = min(60, 5 * (2 ** attempt))
            print(f"shard {i} attempt {attempt+1} failed ({type(exc).__name__}); "
                  f"sleeping {wait}s", flush=True)
            time.sleep(wait)
    else:
        print(f"shard {i}: GIVING UP after retries", flush=True)
        done[str(i)] = [{"event": "__error__", "caselist": None, "rows": 0}]
    PARTIAL.write_text(json.dumps(done))
    pf = sum(e["rows"] for e in done[str(i)] if e["event"] == "pf")
    evs = sorted({e["event"] for e in done[str(i)] if e["event"]})
    print(f"[{len(done)}/{N}] shard {i}: events={evs} pf_rows={pf} "
          f"({time.time()-t0:.0f}s elapsed)", flush=True)
    time.sleep(0.3)

pf_shards = sorted(int(s) for s, v in done.items()
                   if any(e["event"] == "pf" for e in v))
pf_rows = sum(e["rows"] for v in done.values() for e in v if e["event"] == "pf")
OUT.write_text(json.dumps(
    {"elapsed_s": round(time.time() - t0, 1), "pf_shards": pf_shards,
     "pf_rows": pf_rows, "by_shard": done}, indent=1))
print(f"CENSUS DONE: {len(pf_shards)} PF shards, {pf_rows} PF rows", flush=True)
print("pf shards:", pf_shards, flush=True)

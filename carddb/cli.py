"""CLI entry point. Spec §10: ingest, dedup, topics, serve, sync, export, stats.

Usage:
  python -m carddb ingest --source {hf|api|private} [--caselist SLUG] [--since DATE] [--limit N] [paths...]
  python -m carddb dedup
  python -m carddb topics assign
  python -m carddb serve [--host 127.0.0.1] [--port 8321]
  python -m carddb sync [--caselist SLUG]
  python -m carddb export --cards 1,2,3 --out out.docx [--preset house] [--highlight green]
  python -m carddb citehealth [--limit 200]
  python -m carddb stats
  python -m carddb backup
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from .config import ROOT, load_config, resolve_path
from .db import open_db


def _conn(cfg):
    return open_db(resolve_path(cfg, "db"))


def cmd_ingest(args, cfg):
    from .ingest import IngestStats
    conn = _conn(cfg)
    stats = IngestStats()
    if args.source == "hf":
        from .hf_loader import ingest_hf
        ingest_hf(conn, cfg, stats, limit=args.limit)
    elif args.source == "api":
        from .api_sync import sync
        stats = sync(conn, cfg, caselist=args.caselist, since=args.since)
    elif args.source == "private":
        from .db import ledger_seen
        from .docx_parser import ParseFailure, convert_doc_to_docx, parse_docx
        from .ingest import attach_variant, finish_batch, insert_card, ledger_stamp
        from .rawstore import now_iso, record_document, store_bytes
        raw_root = resolve_path(cfg, "raw_store")
        for p in args.paths:
            # Parser failures never abort a batch (spec §3.4): every per-file
            # problem — unreadable path included — is recorded and skipped.
            path = Path(p)
            stats.units_seen += 1
            try:
                data = path.read_bytes()
            except OSError as e:
                print(f"[ingest] cannot read {path}: {e}", file=sys.stderr)
                stats.failed += 1
                continue
            sha, local = store_bytes(raw_root, data)
            if ledger_seen(conn, "private", sha, sha):
                stats.units_skipped += 1
                continue
            doc_id = record_document(conn, sha, "private", None, path.name, str(local))
            try:
                if path.suffix.lower() == ".doc":
                    path = convert_doc_to_docx(path)  # legacy .doc via soffice (§3.4)
                parsed = parse_docx(path)
            except ParseFailure as e:
                conn.execute(
                    "UPDATE documents SET parse_status='failed', parse_error=?, parsed_at=? WHERE id=?",
                    (str(e), now_iso(), doc_id))
                stats.failed += 1
                conn.commit()
                continue
            except Exception as e:  # never lose the batch to one bad file
                conn.execute(
                    "UPDATE documents SET parse_status='failed', parse_error=?, parsed_at=? WHERE id=?",
                    (f"{type(e).__name__}: {e}", now_iso(), doc_id))
                stats.failed += 1
                conn.commit()
                continue
            for rec in parsed.cards:
                rec.fidelity = "private"
                card_id, created = insert_card(conn, rec)
                _, vcreated = attach_variant(conn, card_id, rec, doc_id, None)
                stats.new_cards += int(created)
                stats.new_variants += int(vcreated)
                stats.touched_card_ids.add(card_id)
            conn.execute(
                "UPDATE documents SET parse_status='ok', parsed_at=? WHERE id=?",
                (now_iso(), doc_id))
            ledger_stamp(conn, "private", sha, sha)
            stats.parsed += 1
            conn.commit()  # progress survives a later crash
        finish_batch(conn, stats)
    else:
        print(f"unknown source: {args.source}", file=sys.stderr)
        return 2
    print(stats.summary())
    return 0


def cmd_dedup(args, cfg):
    from .dedup import run_dedup
    conn = _conn(cfg)
    report_dir = resolve_path(cfg, "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    st = run_dedup(conn, report_dir)
    print(f"candidates={st.candidates} merged={st.merged} trims={st.trims} "
          f"disagreements={st.disagreements}")
    return 0


def cmd_topics(args, cfg):
    from .topics import assign_topics, load_topics
    conn = _conn(cfg)
    n = load_topics(conn, resolve_path(cfg, "topics"))
    print(f"topics loaded: {n}")
    if args.topics_cmd == "assign":
        st = assign_topics(conn)
        print(st)
    return 0


def cmd_serve(args, cfg):
    import uvicorn
    from .server import create_app
    app = create_app(db_path=resolve_path(cfg, "db"), cfg=cfg)
    # Private by default (spec §0.4): binds 127.0.0.1 unless overridden.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_sync(args, cfg):
    from .api_sync import sync
    conn = _conn(cfg)
    st = sync(conn, cfg, caselist=args.caselist)
    print(st.summary())
    return 0


def cmd_export(args, cfg):
    from .export_docx import export_cards
    conn = _conn(cfg)
    ids = [int(x) for x in args.cards.split(",") if x.strip()]
    out = export_cards(conn, ids, Path(args.out), preset=args.preset,
                       highlight=args.highlight)
    print(f"wrote {out}")
    return 0


def cmd_citehealth(args, cfg):
    from .citehealth import run_citehealth
    conn = _conn(cfg)
    n = run_citehealth(conn, limit=args.limit)
    print(f"checked {n} urls")
    return 0


def cmd_stats(args, cfg):
    conn = _conn(cfg)
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print(f"canonical cards : {q('SELECT COUNT(*) FROM cards WHERE is_analytic = 0')}")
    print(f"analytics       : {q('SELECT COUNT(*) FROM cards WHERE is_analytic = 1')}")
    print(f"variants        : {q('SELECT COUNT(*) FROM card_variants')}")
    print(f"documents       : {q('SELECT COUNT(*) FROM documents')}")
    print(f"rounds          : {q('SELECT COUNT(*) FROM rounds')}")
    print(f"teams           : {q('SELECT COUNT(*) FROM teams')}")
    print(f"schools         : {q('SELECT COUNT(*) FROM schools')}")
    print(f"topics          : {q('SELECT COUNT(*) FROM topics')}")
    print(f"merges          : {q('SELECT COUNT(*) FROM card_merges')}")
    unassigned = conn.execute(
        "SELECT COUNT(*) FROM rounds WHERE topic_id IS NULL").fetchone()[0]
    print(f"rounds w/o topic: {unassigned}")
    return 0


def cmd_reindex(args, cfg):
    """Repair path: rebuild every FTS row and derived aggregate from the
    tables of record (e.g. after an interrupted bulk load)."""
    from .db import fts_rebuild, recompute_aggregates
    conn = _conn(cfg)
    n = fts_rebuild(conn)
    recompute_aggregates(conn)
    print(f"reindexed {n} cards")
    return 0


def cmd_backup(args, cfg):
    src = resolve_path(cfg, "db")
    if not src.exists():
        print("no database yet", file=sys.stderr)
        return 1
    bdir = resolve_path(cfg, "backups")
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"{date.today().isoformat()}.sqlite"
    # The DB runs in WAL mode; a filesystem copy can miss the WAL or catch a
    # moving checkpoint. sqlite's online backup API is the safe path.
    import sqlite3
    with sqlite3.connect(str(src)) as sconn, sqlite3.connect(str(dest)) as dconn:
        sconn.backup(dconn)
    backups = sorted(bdir.glob("*.sqlite"))
    for old in backups[:-8]:  # keep 8 (spec §10)
        old.unlink()
    print(f"backup: {dest}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="carddb")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest")
    p.add_argument("--source", required=True, choices=["hf", "api", "private"])
    p.add_argument("--caselist")
    p.add_argument("--since")
    p.add_argument("--limit", type=int)
    p.add_argument("paths", nargs="*")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("dedup")
    p.set_defaults(fn=cmd_dedup)

    p = sub.add_parser("topics")
    p.add_argument("topics_cmd", nargs="?", default="load", choices=["load", "assign"])
    p.set_defaults(fn=cmd_topics)

    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8321)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("sync")
    p.add_argument("--caselist")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("export")
    p.add_argument("--cards", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--preset", default="house", choices=["house", "verbatim"])
    p.add_argument("--highlight", default="green",
                   choices=["green", "yellow", "blue", "turquoise"])
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("citehealth")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(fn=cmd_citehealth)

    p = sub.add_parser("stats")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("reindex")
    p.set_defaults(fn=cmd_reindex)

    p = sub.add_parser("backup")
    p.set_defaults(fn=cmd_backup)

    args = ap.parse_args(argv)
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config()
    started = datetime.now()
    rc = args.fn(args, cfg)
    elapsed = (datetime.now() - started).total_seconds()
    print(f"[{args.cmd}] done in {elapsed:.1f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())

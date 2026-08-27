"""Dedup layer 3 tests (spec §4.3–4.4, Appendix A).

A synthetic corpus is pushed through the real ingest path
(insert_card / attach_variant / finish_batch), then run_dedup merges —
or provably refuses to merge — under the exact Appendix A rules.
Bodies are kept realistically long (>= 60 words, most ~190) so 5-token
shingling behaves like it does on production cards.
"""
from pathlib import Path

import pytest

from carddb.db import open_db
from carddb.dedup import (DedupStats, REPORT_NAME, author_tokens, cite_year,
                          run_dedup)
from carddb.ingest import (CardRecord, IngestStats, attach_variant,
                           finish_batch, get_or_create_caselist,
                           get_or_create_round, get_or_create_school,
                           get_or_create_team, insert_card)
from carddb.keys import canonical_key
from carddb.normalize import normalize
from carddb.rawstore import record_document

# --- Fixture prose ---------------------------------------------------------
# Two unrelated ~190-word paragraphs. Same-paragraph derivatives are the
# near-dup pairs; cross-paragraph text never collides.

PARA_A = (
    "The rapid expansion of hyperscale data centers has fundamentally altered "
    "the trajectory of electricity demand in the United States, reversing two "
    "decades of essentially flat load growth and forcing utilities to confront "
    "investment decisions they are structurally unprepared to make. "
    "Interconnection queues across every major regional transmission "
    "organization have swollen to historic lengths, with proposed generation "
    "and large-load requests now waiting four to seven years for the studies "
    "that precede construction. Developers respond by shopping identical "
    "projects to multiple utilities simultaneously, inflating queue volumes "
    "further and obscuring the real level of demand. Utility planners, burned "
    "by a decade of overforecasting, now err in the opposite direction, "
    "committing capital to speculative load that may never materialize while "
    "genuinely committed projects languish behind phantom requests. The result "
    "is a compounding planning failure: transmission built for loads that "
    "vanish, generation deferred for loads that arrive, and ratepayers left "
    "carrying the cost of both errors. Regulators in several states have "
    "opened dockets to reform large-load tariffs, but the procedural timelines "
    "of utility commissions run in years while data center announcements "
    "arrive weekly, guaranteeing that the regulatory framework will remain "
    "perpetually behind the demand it is supposed to govern."
)

PARA_B = (
    "Reshoring semiconductor fabrication has become the organizing principle "
    "of American industrial policy, yet the workforce pipeline required to "
    "operate advanced fabs remains the binding constraint that subsidies alone "
    "cannot relax. A leading-edge fabrication facility employs thousands of "
    "technicians whose training spans vacuum systems, plasma chemistry, and "
    "statistical process control, disciplines that community colleges have "
    "only begun to teach at scale. Taiwan spent three decades building this "
    "human infrastructure through deliberate coordination between industry and "
    "universities, and the notion that tax credits can compress that timeline "
    "into a single election cycle misunderstands how tacit manufacturing "
    "knowledge accumulates. Early evidence from the Arizona and Ohio "
    "construction sites confirms the problem: schedule slips attributed "
    "publicly to permitting were driven substantially by shortages of "
    "cleanroom-qualified labor, forcing companies to fly in experienced staff "
    "from abroad at extraordinary cost. Advocates respond that immigration "
    "reform could fill the gap quickly, but visa categories for skilled "
    "technicians remain capped at levels set decades ago, and no pending "
    "legislation would change them meaningfully. The subsidy architecture "
    "therefore rewards announcements rather than output, a distinction that "
    "will become painfully visible when production targets slip from press "
    "releases into quarterly earnings calls."
)

WORDS_A = PARA_A.split()
WORDS_B = PARA_B.split()


def _drift(words, idx=60, repl="categorically"):
    """One-word drift: the classic re-cut / OCR-noise near-duplicate."""
    w = list(words)
    assert w[idx] != repl
    w[idx] = repl
    return " ".join(w)


def _shingle_set(text, k=5):
    toks = normalize(text).split()
    return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}


def _jaccard(a, b):
    sa, sb = _shingle_set(a), _shingle_set(b)
    return len(sa & sb) / len(sa | sb)


def _containment(a, b):
    sa, sb = _shingle_set(a), _shingle_set(b)
    return len(sa & sb) / min(len(sa), len(sb))


def _trim_of(words, frac=0.885, start=6):
    """A contiguous middle slice sized so shingle-Jaccard with the full
    text lands below 0.90 (so only the containment rule can merge it)
    while staying above the LSH candidate knee (~0.88)."""
    n_shingles = len(words) - 4
    keep = int(frac * n_shingles) + 4
    return " ".join(words[start:start + keep])


# --- Corpus through the real ingest path -----------------------------------

@pytest.fixture()
def db(tmp_path):
    conn = open_db(tmp_path / "t.sqlite")
    yield conn
    conn.close()


def _add_card(conn, school, team, round_ext, doc_sha, body, cite,
              tag="Fixture tag", is_analytic=False):
    """One disclosure via the real path: entities, document, insert_card,
    attach_variant, finish_batch (FTS + aggregates)."""
    cl = get_or_create_caselist(conn, "hspf25", season=2025, event="pf")
    sc = get_or_create_school(conn, cl, school)
    tm = get_or_create_team(conn, sc, team)
    rd = get_or_create_round(conn, tm, round_ext, side="P", tournament="Test RR")
    doc_id = record_document(conn, doc_sha, "test", None, None, None)
    rec = CardRecord(
        tag=tag, cite=cite,
        fullcite=(cite + ", full citation, Journal, https://example.test/a") if cite else None,
        body_text=body, is_analytic=is_analytic,
        markup_html="<p>" + (body or "") + "</p>",
        summary=body, spoken=body, highlight_ratio=0.4, ordinal=0,
    )
    stats = IngestStats()
    card_id, _created = insert_card(conn, rec)
    attach_variant(conn, card_id, rec, doc_id, rd)
    stats.touched_card_ids.add(card_id)
    finish_batch(conn, stats)
    return card_id


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- cite_year / author_tokens (short cites, robustly) ---------------------

@pytest.mark.parametrize("cite,year,authors", [
    ("Diamond '13", "13", {"diamond"}),
    ("Rodgers and Cooper 06", "06", {"rodgers", "cooper"}),
    ("Smith et al. 24", "24", {"smith"}),
    ("Kessler '26", "26", {"kessler"}),
    ("Kessler 2026", "26", {"kessler"}),
    ("O’Brien ’19", "19", {"obrien"}),
    ("Nagle & Chen '22", "22", {"nagle", "chen"}),
    ("No year here", None, {"no", "year", "here"}),
    (None, None, set()),
    ("", None, set()),
])
def test_cite_parsing(cite, year, authors):
    assert cite_year(cite) == year
    assert author_tokens(cite) == authors


# --- near-identical pair merges --------------------------------------------

def test_one_word_drift_merges(db, tmp_path):
    body1 = " ".join(WORDS_A)
    body2 = _drift(WORDS_A)
    assert _jaccard(body1, body2) >= 0.90  # fixture guard
    c1 = _add_card(db, "Alpha", "AA", "r-a1", "sha-a1", body1, "Kessler '26")
    c2 = _add_card(db, "Beta", "BB", "r-b1", "sha-b1", body2, "Kessler '26")
    assert c1 != c2  # layer 2 saw them as distinct

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.candidates >= 1
    assert stats.merged == 1 and stats.trims == 0
    assert _count(db, "cards") == 1

    survivor = db.execute("SELECT id FROM cards").fetchone()["id"]
    # variants repointed to the survivor
    variants = db.execute(
        "SELECT card_id FROM card_variants").fetchall()
    assert len(variants) == 2 and all(v["card_id"] == survivor for v in variants)
    # merge recorded, reversible via the absorbed canonical_key
    merges = db.execute(
        "SELECT survivor_id, absorbed_key, relation, merged_at FROM card_merges"
    ).fetchall()
    assert len(merges) == 1
    m = merges[0]
    absorbed_body = body2 if survivor == c1 else body1
    assert m["survivor_id"] == survivor
    assert m["absorbed_key"] == canonical_key(absorbed_body, "Fixture tag", False)
    assert m["relation"] == "dup"
    assert m["merged_at"]
    # aggregates recomputed for the survivor
    row = db.execute(
        "SELECT variant_count, team_count, school_count FROM cards").fetchone()
    assert tuple(row) == (2, 2, 2)
    # absorbed FTS row gone, survivor's present
    ids = [r["rowid"] for r in db.execute("SELECT rowid FROM card_fts")]
    assert ids == [survivor]


# --- trim case: longer survives, relation='trim' ---------------------------

def test_trim_merges_longer_survives(db, tmp_path):
    long_body = " ".join(WORDS_A)
    short_body = _trim_of(WORDS_A)
    # fixture guards: only the containment rule can merge this pair,
    # and the shorter is fully contained in the longer
    j = _jaccard(long_body, short_body)
    assert 0.86 <= j < 0.90
    assert _containment(long_body, short_body) >= 0.95
    assert len(normalize(short_body).split()) >= 60

    c_long = _add_card(db, "Alpha", "AA", "r-a1", "sha-l", long_body, "Diamond '13")
    c_short = _add_card(db, "Beta", "BB", "r-b1", "sha-s", short_body, "Diamond '13")

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 1 and stats.trims == 1
    assert _count(db, "cards") == 1
    survivor = db.execute("SELECT id, body_text FROM cards").fetchone()
    assert survivor["id"] == c_long          # the LONGER text survives
    assert survivor["body_text"] == long_body
    m = db.execute("SELECT * FROM card_merges").fetchone()
    assert m["relation"] == "trim"
    assert m["survivor_id"] == c_long
    assert m["absorbed_key"] == canonical_key(short_body, "Fixture tag", False)
    # the short card's variant now rides on the survivor
    variants = db.execute(
        "SELECT card_id FROM card_variants").fetchall()
    assert len(variants) == 2 and all(v["card_id"] == c_long for v in variants)
    assert db.execute("SELECT rowid FROM card_fts WHERE rowid = ?",
                      (c_short,)).fetchone() is None


# --- never merge across cite years -----------------------------------------

def test_different_cite_years_never_merge(db, tmp_path):
    body1 = " ".join(WORDS_B)
    body2 = _drift(WORDS_B)
    assert _jaccard(body1, body2) >= 0.90  # text alone WOULD merge
    _add_card(db, "Alpha", "AA", "r-a1", "sha-1", body1, "Diamond '13")
    _add_card(db, "Beta", "BB", "r-b1", "sha-2", body2, "Diamond '14")

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 0
    assert _count(db, "cards") == 2
    assert _count(db, "card_merges") == 0


# --- never merge with disjoint authors -------------------------------------

def test_author_disjoint_never_merges(db, tmp_path):
    body1 = " ".join(WORDS_B)
    body2 = _drift(WORDS_B)
    _add_card(db, "Alpha", "AA", "r-a1", "sha-1", body1, "Smith et al. 24")
    _add_card(db, "Beta", "BB", "r-b1", "sha-2", body2, "Jones '24")

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 0
    assert _count(db, "cards") == 2


# --- distinct cards below 0.90 Jaccard never merge -------------------------

def test_low_jaccard_never_merges(db, tmp_path):
    # Shared 90-word opening, then each card diverges into different text:
    # neither the Jaccard rule nor the containment rule may fire, even
    # though the cites are identical.
    body1 = " ".join(WORDS_A[:90] + WORDS_B[:95])
    body2 = " ".join(WORDS_A[:90] + WORDS_B[100:])
    assert _jaccard(body1, body2) < 0.90
    assert _containment(body1, body2) < 0.95
    _add_card(db, "Alpha", "AA", "r-a1", "sha-1", body1, "Kessler '26")
    _add_card(db, "Beta", "BB", "r-b1", "sha-2", body2, "Kessler '26")

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 0
    assert _count(db, "cards") == 2


# --- analytics and empty/short bodies are excluded -------------------------

def test_analytics_and_short_bodies_excluded(db, tmp_path):
    analytic = _add_card(db, "Alpha", "AA", "r-a1", "sha-an", None,
                         "Kessler '26", tag="No solvency, queues persist",
                         is_analytic=True)
    tiny = _add_card(db, "Alpha", "AA", "r-a2", "sha-tiny",
                     "Queues persist regardless.", "Kessler '26")
    # plus a real merging pair, so the pass demonstrably ran
    _add_card(db, "Beta", "BB", "r-b1", "sha-1", " ".join(WORDS_A), "Kessler '26")
    _add_card(db, "Gamma", "CC", "r-c1", "sha-2", _drift(WORDS_A), "Kessler '26")

    stats = run_dedup(db, tmp_path / "reports")
    assert stats.merged == 1
    remaining = {r["id"] for r in db.execute("SELECT id FROM cards")}
    assert analytic in remaining and tiny in remaining
    assert len(remaining) == 3
    merged_into = {r["survivor_id"] for r in db.execute(
        "SELECT survivor_id FROM card_merges")}
    assert analytic not in merged_into and tiny not in merged_into


# --- idempotence: a second run merges 0 ------------------------------------

def test_second_run_merges_nothing(db, tmp_path):
    _add_card(db, "Alpha", "AA", "r-a1", "sha-1", " ".join(WORDS_A), "Kessler '26")
    _add_card(db, "Beta", "BB", "r-b1", "sha-2", _drift(WORDS_A), "Kessler '26")
    _add_card(db, "Gamma", "CC", "r-c1", "sha-3", " ".join(WORDS_B), "Diamond '13")
    _add_card(db, "Delta", "DD", "r-d1", "sha-4", _trim_of(WORDS_B), "Diamond '13")

    first = run_dedup(db, tmp_path / "reports")
    assert first.merged == 2 and first.trims == 1
    snapshot = (_count(db, "cards"), _count(db, "card_variants"),
                _count(db, "card_merges"), _count(db, "card_fts"))

    second = run_dedup(db, tmp_path / "reports")
    assert second.merged == 0 and second.trims == 0
    assert (_count(db, "cards"), _count(db, "card_variants"),
            _count(db, "card_merges"), _count(db, "card_fts")) == snapshot


# --- disagreement report vs hf_buckets -------------------------------------

def test_disagreement_report(db, tmp_path):
    # pair 1 merges (same cite), but the HF signal put them in different
    # buckets -> disagreement kind (b)
    p1a = _add_card(db, "Alpha", "AA", "r-a1", "sha-1", " ".join(WORDS_A), "Kessler '26")
    p1b = _add_card(db, "Beta", "BB", "r-b1", "sha-2", _drift(WORDS_A), "Kessler '26")
    # pair 2 shares a bucket but we refuse the merge (different cite
    # years) -> disagreement kind (a)
    p2a = _add_card(db, "Gamma", "CC", "r-c1", "sha-3", " ".join(WORDS_B), "Diamond '13")
    p2b = _add_card(db, "Delta", "DD", "r-d1", "sha-4", _drift(WORDS_B), "Diamond '14")

    db.execute("CREATE TABLE IF NOT EXISTS hf_buckets (card_id INTEGER, bucket_id TEXT)")
    db.executemany("INSERT INTO hf_buckets (card_id, bucket_id) VALUES (?,?)",
                   [(p1a, "b1"), (p1b, "b2"), (p2a, "b3"), (p2b, "b3")])
    db.commit()

    report_dir = tmp_path / "reports"
    stats = run_dedup(db, report_dir)
    assert stats.merged == 1

    report = report_dir / "dedup_disagreements.tsv"
    assert report.name == REPORT_NAME and report.exists()
    lines = report.read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t") == ["kind", "card_id_a", "card_id_b",
                                    "buckets_a", "buckets_b"]
    rows = [ln.split("\t") for ln in lines[1:]]
    kinds = sorted(r[0] for r in rows)
    assert kinds == ["bucket_shared_not_merged", "merged_different_buckets"]
    assert stats.disagreements == len(rows) == 2
    (a_row,) = [r for r in rows if r[0] == "bucket_shared_not_merged"]
    assert {int(a_row[1]), int(a_row[2])} == {p2a, p2b}
    (b_row,) = [r for r in rows if r[0] == "merged_different_buckets"]
    assert {int(b_row[1]), int(b_row[2])} == {p1a, p1b}

    # absorbed card's bucket rows were moved to the survivor
    survivor = db.execute(
        "SELECT id FROM cards WHERE canonical_key = ?",
        (canonical_key(" ".join(WORDS_A), "Fixture tag", False),)
    ).fetchone() or db.execute(
        "SELECT id FROM cards WHERE canonical_key = ?",
        (canonical_key(_drift(WORDS_A), "Fixture tag", False),)
    ).fetchone()
    hf_owners = {r["card_id"] for r in db.execute(
        "SELECT card_id FROM hf_buckets WHERE bucket_id IN ('b1','b2')")}
    assert hf_owners == {survivor["id"]}


def test_no_report_without_hf_buckets(db, tmp_path):
    _add_card(db, "Alpha", "AA", "r-a1", "sha-1", " ".join(WORDS_A), "Kessler '26")
    _add_card(db, "Beta", "BB", "r-b1", "sha-2", _drift(WORDS_A), "Kessler '26")
    report_dir = tmp_path / "reports"
    stats = run_dedup(db, report_dir)
    assert stats.merged == 1
    assert stats.disagreements == 0
    assert not (report_dir / REPORT_NAME).exists()


def test_empty_db(db, tmp_path):
    stats = run_dedup(db, tmp_path / "reports")
    assert stats == DedupStats(candidates=0, merged=0, trims=0, disagreements=0)

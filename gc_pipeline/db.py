"""SQLite schema and connection helpers for the GC report database."""

import datetime
import re
import sqlite3
import uuid

from gc_pipeline import channels

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id           INTEGER PRIMARY KEY,
  source_file      TEXT NOT NULL,
  content_hash     TEXT NOT NULL,
  sample_name      TEXT,
  sample_type      TEXT,
  sample_amount    REAL,
  instrument       TEXT,
  injection_date   TEXT,
  acq_method       TEXT,
  analysis_method  TEXT,
  signal           TEXT,
  last_changed     TEXT,
  raw_header_text  TEXT NOT NULL,
  duplicate_of     INTEGER REFERENCES runs(run_id),
  excluded         INTEGER NOT NULL DEFAULT 0,
  ingested_at      TEXT NOT NULL,
  run_number       INTEGER,
  notes            TEXT,
  is_synthetic_group INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source_file, content_hash)
);

CREATE TABLE IF NOT EXISTS peaks (
  peak_id       INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES runs(run_id),
  gas           TEXT NOT NULL,
  rt            REAL,
  rf            REAL,
  area          REAL,
  amount        REAL,
  concentration REAL
);

CREATE INDEX IF NOT EXISTS idx_runs_sample_name ON runs(sample_name);
CREATE INDEX IF NOT EXISTS idx_runs_injection_date ON runs(injection_date);
CREATE INDEX IF NOT EXISTS idx_runs_content_hash ON runs(content_hash);
CREATE INDEX IF NOT EXISTS idx_peaks_gas ON peaks(gas);

CREATE TABLE IF NOT EXISTS standards (
  standard_id  INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  description  TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS standard_compositions (
  standard_id  INTEGER NOT NULL REFERENCES standards(standard_id),
  gas          TEXT NOT NULL,
  value        REAL NOT NULL,
  units        TEXT NOT NULL DEFAULT 'percent',
  PRIMARY KEY (standard_id, gas)
);

CREATE TABLE IF NOT EXISTS standard_references (
  run_id      INTEGER PRIMARY KEY REFERENCES runs(run_id),
  pressure    REAL,
  entered_at  TEXT NOT NULL,
  standard_id INTEGER REFERENCES standards(standard_id)
);

CREATE TABLE IF NOT EXISTS ignored_hashes (
  content_hash TEXT PRIMARY KEY,
  ignored_at   TEXT NOT NULL,
  sample_name  TEXT,
  source_file  TEXT,
  run_number   INTEGER
);

CREATE TABLE IF NOT EXISTS calibration_series (
  series_id            INTEGER PRIMARY KEY,
  gas                  TEXT NOT NULL,
  name                 TEXT NOT NULL,
  slope                REAL,
  intercept            REAL,
  r_squared            REAL,
  force_through_origin INTEGER NOT NULL DEFAULT 0,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration_series_points (
  series_id       INTEGER NOT NULL REFERENCES calibration_series(series_id),
  run_id          INTEGER NOT NULL REFERENCES runs(run_id),
  gas             TEXT NOT NULL,
  excluded        INTEGER NOT NULL DEFAULT 0,
  override_area   REAL,
  override_amount REAL,
  PRIMARY KEY (series_id, run_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS sample_group_members (
  group_run_id  INTEGER NOT NULL REFERENCES runs(run_id),
  member_run_id INTEGER NOT NULL REFERENCES runs(run_id),
  PRIMARY KEY (group_run_id, member_run_id)
);

CREATE TABLE IF NOT EXISTS analysis_series_selection (
  run_id     INTEGER NOT NULL REFERENCES runs(run_id),
  gas        TEXT NOT NULL,
  series_id  INTEGER REFERENCES calibration_series(series_id),
  PRIMARY KEY (run_id, gas)
);

CREATE TABLE IF NOT EXISTS run_rounds (
  round_id       INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  numbering_mode TEXT NOT NULL DEFAULT 'time',
  name_is_auto   INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS highlight_swatches (
  swatch_id INTEGER PRIMARY KEY,
  color     TEXT NOT NULL,
  label     TEXT,
  position  INTEGER NOT NULL
);

-- Same swatch palette as runs.highlight_swatch_id, but scoped to one (run, gas)
-- cell in the Analysis tab's Results table rather than a whole row.
CREATE TABLE IF NOT EXISTS cell_highlights (
  run_id    INTEGER NOT NULL REFERENCES runs(run_id),
  gas       TEXT NOT NULL,
  swatch_id INTEGER NOT NULL REFERENCES highlight_swatches(swatch_id),
  PRIMARY KEY (run_id, gas)
);
"""

RUN_NUMBER_SAMPLE_ID_RE = re.compile(r"_(\d+)$")

# A reserved standard name: linking a run to "Sample" marks it as a known non-standard
# (distinct from leaving it unlinked, which just means "not yet decided") - it's a
# normal row in `standards` like any other, just always left with an empty
# composition, which is what naturally keeps it out of every calibration regression
# (build_calibration_candidates only produces a point where the linked standard has a
# composition entry for that gas).
SAMPLE_STANDARD_NAME = "Sample"


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add/backfill columns introduced after a database may already have been created
    and populated with real data - never drops or recreates existing tables."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "run_number" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN run_number INTEGER")
        conn.commit()
    if "notes" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN notes TEXT")
        conn.commit()
    if "is_synthetic_group" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN is_synthetic_group INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "round_id" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN round_id INTEGER REFERENCES run_rounds(round_id)")
        conn.commit()
    if "highlight_swatch_id" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN highlight_swatch_id INTEGER REFERENCES highlight_swatches(swatch_id)"
        )
        conn.commit()
    if "load_finalized" not in columns:
        # DEFAULT 1 applies to existing rows too - everything already in the
        # database predates this flag and is by definition already "reviewed".
        conn.execute("ALTER TABLE runs ADD COLUMN load_finalized INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    round_columns = {row["name"] for row in conn.execute("PRAGMA table_info(run_rounds)")}
    if "name_is_auto" not in round_columns:
        conn.execute("ALTER TABLE run_rounds ADD COLUMN name_is_auto INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    ignored_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ignored_hashes)")}
    for col in ("sample_name", "source_file", "run_number"):
        if col not in ignored_columns:
            col_type = "INTEGER" if col == "run_number" else "TEXT"
            conn.execute(f"ALTER TABLE ignored_hashes ADD COLUMN {col} {col_type}")
    conn.commit()

    sr_columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(standard_references)")}
    if "standard_id" not in sr_columns:
        conn.execute("ALTER TABLE standard_references ADD COLUMN standard_id INTEGER REFERENCES standards(standard_id)")
        conn.commit()
        sr_columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(standard_references)")}

    # Pressure used to be NOT NULL (one row = "a pressure was entered"), but a run can
    # now get a standard link before its pressure exists - SQLite can't ALTER a column's
    # NOT NULL in place, so rebuild the table when an old, still-NOT-NULL copy is found.
    if sr_columns["pressure"]["notnull"]:
        conn.executescript(
            """
            CREATE TABLE standard_references_new (
              run_id      INTEGER PRIMARY KEY REFERENCES runs(run_id),
              pressure    REAL,
              entered_at  TEXT NOT NULL,
              standard_id INTEGER REFERENCES standards(standard_id)
            );
            INSERT INTO standard_references_new (run_id, pressure, entered_at, standard_id)
                SELECT run_id, pressure, entered_at, standard_id FROM standard_references;
            DROP TABLE standard_references;
            ALTER TABLE standard_references_new RENAME TO standard_references;
            """
        )
        conn.commit()

    # Fractions have been retired in favor of percent-only compositions - convert any
    # pre-existing fraction rows in place rather than requiring the user to redo them.
    conn.execute(
        "UPDATE standard_compositions SET value = value * 100, units = 'percent' WHERE units = 'fraction'"
    )
    conn.commit()

    cs_columns = {row["name"] for row in conn.execute("PRAGMA table_info(calibration_series)")}
    if "force_through_origin" not in cs_columns:
        conn.execute("ALTER TABLE calibration_series ADD COLUMN force_through_origin INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    sr_columns2 = {row["name"] for row in conn.execute("PRAGMA table_info(standard_references)")}
    if "pressure_not_recorded" not in sr_columns2:
        conn.execute(
            "ALTER TABLE standard_references ADD COLUMN pressure_not_recorded INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()

    recompute_run_numbers(conn)
    ensure_sample_standard(conn)


def recompute_run_numbers(conn):
    """run_number = a run's 1-based rank by injection_date within its own date-folder
    - always derived from time, never parsed from a filename (a trailing digit like
    "Air_01" can mean "bottle instance", not injection order, depending on the batch).
    Safe/cheap to call after anything that changes which runs exist for a given day.

    Only touches runs with no round assigned (round_id IS NULL) - a run that's part
    of a user-defined round gets its number from recompute_round_numbers instead,
    which spans multiple calendar days on purpose; this per-date pass must never
    clobber that.

    Also only touches runs with load_finalized = 1 - a run fresh out of ingestion
    hasn't been through the Load Data tab's duplicate-check/pressure-entry review
    yet, so it has no business being ranked into (and shifting) the real numbering
    of already-reviewed runs. It sits with run_number = NULL until finalize_runs()
    marks it reviewed, at which point this function naturally picks it up."""
    rows = conn.execute(
        "SELECT run_id, injection_date FROM runs WHERE round_id IS NULL AND load_finalized = 1 "
        "ORDER BY injection_date"
    ).fetchall()
    groups = {}
    for row in rows:
        date_key = (row["injection_date"] or "")[:10]
        groups.setdefault(date_key, []).append(row["run_id"])
    for run_ids in groups.values():
        for rank, run_id in enumerate(run_ids, start=1):
            conn.execute("UPDATE runs SET run_number = ? WHERE run_id = ?", (rank, run_id))
    conn.commit()


def list_pending_load_runs(conn):
    """Runs still awaiting Load Data tab review (duplicate-check + pressure entry) -
    the Load tab's working set. Chronological order."""
    return conn.execute(
        "SELECT * FROM runs WHERE load_finalized = 0 ORDER BY injection_date"
    ).fetchall()


def count_pending_load_runs(conn):
    return conn.execute("SELECT COUNT(*) FROM runs WHERE load_finalized = 0").fetchone()[0]


def finalize_runs(conn, run_ids):
    """Marks these runs reviewed - the Load Data tab's "Save" action. Only after
    this do they participate in real run numbering: recompute_run_numbers picks up
    the non-round ones automatically, but a run can already carry a round_id at
    this point (duplicate resolution migrates round_id from whichever copy had one
    onto the surviving run, even before that run is reviewed) - those need their
    own round explicitly recomputed too, or they'd stay stuck with no run_number
    forever (recompute_run_numbers deliberately skips round-assigned runs)."""
    run_ids = list(run_ids)
    if not run_ids:
        return
    placeholders = ",".join("?" for _ in run_ids)
    conn.execute(f"UPDATE runs SET load_finalized = 1 WHERE run_id IN ({placeholders})", run_ids)
    conn.commit()
    round_ids = {
        row["round_id"] for row in conn.execute(
            f"SELECT DISTINCT round_id FROM runs WHERE run_id IN ({placeholders}) AND round_id IS NOT NULL",
            run_ids,
        )
    }
    for round_id in round_ids:
        recompute_round_numbers(conn, round_id)
    recompute_run_numbers(conn)


# -- run rounds (multi-day calibration cycles) ---------------------------------

def compute_round_auto_name(conn, run_ids):
    """Derives a name like "2026-07-29-to-08-01_Ar" (or "...-to-04_Ar-He" for a
    same-month span on multiple channels) from the given runs' date range and
    acq_method channels - the auto-generated default a round's name starts as, and
    what it's regenerated to (if still auto) whenever the round's membership grows."""
    run_ids = list(run_ids)
    if not run_ids:
        return "New round"
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT injection_date, acq_method FROM runs WHERE run_id IN ({placeholders})", run_ids
    ).fetchall()
    dates = sorted({(r["injection_date"] or "")[:10] for r in rows if r["injection_date"]})
    if not dates:
        return "New round"
    start, end = dates[0], dates[-1]
    if start == end:
        date_part = start
    else:
        start_y, start_m, _ = start.split("-")
        end_y, end_m, end_d = end.split("-")
        if end_y != start_y:
            end_str = end
        elif end_m != start_m:
            end_str = f"{end_m}-{end_d}"
        else:
            end_str = end_d
        date_part = f"{start}-to-{end_str}"
    channel_set = {channels.channel_of(r["acq_method"]) for r in rows if r["acq_method"]}
    channel_part = "-".join(sorted(channel_set)) if channel_set else "Unknown"
    return f"{date_part}_{channel_part}"


def create_run_round(conn, name, run_ids, numbering_mode="time", name_is_auto=False):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO run_rounds (name, numbering_mode, name_is_auto, created_at) VALUES (?, ?, ?, ?)",
        (name, numbering_mode, 1 if name_is_auto else 0, now),
    )
    round_id = cur.lastrowid
    conn.commit()
    add_runs_to_round(conn, round_id, run_ids)
    return round_id


def add_runs_to_round(conn, round_id, run_ids):
    run_ids = list(run_ids)
    if not run_ids:
        return
    placeholders = ",".join("?" for _ in run_ids)
    conn.execute(f"UPDATE runs SET round_id = ? WHERE run_id IN ({placeholders})", [round_id] + run_ids)
    conn.commit()
    round_row = conn.execute(
        "SELECT name_is_auto FROM run_rounds WHERE round_id = ?", (round_id,)
    ).fetchone()
    if round_row is not None and round_row["name_is_auto"]:
        all_members = [
            row["run_id"] for row in conn.execute("SELECT run_id FROM runs WHERE round_id = ?", (round_id,))
        ]
        new_name = compute_round_auto_name(conn, all_members)
        conn.execute("UPDATE run_rounds SET name = ? WHERE round_id = ?", (new_name, round_id))
        conn.commit()
    recompute_round_numbers(conn, round_id)


def remove_runs_from_round(conn, round_id, run_ids):
    """Clears these runs' round assignment - they fall back to ordinary per-date
    numbering (recompute_run_numbers) the next time that runs, same as a run that
    was never assigned to a round in the first place."""
    run_ids = list(run_ids)
    if not run_ids:
        return
    placeholders = ",".join("?" for _ in run_ids)
    conn.execute(
        f"UPDATE runs SET round_id = NULL WHERE round_id = ? AND run_id IN ({placeholders})",
        [round_id] + run_ids,
    )
    conn.commit()
    recompute_round_numbers(conn, round_id)
    recompute_run_numbers(conn)


def list_run_rounds(conn):
    return conn.execute("SELECT * FROM run_rounds ORDER BY name").fetchall()


def get_round_latest_dates(conn):
    """{round_id: latest injection_date string among its runs} - used to order
    round pickers by actual recency (newest first) rather than alphabetically by
    name, since a round's name doesn't reliably sort chronologically once renamed."""
    rows = conn.execute(
        "SELECT round_id, MAX(injection_date) AS latest FROM runs WHERE round_id IS NOT NULL GROUP BY round_id"
    ).fetchall()
    return {row["round_id"]: row["latest"] or "" for row in rows}


def get_run_rounds_by_id(conn):
    """{run_id: round_name or None} for every run - a lookup dict for display,
    mirroring the shape of get_run_standards."""
    rows = conn.execute(
        "SELECT r.run_id, rr.name AS round_name FROM runs r LEFT JOIN run_rounds rr ON rr.round_id = r.round_id"
    ).fetchall()
    return {row["run_id"]: row["round_name"] for row in rows}


def rename_run_round(conn, round_id, name):
    # An explicit rename always means "stop auto-updating this name" - otherwise
    # the next run added to the round would silently discard the user's own name.
    conn.execute(
        "UPDATE run_rounds SET name = ?, name_is_auto = 0 WHERE round_id = ?", (name, round_id)
    )
    conn.commit()


def set_round_numbering_mode(conn, round_id, mode):
    conn.execute("UPDATE run_rounds SET numbering_mode = ? WHERE round_id = ?", (mode, round_id))
    conn.commit()
    recompute_round_numbers(conn, round_id)


def recompute_round_numbers(conn, round_id):
    """Assigns 1..N within this round only, per its numbering_mode:
    - 'time': rank by injection_date ascending.
    - 'sample_id': rank by the sample name's trailing _<digits> (a run with no
      parseable trailing digit sorts last, tiebroken by injection_date).

    Only touches load_finalized = 1 runs, same reasoning as recompute_run_numbers -
    a run can pick up a round_id before it's been reviewed (e.g. resolving a
    duplicate migrates the surviving run's round_id from whichever copy had one),
    and it still shouldn't be ranked into the round's real numbering until finalized."""
    mode_row = conn.execute("SELECT numbering_mode FROM run_rounds WHERE round_id = ?", (round_id,)).fetchone()
    if mode_row is None:
        return
    rows = conn.execute(
        "SELECT run_id, sample_name, injection_date FROM runs WHERE round_id = ? AND load_finalized = 1",
        (round_id,),
    ).fetchall()

    if mode_row["numbering_mode"] == "sample_id":
        def sort_key(row):
            match = RUN_NUMBER_SAMPLE_ID_RE.search(row["sample_name"] or "")
            has_digit = match is None
            digit = int(match.group(1)) if match else 0
            return (has_digit, digit, row["injection_date"] or "")
    else:
        def sort_key(row):
            return (row["injection_date"] or "",)

    ordered = sorted(rows, key=sort_key)
    for rank, row in enumerate(ordered, start=1):
        conn.execute("UPDATE runs SET run_number = ? WHERE run_id = ?", (rank, row["run_id"]))
    conn.commit()


def set_run_number_manual(conn, run_id, new_number):
    """The one run-numbering entry point that does NOT do a full recompute - a
    user-typed override. Matches the confirmed "rename rank 20 to 21 -> old 21..40
    become 22..41" example: increasing a run's number shifts every OTHER run whose
    OLD number was greater than it by the same delta (a uniform shift of the whole
    tail, not a minimal reflow - it can leave a gap, by design). This direction has
    unlimited headroom (no upper bound on a run number), so a full-delta shift of
    the whole tail can never produce an invalid result.

    Decreasing is NOT the same "shift the whole head by the full delta" mirror -
    that formula hits a floor (run numbers don't go below 1) the moment the delta
    is large or the edited run's old number is small, producing negative/zero
    numbers (confirmed: moving rank 20 to 15 in a 40-run round pushed ranks 1-4 to
    -4..0). Since there's no equivalent unlimited room below, decreasing instead
    shifts only the band strictly between the new and old number - i.e. exactly
    what has to move to vacate the new slot and re-close the gap the edited run
    leaves behind - by a single step (+1), regardless of how large the delta
    itself is: e.g. rank 20 -> 15 moves whatever currently occupies 15..19 up to
    16..20 (a uniform +1 bulk shift, safe as one statement since the WHERE clause
    evaluates against the pre-update values), never touching anything below 15 or
    above 20.

    Returns False (writes nothing) if this run has no round assigned - manual
    numbering only makes sense within a round, since unassigned runs are always
    fully recomputed per-date anyway."""
    row = conn.execute("SELECT round_id, run_number FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None or row["round_id"] is None:
        return False
    old_number = row["run_number"] or 0
    delta = new_number - old_number
    if delta > 0:
        conn.execute(
            "UPDATE runs SET run_number = run_number + ? WHERE round_id = ? AND run_number > ? AND run_id != ?",
            (delta, row["round_id"], old_number, run_id),
        )
    elif delta < 0:
        conn.execute(
            "UPDATE runs SET run_number = run_number + 1 WHERE round_id = ? "
            "AND run_number >= ? AND run_number < ? AND run_id != ?",
            (row["round_id"], new_number, old_number, run_id),
        )
    conn.execute("UPDATE runs SET run_number = ? WHERE run_id = ?", (new_number, run_id))
    conn.commit()
    return True


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def find_run_by_source_and_hash(conn, source_file, content_hash):
    cur = conn.execute(
        "SELECT run_id FROM runs WHERE source_file = ? AND content_hash = ?",
        (source_file, content_hash),
    )
    return cur.fetchone()


def find_run_by_hash(conn, content_hash):
    """Return the earliest run with this content hash, if any (for duplicate detection)."""
    cur = conn.execute(
        "SELECT run_id FROM runs WHERE content_hash = ? ORDER BY run_id ASC LIMIT 1",
        (content_hash,),
    )
    return cur.fetchone()


def insert_run(conn, run_fields, peaks, duplicate_of, ingested_at):
    # load_finalized = 0: a freshly-ingested run sits outside the real numbering
    # until it's been through the Load Data tab's duplicate-check/pressure-entry
    # review and finalize_runs() marks it reviewed - see recompute_run_numbers.
    cur = conn.execute(
        """
        INSERT INTO runs (
            source_file, content_hash, sample_name, sample_type, sample_amount,
            instrument, injection_date, acq_method, analysis_method, signal,
            last_changed, raw_header_text, duplicate_of, excluded, ingested_at, run_number,
            load_finalized
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0)
        """,
        (
            run_fields["source_file"],
            run_fields["content_hash"],
            run_fields.get("sample_name"),
            run_fields.get("sample_type"),
            run_fields.get("sample_amount"),
            run_fields.get("instrument"),
            run_fields.get("injection_date"),
            run_fields.get("acq_method"),
            run_fields.get("analysis_method"),
            run_fields.get("signal"),
            run_fields.get("last_changed"),
            run_fields.get("raw_header_text", ""),
            duplicate_of,
            ingested_at,
            run_fields.get("run_number"),
        ),
    )
    run_id = cur.lastrowid
    for p in peaks:
        conn.execute(
            """
            INSERT INTO peaks (run_id, gas, rt, rf, area, amount, concentration)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, p["gas"], p["rt"], p["rf"], p["area"], p["amount"], p["concentration"]),
        )
    return run_id


def set_excluded(conn, run_id, excluded):
    conn.execute("UPDATE runs SET excluded = ? WHERE run_id = ?", (1 if excluded else 0, run_id))
    conn.commit()


def clear_duplicate(conn, run_id):
    """Treat this run as not actually a duplicate (clears its duplicate_of link)."""
    conn.execute("UPDATE runs SET duplicate_of = NULL WHERE run_id = ?", (run_id,))
    conn.commit()


def exclude_all_duplicates(conn):
    """Bulk-mark every currently-flagged duplicate as excluded. Returns the count affected."""
    cur = conn.execute("UPDATE runs SET excluded = 1 WHERE duplicate_of IS NOT NULL AND excluded = 0")
    conn.commit()
    return cur.rowcount


# -- Duplicates Wizard: resolve a group of byte-identical runs down to one -------

def get_duplicate_groups(conn):
    """{content_hash: [run_id, ...]} for every content_hash shared by more than one
    run, ordered oldest-ingested-first within each group - the grouping the
    Duplicates Wizard splits into "older" (left) / "newest" (right)."""
    rows = conn.execute(
        """
        SELECT run_id, content_hash FROM runs
        WHERE content_hash IN (
            SELECT content_hash FROM runs GROUP BY content_hash HAVING COUNT(*) > 1
        )
        ORDER BY ingested_at ASC, run_id ASC
        """
    ).fetchall()
    groups = {}
    for row in rows:
        groups.setdefault(row["content_hash"], []).append(row["run_id"])
    return groups


# -- Repair tool: same-identity groups (a strict superset of duplicate groups - also
# catches a reprocessed file, which changes content_hash but keeps the same physical
# run identity) --------------------------------------------------------------------

GROUP_PEAK_FIELD_PREFIX = "gas:"


def find_same_identity_groups(conn):
    """{(sample_name, injection_date): [run_id, ...]} (oldest-ingested-first) for
    every identity shared by more than one non-synthetic run - a strict superset of
    get_duplicate_groups' content-hash grouping, since it also catches same-identity
    runs whose content differs (reprocessed exports). This is the Repair tool's
    queue."""
    rows = conn.execute(
        """
        SELECT run_id, sample_name, injection_date FROM runs
        WHERE is_synthetic_group = 0 AND sample_name IS NOT NULL AND injection_date IS NOT NULL
          AND (sample_name, injection_date) IN (
              SELECT sample_name, injection_date FROM runs
              WHERE is_synthetic_group = 0 AND sample_name IS NOT NULL AND injection_date IS NOT NULL
              GROUP BY sample_name, injection_date HAVING COUNT(*) > 1
          )
        ORDER BY ingested_at ASC, run_id ASC
        """
    ).fetchall()
    groups = {}
    for row in rows:
        key = (row["sample_name"], row["injection_date"])
        groups.setdefault(key, []).append(row["run_id"])
    return groups


def get_group_field_matrix(conn, run_ids):
    """{field_name: {run_id: value}} for pressure, standard_id, round_id, run_number,
    notes, highlight_swatch_id, and one "gas:<gas>" entry per gas peak seen on any
    member run (value = that run's full peaks row for the gas, so rt/rf/area/amount/
    concentration move together as a unit, not the area alone)."""
    run_ids = list(run_ids)
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    runs_rows = {
        row["run_id"]: row
        for row in conn.execute(f"SELECT * FROM runs WHERE run_id IN ({placeholders})", run_ids)
    }
    sr_rows = {
        row["run_id"]: row
        for row in conn.execute(
            f"SELECT * FROM standard_references WHERE run_id IN ({placeholders})", run_ids
        )
    }
    matrix = {
        "pressure": {rid: (sr_rows[rid]["pressure"] if rid in sr_rows else None) for rid in run_ids},
        "standard_id": {rid: (sr_rows[rid]["standard_id"] if rid in sr_rows else None) for rid in run_ids},
        "round_id": {rid: runs_rows[rid]["round_id"] for rid in run_ids if rid in runs_rows},
        "run_number": {rid: runs_rows[rid]["run_number"] for rid in run_ids if rid in runs_rows},
        "notes": {rid: runs_rows[rid]["notes"] for rid in run_ids if rid in runs_rows},
        "highlight_swatch_id": {rid: runs_rows[rid]["highlight_swatch_id"] for rid in run_ids if rid in runs_rows},
    }
    peaks_by_gas = {}
    for p in get_peaks_for_runs(conn, run_ids):
        peaks_by_gas.setdefault(p["gas"], {})[p["run_id"]] = p
    for gas, by_run in peaks_by_gas.items():
        matrix[f"{GROUP_PEAK_FIELD_PREFIX}{gas}"] = by_run
    return matrix


def default_field_selections(field_matrix, run_ids):
    """field_name -> run_id: the pre-filled pick for each field - newest run (last in
    run_ids, which is oldest-ingested-first per find_same_identity_groups) that has a
    non-null value for peak fields ("newest CSV is right"), earliest run with a
    non-null value for the human-entered fields (pressure/standard/round/run_number/
    notes/highlight). A field with no non-null value on any member is left unset."""
    run_ids = list(run_ids)
    selections = {}
    for field_name, by_run in field_matrix.items():
        candidates = [rid for rid in run_ids if by_run.get(rid) is not None]
        if not candidates:
            continue
        if field_name.startswith(GROUP_PEAK_FIELD_PREFIX):
            selections[field_name] = candidates[-1]
        else:
            selections[field_name] = candidates[0]
    return selections


def merge_same_identity_group(conn, target_run_id, run_ids, selections):
    """Applies `selections` (field_name -> source run_id) onto target_run_id -
    replaces its peaks with the selected gas rows, sets pressure/standard_id/
    round_id/run_number/notes/highlight_swatch_id from the selected sources - then
    deletes every other run_id in the group (peaks/standard_references/runs rows),
    same FK-safe cleanup delete_runs already uses. Finishes with
    recompute_run_numbers (and recompute_round_numbers for the round the target ends
    up in, if any, same as finalize_runs).

    Each deleted run's content_hash is recorded in ignored_hashes, same as
    delete_runs does - unlike resolve_duplicate_group's byte-identical case (where
    the kept and discarded copies share one hash, so blocking it would also block
    the survivor), a same-identity group's members have genuinely different bytes.
    Without this, the raw CSV sitting untouched in a still-watched folder gets
    silently re-ingested on the next poll, recreating the exact run this merge just
    resolved - the surviving run's own hash is never touched, so it stays freely
    re-ingestable if it's ever deleted on its own later."""
    run_ids = list(run_ids)
    other_ids = [rid for rid in run_ids if rid != target_run_id]

    gas_selections = {
        field[len(GROUP_PEAK_FIELD_PREFIX):]: source_rid
        for field, source_rid in selections.items()
        if field.startswith(GROUP_PEAK_FIELD_PREFIX)
    }
    if gas_selections:
        source_ids = list({rid for rid in gas_selections.values()})
        peaks_by_run_gas = {(p["run_id"], p["gas"]): p for p in get_peaks_for_runs(conn, source_ids)}
        conn.execute("DELETE FROM peaks WHERE run_id = ?", (target_run_id,))
        for gas, source_rid in gas_selections.items():
            p = peaks_by_run_gas.get((source_rid, gas))
            if p is None:
                continue
            conn.execute(
                "INSERT INTO peaks (run_id, gas, rt, rf, area, amount, concentration) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (target_run_id, gas, p["rt"], p["rf"], p["area"], p["amount"], p["concentration"]),
            )

    runs_field_columns = ("round_id", "run_number", "notes", "highlight_swatch_id")
    for column in runs_field_columns:
        source_rid = selections.get(column)
        if source_rid is None:
            continue
        source_row = conn.execute(f"SELECT {column} FROM runs WHERE run_id = ?", (source_rid,)).fetchone()
        if source_row is not None:
            conn.execute(f"UPDATE runs SET {column} = ? WHERE run_id = ?", (source_row[column], target_run_id))

    pressure_source = selections.get("pressure")
    standard_source = selections.get("standard_id")
    if pressure_source is not None or standard_source is not None:
        existing_sr = conn.execute(
            "SELECT * FROM standard_references WHERE run_id = ?", (target_run_id,)
        ).fetchone()
        final_pressure = existing_sr["pressure"] if existing_sr else None
        final_standard = existing_sr["standard_id"] if existing_sr else None
        if pressure_source is not None:
            row = conn.execute(
                "SELECT pressure FROM standard_references WHERE run_id = ?", (pressure_source,)
            ).fetchone()
            final_pressure = row["pressure"] if row else final_pressure
        if standard_source is not None:
            row = conn.execute(
                "SELECT standard_id FROM standard_references WHERE run_id = ?", (standard_source,)
            ).fetchone()
            final_standard = row["standard_id"] if row else final_standard
        now = datetime.datetime.now().isoformat(timespec="seconds")
        if existing_sr is None:
            conn.execute(
                "INSERT INTO standard_references (run_id, pressure, standard_id, entered_at) VALUES (?, ?, ?, ?)",
                (target_run_id, final_pressure, final_standard, now),
            )
        else:
            conn.execute(
                "UPDATE standard_references SET pressure = ?, standard_id = ? WHERE run_id = ?",
                (final_pressure, final_standard, target_run_id),
            )

    conn.commit()

    now = datetime.datetime.now().isoformat(timespec="seconds")
    for other_id in other_ids:
        row = conn.execute(
            "SELECT content_hash, sample_name, source_file, run_number FROM runs WHERE run_id = ?",
            (other_id,),
        ).fetchone()
        if row is not None:
            # Same upsert delete_runs uses - without this, the raw CSV still sitting
            # in a watched folder gets silently re-ingested on the next poll,
            # recreating the exact run this merge just resolved.
            conn.execute(
                """
                INSERT INTO ignored_hashes (content_hash, ignored_at, sample_name, source_file, run_number)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    ignored_at = excluded.ignored_at,
                    sample_name = excluded.sample_name,
                    source_file = excluded.source_file,
                    run_number = excluded.run_number
                """,
                (row["content_hash"], now, row["sample_name"], row["source_file"], row["run_number"]),
            )
        _clear_run_references(conn, other_id)
        conn.execute("DELETE FROM peaks WHERE run_id = ?", (other_id,))
        conn.execute("DELETE FROM standard_references WHERE run_id = ?", (other_id,))
        conn.execute("DELETE FROM runs WHERE run_id = ?", (other_id,))
    conn.execute("UPDATE runs SET duplicate_of = NULL WHERE run_id = ?", (target_run_id,))
    conn.commit()

    recompute_run_numbers(conn)
    round_row = conn.execute("SELECT round_id FROM runs WHERE run_id = ?", (target_run_id,)).fetchone()
    if round_row is not None and round_row["round_id"] is not None:
        recompute_round_numbers(conn, round_row["round_id"])
    conn.commit()
    return target_run_id


def port_pressure_standard(conn, run_ids, copy_pressure=True, copy_standard=True):
    """Non-destructive: fills in any run's missing pressure/standard_id from
    whichever other run among run_ids has it (first found wins) - never deletes or
    overwrites an existing value. copy_pressure/copy_standard let the caller port
    just one of the two fields (the Duplicates Wizard's "Copy Pressures to Selected"
    / "Copy Standards to Selected" checkboxes). Returns the number of runs actually
    updated."""
    run_ids = list(run_ids)
    if len(run_ids) < 2 or (not copy_pressure and not copy_standard):
        return 0
    srs = {rid: conn.execute("SELECT * FROM standard_references WHERE run_id = ?", (rid,)).fetchone()
           for rid in run_ids}
    pressure = None
    if copy_pressure:
        pressure = next(
            (srs[rid]["pressure"] for rid in run_ids if srs[rid] and srs[rid]["pressure"] is not None), None
        )
    standard_id = None
    if copy_standard:
        standard_id = next(
            (srs[rid]["standard_id"] for rid in run_ids if srs[rid] and srs[rid]["standard_id"] is not None), None
        )
    if pressure is None and standard_id is None:
        return 0
    now = datetime.datetime.now().isoformat(timespec="seconds")
    updated = 0
    for rid in run_ids:
        sr = srs[rid]
        new_pressure = sr["pressure"] if sr and sr["pressure"] is not None else pressure
        new_standard = sr["standard_id"] if sr and sr["standard_id"] is not None else standard_id
        if not copy_pressure:
            new_pressure = sr["pressure"] if sr else None
        if not copy_standard:
            new_standard = sr["standard_id"] if sr else None
        if sr is None:
            if new_pressure is None and new_standard is None:
                continue
            conn.execute(
                "INSERT INTO standard_references (run_id, pressure, standard_id, entered_at) VALUES (?, ?, ?, ?)",
                (rid, new_pressure, new_standard, now),
            )
            updated += 1
        elif new_pressure != sr["pressure"] or new_standard != sr["standard_id"]:
            conn.execute(
                "UPDATE standard_references SET pressure = ?, standard_id = ? WHERE run_id = ?",
                (new_pressure, new_standard, rid),
            )
            updated += 1
    conn.commit()
    return updated


def resolve_duplicate_group(conn, keep_run_id, discard_run_ids):
    """Keep exactly one run out of a duplicate group, permanently deleting the rest -
    but first migrates any pressure/standard/notes/round/highlight data the kept run
    is missing from whichever discarded run has it (first non-null wins), so
    resolving duplicates never silently drops human-entered data. Unlike delete_runs,
    this deliberately does NOT touch ignored_hashes - the content_hash being
    "discarded" is still alive on the kept run, so blocking future re-ingestion of
    that same content would be wrong."""
    discard_run_ids = [rid for rid in discard_run_ids if rid != keep_run_id]
    if not discard_run_ids:
        return 0

    keep_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (keep_run_id,)).fetchone()
    if keep_row is None:
        return 0
    keep_sr = conn.execute(
        "SELECT * FROM standard_references WHERE run_id = ?", (keep_run_id,)
    ).fetchone()

    # -- migrate whatever the kept run is missing, from the first discarded run that has it --
    notes = keep_row["notes"]
    round_id = keep_row["round_id"]
    highlight_swatch_id = keep_row["highlight_swatch_id"]
    pressure = keep_sr["pressure"] if keep_sr else None
    standard_id = keep_sr["standard_id"] if keep_sr else None

    for discard_id in discard_run_ids:
        d_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (discard_id,)).fetchone()
        if d_row is None:
            continue
        if not notes and d_row["notes"]:
            notes = d_row["notes"]
        if round_id is None and d_row["round_id"] is not None:
            round_id = d_row["round_id"]
        if highlight_swatch_id is None and d_row["highlight_swatch_id"] is not None:
            highlight_swatch_id = d_row["highlight_swatch_id"]
        d_sr = conn.execute("SELECT * FROM standard_references WHERE run_id = ?", (discard_id,)).fetchone()
        if d_sr:
            if pressure is None and d_sr["pressure"] is not None:
                pressure = d_sr["pressure"]
            if standard_id is None and d_sr["standard_id"] is not None:
                standard_id = d_sr["standard_id"]
        # per-gas cell highlights: adopt any the kept run doesn't already have
        for ch in conn.execute("SELECT * FROM cell_highlights WHERE run_id = ?", (discard_id,)):
            exists = conn.execute(
                "SELECT 1 FROM cell_highlights WHERE run_id = ? AND gas = ?", (keep_run_id, ch["gas"])
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO cell_highlights (run_id, gas, swatch_id) VALUES (?, ?, ?)",
                    (keep_run_id, ch["gas"], ch["swatch_id"]),
                )

    conn.execute(
        "UPDATE runs SET notes = ?, round_id = ?, highlight_swatch_id = ? WHERE run_id = ?",
        (notes, round_id, highlight_swatch_id, keep_run_id),
    )
    if pressure is not None or standard_id is not None:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        if keep_sr is None:
            conn.execute(
                "INSERT INTO standard_references (run_id, pressure, standard_id, entered_at) VALUES (?, ?, ?, ?)",
                (keep_run_id, pressure, standard_id, now),
            )
        else:
            conn.execute(
                "UPDATE standard_references SET pressure = ?, standard_id = ? WHERE run_id = ?",
                (pressure, standard_id, keep_run_id),
            )

    for discard_id in discard_run_ids:
        _clear_run_references(conn, discard_id)
        conn.execute("DELETE FROM peaks WHERE run_id = ?", (discard_id,))
        conn.execute("DELETE FROM standard_references WHERE run_id = ?", (discard_id,))
        conn.execute("DELETE FROM runs WHERE run_id = ?", (discard_id,))
    # The kept run is now the sole copy of this content - it's no longer "a duplicate
    # of" anything (whatever it used to point at may itself have just been deleted).
    conn.execute("UPDATE runs SET duplicate_of = NULL WHERE run_id = ?", (keep_run_id,))
    recompute_run_numbers(conn)
    conn.commit()
    return len(discard_run_ids)


def query_runs(conn, sample_name=None, gases=None, date_from=None, date_to=None,
                acq_method=None, analysis_method=None, sample_type=None,
                show_duplicates=True, show_excluded=True):
    """gases: None means no gas filter; a list means only runs with at least one
    peak among those gases (an empty list matches nothing, mirroring an
    Excel-style filter with every box unchecked)."""
    sql = "SELECT DISTINCT r.* FROM runs r"
    joins = []
    # Synthetic averaged-group runs (Analysis tab's duplicate-averaging feature) are
    # never backed by a raw CSV and only make sense inside Analysis - the Selector,
    # Data Viewer, and everything downstream of "what's selected" should never see
    # them at all.
    where = ["r.is_synthetic_group = 0"]
    params = []

    if gases is not None:
        if not gases:
            where.append("0 = 1")
        else:
            joins.append("JOIN peaks p ON p.run_id = r.run_id")
            placeholders = ",".join("?" for _ in gases)
            where.append(f"p.gas IN ({placeholders})")
            params.extend(gases)

    if sample_name:
        where.append("r.sample_name LIKE ?")
        params.append(f"%{sample_name}%")
    if date_from:
        where.append("r.injection_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("r.injection_date <= ?")
        params.append(date_to)
    if acq_method:
        where.append("r.acq_method = ?")
        params.append(acq_method)
    if analysis_method:
        where.append("r.analysis_method = ?")
        params.append(analysis_method)
    if sample_type:
        where.append("r.sample_type = ?")
        params.append(sample_type)
    if not show_duplicates:
        where.append("r.duplicate_of IS NULL")
    if not show_excluded:
        where.append("r.excluded = 0")

    if joins:
        sql += " " + " ".join(joins)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.injection_date"

    return conn.execute(sql, params).fetchall()


def get_peaks_for_run(conn, run_id):
    return conn.execute(
        "SELECT * FROM peaks WHERE run_id = ? ORDER BY rt", (run_id,)
    ).fetchall()


def get_peaks_for_runs(conn, run_ids):
    """Every peak belonging to any of run_ids, each row carrying its run's sample_name."""
    run_ids = list(run_ids)
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    return conn.execute(
        f"""
        SELECT p.*, r.sample_name, r.injection_date
        FROM peaks p JOIN runs r ON r.run_id = p.run_id
        WHERE p.run_id IN ({placeholders})
        ORDER BY r.sample_name, p.gas
        """,
        run_ids,
    ).fetchall()


def set_run_notes(conn, run_id, notes):
    conn.execute("UPDATE runs SET notes = ? WHERE run_id = ?", (notes, run_id))
    conn.commit()


def get_data_viewer_rows(conn, run_ids):
    """One row per run_id: every runs.* field plus its linked pressure and standard
    name (both NULL-able) - same join shape as list_runs_for_pressure_entry, but keyed
    for the Data Viewer's own row/column model rather than a fixed pressure-entry list."""
    run_ids = list(run_ids)
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    return conn.execute(
        f"""
        SELECT r.*, sr.pressure AS pressure, s.name AS standard_name
        FROM runs r
        LEFT JOIN standard_references sr ON sr.run_id = r.run_id
        LEFT JOIN standards s ON s.standard_id = sr.standard_id
        WHERE r.run_id IN ({placeholders})
        """,
        run_ids,
    ).fetchall()


def get_peaks_map_for_runs(conn, run_ids):
    """{run_id: {gas: peaks-row}} - the same underlying data as get_peaks_for_runs,
    indexed for O(1) per-(run, gas) lookup instead of a flat list."""
    result = {}
    for p in get_peaks_for_runs(conn, run_ids):
        result.setdefault(p["run_id"], {})[p["gas"]] = p
    return result


def list_gases(conn):
    return [row["gas"] for row in conn.execute("SELECT DISTINCT gas FROM peaks ORDER BY gas")]


def list_composition_gas_names(conn):
    """Every gas name that appears in any standard's composition - a strict
    superset of list_gases() once a gas has been manually added via the Standards
    window's "Add gas..." (a standard's known composition, e.g. from its
    certificate of analysis, can include a component the instrument has never
    actually reported a peak for - a real mismatch worth being able to record
    rather than silently dropping)."""
    return [row["gas"] for row in conn.execute("SELECT DISTINCT gas FROM standard_compositions ORDER BY gas")]


def get_gas_acq_methods(conn):
    """Every distinct (gas, acq_method) pairing ever ingested - used to work out
    which instrument channel(s) a gas has actually been reported on, since which
    gases a run can report depends on whether it was run on the Ar or He channel."""
    return conn.execute(
        "SELECT DISTINCT p.gas, r.acq_method FROM peaks p JOIN runs r ON r.run_id = p.run_id"
    ).fetchall()


def get_gas_channels(conn):
    """{gas: {channels it's ever been reported on}} - derived fresh from the data
    (not a hardcoded list), so a brand new hydrocarbon name reported on the He
    channel gets classified automatically without a code change."""
    result = {}
    for row in get_gas_acq_methods(conn):
        result.setdefault(row["gas"], set()).add(channels.channel_of(row["acq_method"]))
    return result


def list_runs_for_pressure_entry(conn, run_ids=None, missing_only=False):
    """Runs in chronological order, each carrying its standard_references.pressure
    (NULL if never entered). Optionally restricted to a specific run_ids set and/or
    to only the rows still missing a pressure.

    When run_ids is given explicitly, whatever's asked for is returned as-is (this is
    how the Analysis tab looks up its own synthetic averaged-group runs, which are
    never backed by a raw CSV). When run_ids is None - a blanket "browse everything"
    call, e.g. the pressure wizard's general backfill view - synthetic group runs are
    excluded, since they only make sense inside Analysis."""
    sql = """
        SELECT r.run_id, r.sample_name, r.injection_date, r.run_number, r.notes, r.round_id, sr.pressure,
               sr.standard_id, s.name AS standard_name,
               COALESCE(sr.pressure_not_recorded, 0) AS pressure_not_recorded
        FROM runs r
        LEFT JOIN standard_references sr ON sr.run_id = r.run_id
        LEFT JOIN standards s ON s.standard_id = sr.standard_id
    """
    where = []
    params = []
    if run_ids is not None:
        run_ids = list(run_ids)
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        where.append(f"r.run_id IN ({placeholders})")
        params.extend(run_ids)
    else:
        where.append("r.is_synthetic_group = 0")
    if missing_only:
        where.append("sr.pressure IS NULL AND COALESCE(sr.pressure_not_recorded, 0) = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.injection_date"
    return conn.execute(sql, params).fetchall()


def count_missing_pressure(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM runs r LEFT JOIN standard_references sr ON sr.run_id = r.run_id "
        "WHERE sr.pressure IS NULL AND COALESCE(sr.pressure_not_recorded, 0) = 0 AND r.is_synthetic_group = 0"
    ).fetchone()[0]


def set_pressures(conn, values):
    """values: {run_id: pressure or None}. Upserts each (None clears a previously
    entered pressure while leaving any linked standard alone), single commit. Setting
    a real numeric pressure always clears pressure_not_recorded - entering a value is
    itself the "actually, it was recorded" correction."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for run_id, pressure in values.items():
        conn.execute(
            """
            INSERT INTO standard_references (run_id, pressure, entered_at, pressure_not_recorded)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(run_id) DO UPDATE SET
                pressure = excluded.pressure, entered_at = excluded.entered_at,
                pressure_not_recorded = CASE WHEN excluded.pressure IS NOT NULL THEN 0
                                             ELSE standard_references.pressure_not_recorded END
            """,
            (run_id, pressure, now),
        )
    conn.commit()


def set_pressure_not_recorded(conn, run_id, flag):
    """Marks a run's pressure as deliberately "not recorded" (the scientist forgot
    to take a reading, or it genuinely doesn't apply) rather than "not yet entered" -
    stops the pressure wizard's missing-pressure nag for that run without faking a
    zero/blank value that would otherwise look identical to "just haven't gotten to
    it yet". Clears any existing numeric pressure when set, since the two are
    mutually exclusive."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO standard_references (run_id, pressure, entered_at, pressure_not_recorded)
        VALUES (?, NULL, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            pressure = CASE WHEN excluded.pressure_not_recorded = 1 THEN NULL ELSE standard_references.pressure END,
            pressure_not_recorded = excluded.pressure_not_recorded,
            entered_at = excluded.entered_at
        """,
        (run_id, now, 1 if flag else 0),
    )
    conn.commit()


def is_hash_ignored(conn, content_hash):
    row = conn.execute(
        "SELECT 1 FROM ignored_hashes WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    return row is not None


def _clear_run_references(conn, run_id):
    """Every table besides `runs`/`peaks`/`standard_references` that can reference a
    run_id - clears/detaches all of them so a subsequent DELETE FROM runs never trips
    a foreign-key error. delete_runs previously only cleared duplicate_of, which
    worked by luck (real-world deletions rarely touched a run already in a
    calibration series or sample group) - this closes that gap for good."""
    conn.execute("UPDATE runs SET duplicate_of = NULL WHERE duplicate_of = ?", (run_id,))
    conn.execute("DELETE FROM calibration_series_points WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM sample_group_members WHERE member_run_id = ? OR group_run_id = ?", (run_id, run_id))
    conn.execute("DELETE FROM analysis_series_selection WHERE run_id = ?", (run_id,))
    conn.execute("DELETE FROM cell_highlights WHERE run_id = ?", (run_id,))


def delete_runs(conn, run_ids):
    """Permanently remove these runs (and their peaks/pressure entries), recording
    each content_hash in ignored_hashes so re-ingesting the same bytes later doesn't
    just recreate the row. Raw CSVs on disk are never touched - this only prunes the
    database's copy. Returns the number of runs deleted."""
    run_ids = list(run_ids)
    if not run_ids:
        return 0
    now = datetime.datetime.now().isoformat(timespec="seconds")
    deleted = 0
    for run_id in run_ids:
        row = conn.execute(
            "SELECT content_hash, sample_name, source_file, run_number FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            continue
        # Upsert (not INSERT OR IGNORE) - if this hash was already ignored from an
        # earlier deletion, refresh the snapshot with this run's metadata rather than
        # silently keeping whatever (possibly blank/stale) info was there before.
        conn.execute(
            """
            INSERT INTO ignored_hashes (content_hash, ignored_at, sample_name, source_file, run_number)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                ignored_at = excluded.ignored_at,
                sample_name = excluded.sample_name,
                source_file = excluded.source_file,
                run_number = excluded.run_number
            """,
            (row["content_hash"], now, row["sample_name"], row["source_file"], row["run_number"]),
        )
        _clear_run_references(conn, run_id)
        conn.execute("DELETE FROM peaks WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM standard_references WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        deleted += 1
    if deleted:
        # The remaining runs in whatever date-folder(s) these belonged to need to
        # renumber contiguously now that some are gone.
        recompute_run_numbers(conn)
    conn.commit()
    return deleted


def list_ignored_hashes(conn):
    return conn.execute("SELECT * FROM ignored_hashes ORDER BY ignored_at DESC").fetchall()


def unignore_hashes(conn, content_hashes):
    """Remove these hashes from the ignore list - the run itself doesn't reappear on
    its own, but a subsequent ingest of its original file will no longer be blocked."""
    content_hashes = list(content_hashes)
    if not content_hashes:
        return
    placeholders = ",".join("?" for _ in content_hashes)
    conn.execute(f"DELETE FROM ignored_hashes WHERE content_hash IN ({placeholders})", content_hashes)
    conn.commit()


# -- standards / known compositions -----------------------------------------

def create_standard(conn, name, description=""):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO standards (name, description, created_at) VALUES (?, ?, ?)",
        (name, description, now),
    )
    conn.commit()
    return cur.lastrowid


def rename_standard(conn, standard_id, name, description):
    conn.execute(
        "UPDATE standards SET name = ?, description = ? WHERE standard_id = ?",
        (name, description, standard_id),
    )
    conn.commit()


def delete_standard(conn, standard_id):
    """Clears the link on any runs pointing to this standard first (FK safety, same
    pattern as delete_runs clearing duplicate_of), then removes the standard itself."""
    conn.execute("UPDATE standard_references SET standard_id = NULL WHERE standard_id = ?", (standard_id,))
    conn.execute("DELETE FROM standard_compositions WHERE standard_id = ?", (standard_id,))
    conn.execute("DELETE FROM standards WHERE standard_id = ?", (standard_id,))
    conn.commit()


def list_standards(conn):
    return conn.execute("SELECT * FROM standards ORDER BY name").fetchall()


def get_run_standards(conn):
    """{run_id: standard_name or None} for every run - used by the main browser's
    "Standard" filter to show which runs are linked to which known standard."""
    rows = conn.execute(
        """
        SELECT r.run_id, s.name AS standard_name
        FROM runs r
        LEFT JOIN standard_references sr ON sr.run_id = r.run_id
        LEFT JOIN standards s ON s.standard_id = sr.standard_id
        """
    ).fetchall()
    return {row["run_id"]: row["standard_name"] for row in rows}


def get_standard_compositions(conn, standard_id):
    return conn.execute(
        "SELECT * FROM standard_compositions WHERE standard_id = ? ORDER BY gas",
        (standard_id,),
    ).fetchall()


def upsert_standard_composition(conn, standard_id, gas, value):
    """Single-field autosave - unlike set_standard_compositions (a full replace-all,
    meant for the explicit bulk save path), this touches only the one gas being
    edited, so a transiently invalid value being typed into a sibling field can never
    clobber this one (or vice versa)."""
    conn.execute(
        "INSERT INTO standard_compositions (standard_id, gas, value, units) VALUES (?, ?, ?, 'percent') "
        "ON CONFLICT(standard_id, gas) DO UPDATE SET value = excluded.value, units = excluded.units",
        (standard_id, gas, value),
    )
    conn.commit()


def delete_standard_composition(conn, standard_id, gas):
    conn.execute(
        "DELETE FROM standard_compositions WHERE standard_id = ? AND gas = ?", (standard_id, gas)
    )
    conn.commit()


def set_standard_compositions(conn, standard_id, rows):
    """rows: [{gas, value, units}]. Replace-all: the given rows become the complete
    composition for this standard - anything not included is removed."""
    conn.execute("DELETE FROM standard_compositions WHERE standard_id = ?", (standard_id,))
    for row in rows:
        conn.execute(
            "INSERT INTO standard_compositions (standard_id, gas, value, units) VALUES (?, ?, ?, ?)",
            (standard_id, row["gas"], row["value"], row["units"]),
        )
    conn.commit()


def set_run_standard(conn, run_id, standard_id):
    """Upserts the standard link for a run - pressure, standard, and notes can each be
    entered independently and in any order, so this works even if no pressure has been
    saved for the run yet (its standard_references row is created with pressure NULL)."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO standard_references (run_id, pressure, standard_id, entered_at)
        VALUES (?, NULL, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET standard_id = excluded.standard_id
        """,
        (run_id, standard_id, now),
    )
    conn.commit()


def remove_pressure_entry(conn, run_id):
    """Fully clears a run's pressure + standard link (deletes its standard_references
    row entirely) - the "remove this entry" action in the pressure wizard."""
    conn.execute("DELETE FROM standard_references WHERE run_id = ?", (run_id,))
    conn.commit()


# -- calibration series (Models tab) -----------------------------------------

def create_calibration_series(conn, gas, name):
    # New series default to forcing the fit through the origin - a calibration
    # curve should read 0 amount at 0 peak area, and that's overwhelmingly what's
    # wanted in practice; still a per-series toggle (the "Zero" column/checkbox)
    # for the rare case a real nonzero intercept is actually correct.
    now = datetime.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO calibration_series (gas, name, force_through_origin, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?)",
        (gas, name, now, now),
    )
    conn.commit()
    return cur.lastrowid


def list_calibration_series(conn, gas=None):
    if gas is None:
        return conn.execute("SELECT * FROM calibration_series ORDER BY gas, name").fetchall()
    return conn.execute(
        "SELECT * FROM calibration_series WHERE gas = ? ORDER BY name", (gas,)
    ).fetchall()


def rename_calibration_series(conn, series_id, name):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE calibration_series SET name = ?, updated_at = ? WHERE series_id = ?",
        (name, now, series_id),
    )
    conn.commit()


def delete_calibration_series(conn, series_id):
    # Clear any Analysis-tab cell that had this series selected first (FK safety,
    # same pattern as delete_standard clearing standard_references.standard_id) -
    # AnalysisPanel.on_curves_changed() is what notices the NULL and picks a
    # replacement curve for those cells.
    conn.execute("UPDATE analysis_series_selection SET series_id = NULL WHERE series_id = ?", (series_id,))
    conn.execute("DELETE FROM calibration_series_points WHERE series_id = ?", (series_id,))
    conn.execute("DELETE FROM calibration_series WHERE series_id = ?", (series_id,))
    conn.commit()


def get_calibration_series_points(conn, series_id):
    return conn.execute(
        "SELECT * FROM calibration_series_points WHERE series_id = ?", (series_id,)
    ).fetchall()


def set_calibration_series_points(conn, series_id, run_ids, gas):
    """Replace-all membership for this series: exactly these run_ids (all for the
    series' own gas) become its points. Points removed by this call are dropped
    entirely (their override/excluded state doesn't follow a point that's no longer
    in any series - the UI only ever calls this to define a series's full membership)."""
    existing = {row["run_id"]: row for row in get_calibration_series_points(conn, series_id)}
    run_ids = list(run_ids)
    conn.execute("DELETE FROM calibration_series_points WHERE series_id = ?", (series_id,))
    for run_id in run_ids:
        prior = existing.get(run_id)
        conn.execute(
            """
            INSERT INTO calibration_series_points
                (series_id, run_id, gas, excluded, override_area, override_amount)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                series_id, run_id, gas,
                prior["excluded"] if prior else 0,
                prior["override_area"] if prior else None,
                prior["override_amount"] if prior else None,
            ),
        )
    conn.commit()


def add_calibration_series_point(conn, series_id, run_id, gas):
    """Add run_id to this series' membership without touching any other series it
    may already belong to - unlike set_calibration_series_points (a replace-all for
    one series), this is what lets a single point be plotted/fit in two different
    series at once."""
    existing = get_calibration_series_points(conn, series_id)
    if any(row["run_id"] == run_id for row in existing):
        return
    conn.execute(
        """
        INSERT INTO calibration_series_points
            (series_id, run_id, gas, excluded, override_area, override_amount)
        VALUES (?, ?, ?, 0, NULL, NULL)
        """,
        (series_id, run_id, gas),
    )
    conn.commit()


def set_point_state(conn, series_id, run_id, excluded=None, override_area=None, override_amount=None,
                     clear_override=False):
    """Partial update of one point's excluded/override state. `excluded=None` leaves
    that flag unchanged; pass `clear_override=True` to reset both override_area and
    override_amount back to NULL (moving a point back to its real, derived values)."""
    row = conn.execute(
        "SELECT * FROM calibration_series_points WHERE series_id = ? AND run_id = ?",
        (series_id, run_id),
    ).fetchone()
    if row is None:
        return
    new_excluded = row["excluded"] if excluded is None else (1 if excluded else 0)
    if clear_override:
        new_area, new_amount = None, None
    else:
        new_area = row["override_area"] if override_area is None else override_area
        new_amount = row["override_amount"] if override_amount is None else override_amount
    conn.execute(
        "UPDATE calibration_series_points SET excluded = ?, override_area = ?, override_amount = ? "
        "WHERE series_id = ? AND run_id = ?",
        (new_excluded, new_area, new_amount, series_id, run_id),
    )
    conn.commit()


def set_series_force_through_origin(conn, series_id, enforced):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE calibration_series SET force_through_origin = ?, updated_at = ? WHERE series_id = ?",
        (1 if enforced else 0, now, series_id),
    )
    conn.commit()


def save_calibration_fit(conn, series_id, slope, intercept, r_squared):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE calibration_series SET slope = ?, intercept = ?, r_squared = ?, updated_at = ? "
        "WHERE series_id = ?",
        (slope, intercept, r_squared, now, series_id),
    )
    conn.commit()


# -- Analysis tab: duplicate-averaging sample groups --------------------------

def ensure_sample_standard(conn):
    row = conn.execute(
        "SELECT standard_id FROM standards WHERE name = ?", (SAMPLE_STANDARD_NAME,)
    ).fetchone()
    if row is not None:
        return row["standard_id"]
    return create_standard(conn, SAMPLE_STANDARD_NAME)


def create_sample_group(conn, member_run_ids):
    """Creates a real, persisted synthetic `runs` row representing the average of
    member_run_ids (never backed by a raw CSV - is_synthetic_group=1), links it to the
    reserved "Sample" standard, records its membership, and populates its averaged
    peaks/pressure via recompute_sample_group. Returns the new group_run_id."""
    member_run_ids = list(member_run_ids)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    names = [
        row["sample_name"] or f"run {row['run_id']}"
        for row in conn.execute(
            f"SELECT run_id, sample_name FROM runs WHERE run_id IN ({','.join('?' for _ in member_run_ids)})",
            member_run_ids,
        )
    ]
    cur = conn.execute(
        """
        INSERT INTO runs (
            source_file, content_hash, sample_name, raw_header_text,
            excluded, ingested_at, is_synthetic_group
        ) VALUES (?, ?, ?, '', 0, ?, 1)
        """,
        (f"__group__/{uuid.uuid4()}", f"group:{uuid.uuid4()}", "Avg: " + ", ".join(names), now),
    )
    group_run_id = cur.lastrowid
    for member_run_id in member_run_ids:
        conn.execute(
            "INSERT INTO sample_group_members (group_run_id, member_run_id) VALUES (?, ?)",
            (group_run_id, member_run_id),
        )
    conn.commit()
    set_run_standard(conn, group_run_id, ensure_sample_standard(conn))
    recompute_sample_group(conn, group_run_id)
    return group_run_id


def recompute_sample_group(conn, group_run_id):
    """Re-derives a group's synthetic peaks (mean area per gas, averaged only over
    members that actually have that gas) and standard_references.pressure (mean of
    members' entered pressures) from its current sample_group_members. Called after
    creation and after any membership change so the synthetic values never drift
    stale relative to the real data they're derived from."""
    member_ids = [
        row["member_run_id"]
        for row in conn.execute(
            "SELECT member_run_id FROM sample_group_members WHERE group_run_id = ?", (group_run_id,)
        )
    ]
    areas_by_gas = {}
    for p in get_peaks_for_runs(conn, member_ids):
        if p["area"] is not None:
            areas_by_gas.setdefault(p["gas"], []).append(p["area"])
    conn.execute("DELETE FROM peaks WHERE run_id = ?", (group_run_id,))
    for gas, areas in areas_by_gas.items():
        conn.execute(
            "INSERT INTO peaks (run_id, gas, area) VALUES (?, ?, ?)",
            (group_run_id, gas, sum(areas) / len(areas)),
        )

    pressures = [
        row["pressure"]
        for row in conn.execute(
            f"SELECT pressure FROM standard_references WHERE run_id IN "
            f"({','.join('?' for _ in member_ids)}) AND pressure IS NOT NULL",
            member_ids,
        )
    ] if member_ids else []
    avg_pressure = sum(pressures) / len(pressures) if pressures else None
    now = datetime.datetime.now().isoformat(timespec="seconds")
    standard_id = ensure_sample_standard(conn)
    conn.execute(
        """
        INSERT INTO standard_references (run_id, pressure, standard_id, entered_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET pressure = excluded.pressure
        """,
        (group_run_id, avg_pressure, standard_id, now),
    )
    conn.commit()
    _carry_highlight_notes_to_group(conn, group_run_id, member_ids)


_HIGHLIGHT_NOTE_BLOCK_RE = re.compile(r"\[\[highlights\]\].*?\[\[/highlights\]\]\n?", re.DOTALL)


def _carry_highlight_notes_to_group(conn, group_run_id, member_ids):
    """A highlighted member run/cell represents something a human deliberately
    flagged (e.g. "diluted sample", "possible contamination") - averaging would
    otherwise silently lose that. Every member's highlight label is appended to the
    group's own notes (replacing any previously-carried block, so edits/removals on
    the members don't leave stale duplicate lines behind on every recompute), and if
    every member sharing a highlight uses the same swatch, that swatch is applied to
    the group's own row/cell too, not just noted in text."""
    if not member_ids:
        return
    placeholders = ",".join("?" for _ in member_ids)
    note_lines = []

    run_swatches = conn.execute(
        f"""
        SELECT r.run_id, r.sample_name, hs.swatch_id, hs.color, hs.label
        FROM runs r JOIN highlight_swatches hs ON hs.swatch_id = r.highlight_swatch_id
        WHERE r.run_id IN ({placeholders})
        """,
        member_ids,
    ).fetchall()
    for row in run_swatches:
        note_lines.append(f"{row['sample_name'] or row['run_id']}: {row['label'] or row['color']}")
    distinct_run_swatches = {row["swatch_id"] for row in run_swatches}
    set_run_highlight(conn, group_run_id, next(iter(distinct_run_swatches)) if len(distinct_run_swatches) == 1 else None)

    cell_rows = conn.execute(
        f"""
        SELECT ch.run_id, ch.gas, r.sample_name, hs.swatch_id, hs.color, hs.label
        FROM cell_highlights ch
        JOIN runs r ON r.run_id = ch.run_id
        JOIN highlight_swatches hs ON hs.swatch_id = ch.swatch_id
        WHERE ch.run_id IN ({placeholders})
        """,
        member_ids,
    ).fetchall()
    by_gas = {}
    for row in cell_rows:
        by_gas.setdefault(row["gas"], []).append(row)
        note_lines.append(f"{row['sample_name'] or row['run_id']} {row['gas']}: {row['label'] or row['color']}")
    all_gases = {row["gas"] for row in cell_rows}
    for gas in all_gases:
        distinct = {r["swatch_id"] for r in by_gas[gas]}
        set_cell_highlight(conn, group_run_id, gas, next(iter(distinct)) if len(distinct) == 1 else None)

    existing = conn.execute("SELECT notes FROM runs WHERE run_id = ?", (group_run_id,)).fetchone()
    base_notes = _HIGHLIGHT_NOTE_BLOCK_RE.sub("", (existing["notes"] or "") if existing else "").rstrip()
    if note_lines:
        block = "[[highlights]]\n" + "\n".join(note_lines) + "\n[[/highlights]]"
        new_notes = (base_notes + "\n" + block).strip() if base_notes else block
    else:
        new_notes = base_notes
    conn.execute("UPDATE runs SET notes = ? WHERE run_id = ?", (new_notes, group_run_id))
    conn.commit()


def add_sample_group_member(conn, group_run_id, member_run_id):
    conn.execute(
        "INSERT OR IGNORE INTO sample_group_members (group_run_id, member_run_id) VALUES (?, ?)",
        (group_run_id, member_run_id),
    )
    conn.commit()
    recompute_sample_group(conn, group_run_id)


def remove_sample_group_member(conn, group_run_id, member_run_id):
    """Removes one run from a group; if that would leave the group with <=1 member,
    the group no longer means anything as an "average", so it's dissolved instead."""
    conn.execute(
        "DELETE FROM sample_group_members WHERE group_run_id = ? AND member_run_id = ?",
        (group_run_id, member_run_id),
    )
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM sample_group_members WHERE group_run_id = ?", (group_run_id,)
    ).fetchone()[0]
    if remaining <= 1:
        dissolve_sample_group(conn, group_run_id)
    else:
        recompute_sample_group(conn, group_run_id)


def dissolve_sample_group(conn, group_run_id):
    """Deletes the synthetic group entirely (its peaks/pressure/membership/runs row) -
    the member runs it was averaging are completely untouched."""
    conn.execute("DELETE FROM sample_group_members WHERE group_run_id = ?", (group_run_id,))
    conn.execute("DELETE FROM peaks WHERE run_id = ?", (group_run_id,))
    conn.execute("DELETE FROM standard_references WHERE run_id = ?", (group_run_id,))
    conn.execute("DELETE FROM analysis_series_selection WHERE run_id = ?", (group_run_id,))
    conn.execute("DELETE FROM runs WHERE run_id = ?", (group_run_id,))
    conn.commit()


def list_sample_groups(conn):
    """{group_run_id: [member_run_id, ...]} for every currently-defined group."""
    result = {}
    for row in conn.execute("SELECT group_run_id, member_run_id FROM sample_group_members"):
        result.setdefault(row["group_run_id"], []).append(row["member_run_id"])
    return result


# -- Analysis tab: per-cell calibration-series selection -----------------------

def get_analysis_series_selection(conn, run_ids):
    run_ids = list(run_ids)
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT run_id, gas, series_id FROM analysis_series_selection WHERE run_id IN ({placeholders})",
        run_ids,
    ).fetchall()
    return {(row["run_id"], row["gas"]): row["series_id"] for row in rows}


def set_analysis_series_selection(conn, run_id, gas, series_id):
    conn.execute(
        "INSERT INTO analysis_series_selection (run_id, gas, series_id) VALUES (?, ?, ?) "
        "ON CONFLICT(run_id, gas) DO UPDATE SET series_id = excluded.series_id",
        (run_id, gas, series_id),
    )
    conn.commit()


# -- row highlight swatches (a small user-managed color palette, assignable per
# run, that shows up as a chip wherever that run appears - Selector, Data Viewer,
# and the Analysis tab's Results table) --------------------------------------

def list_highlight_swatches(conn):
    return conn.execute("SELECT * FROM highlight_swatches ORDER BY position").fetchall()


def create_highlight_swatch(conn, color, label=""):
    max_pos = conn.execute("SELECT MAX(position) FROM highlight_swatches").fetchone()[0]
    position = 0 if max_pos is None else max_pos + 1
    cur = conn.execute(
        "INSERT INTO highlight_swatches (color, label, position) VALUES (?, ?, ?)",
        (color, label, position),
    )
    conn.commit()
    return cur.lastrowid


def update_highlight_swatch(conn, swatch_id, color=None, label=None):
    row = conn.execute(
        "SELECT color, label FROM highlight_swatches WHERE swatch_id = ?", (swatch_id,)
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE highlight_swatches SET color = ?, label = ? WHERE swatch_id = ?",
        (color if color is not None else row["color"], label if label is not None else row["label"], swatch_id),
    )
    conn.commit()


def delete_highlight_swatch(conn, swatch_id):
    """Clears the swatch from any runs/cells using it first (FK safety, same pattern
    as delete_standard clearing standard_references.standard_id), then removes it."""
    conn.execute("UPDATE runs SET highlight_swatch_id = NULL WHERE highlight_swatch_id = ?", (swatch_id,))
    conn.execute("DELETE FROM cell_highlights WHERE swatch_id = ?", (swatch_id,))
    conn.execute("DELETE FROM highlight_swatches WHERE swatch_id = ?", (swatch_id,))
    conn.commit()


def set_run_highlight(conn, run_id, swatch_id):
    conn.execute("UPDATE runs SET highlight_swatch_id = ? WHERE run_id = ?", (swatch_id, run_id))
    conn.commit()


def get_run_highlight_colors(conn):
    """{run_id: color} for every run that currently has a highlight swatch set -
    runs with none simply aren't in the dict, mirroring get_run_standards' shape."""
    rows = conn.execute(
        """
        SELECT r.run_id, hs.color AS color
        FROM runs r JOIN highlight_swatches hs ON hs.swatch_id = r.highlight_swatch_id
        """
    ).fetchall()
    return {row["run_id"]: row["color"] for row in rows}


def get_run_highlight_labels(conn):
    """{run_id: (color, label)} - like get_run_highlight_colors but also carries the
    swatch's label, for building a human-readable annotation (e.g. in copy/export)."""
    rows = conn.execute(
        """
        SELECT r.run_id, hs.color AS color, hs.label AS label
        FROM runs r JOIN highlight_swatches hs ON hs.swatch_id = r.highlight_swatch_id
        """
    ).fetchall()
    return {row["run_id"]: (row["color"], row["label"]) for row in rows}


# -- per-cell highlights (Analysis tab's Results table: one (run, gas) cell rather
# than a whole row) - same swatch palette as the row-level highlight above --------

def set_cell_highlight(conn, run_id, gas, swatch_id):
    if swatch_id is None:
        conn.execute("DELETE FROM cell_highlights WHERE run_id = ? AND gas = ?", (run_id, gas))
    else:
        conn.execute(
            """
            INSERT INTO cell_highlights (run_id, gas, swatch_id) VALUES (?, ?, ?)
            ON CONFLICT(run_id, gas) DO UPDATE SET swatch_id = excluded.swatch_id
            """,
            (run_id, gas, swatch_id),
        )
    conn.commit()


def get_cell_highlights_for_runs(conn, run_ids):
    """{(run_id, gas): (color, label)} for every cell-highlighted (run, gas) pair
    among the given run_ids."""
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"""
        SELECT ch.run_id, ch.gas, hs.color AS color, hs.label AS label
        FROM cell_highlights ch JOIN highlight_swatches hs ON hs.swatch_id = ch.swatch_id
        WHERE ch.run_id IN ({placeholders})
        """,
        run_ids,
    ).fetchall()
    return {(row["run_id"], row["gas"]): (row["color"], row["label"]) for row in rows}

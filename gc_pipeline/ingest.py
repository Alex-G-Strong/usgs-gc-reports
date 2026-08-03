"""Scan a folder of raw GC CSVs and load new/changed runs into the SQLite DB."""

import argparse
import datetime
import hashlib
from pathlib import Path

from gc_pipeline import db
from gc_pipeline.parser import parse_file


def _hash_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ingest(folder_path, db_path):
    conn = db.connect(db_path)
    folder = Path(folder_path)

    new_count = 0
    duplicate_count = 0
    skipped_count = 0
    error_count = 0
    ignored_count = 0
    new_run_ids = []

    # A ".sirslt" run folder with no exported CSV at all isn't a parse error (there's
    # nothing to try parsing) - it means the Agilent software never produced the peak
    # table for that injection. Left undetected, this reads as "the app is ignoring
    # my files" rather than what it actually is: an export step that still needs to
    # be run/fixed on the instrument side. See the Help tab.
    missing_export_count = 0
    missing_export_paths = []
    for sirslt_dir in sorted(folder.rglob("*.sirslt")):
        if sirslt_dir.is_dir() and not any(sirslt_dir.glob("*.csv")):
            missing_export_count += 1
            missing_export_paths.append(sirslt_dir.relative_to(folder).as_posix())

    for path in sorted(folder.rglob("*.csv")):
        source_file = path.relative_to(folder).as_posix()
        content_hash = _hash_file(path)

        if db.find_run_by_source_and_hash(conn, source_file, content_hash):
            skipped_count += 1
            continue

        if db.is_hash_ignored(conn, content_hash):
            # A human already deleted this exact content on purpose - don't let
            # re-ingesting the same folder quietly bring it back.
            ignored_count += 1
            continue

        try:
            parsed = parse_file(path)
        except Exception as exc:
            print(f"Skipping {source_file}: failed to parse ({exc})")
            error_count += 1
            continue

        fields = parsed["fields"]
        fields["source_file"] = source_file
        fields["content_hash"] = content_hash

        existing = db.find_run_by_hash(conn, content_hash)
        duplicate_of = existing["run_id"] if existing else None
        if duplicate_of is not None:
            duplicate_count += 1

        run_id = db.insert_run(
            conn,
            fields,
            parsed["peaks"],
            duplicate_of,
            datetime.datetime.now().isoformat(timespec="seconds"),
        )
        new_count += 1
        new_run_ids.append(run_id)

    if new_count:
        db.recompute_run_numbers(conn)

    # Detection only - never merges. A same-identity group (same sample_name +
    # injection_date, a strict superset of the content-hash duplicate check above,
    # since it also catches a reprocessed file whose bytes changed) is always
    # reported so the caller can decide what to do: gui.py either auto-merges it
    # (live watch + round-assign mode) or routes it to the Repair tool (everything
    # else) - see Phase 12 Part B. same_identity_groups only lists groups touched by
    # *this* ingest pass (i.e. containing at least one of new_run_ids), not every
    # group that happens to already exist in the database.
    same_identity_groups = []
    needs_repair_count = 0
    if new_run_ids:
        new_ids_set = set(new_run_ids)
        for group_run_ids in db.find_same_identity_groups(conn).values():
            touched = new_ids_set.intersection(group_run_ids)
            if touched:
                same_identity_groups.append(group_run_ids)
                needs_repair_count += len(touched)

    conn.commit()
    conn.close()
    return {
        "new": new_count,
        "duplicates": duplicate_count,
        "skipped": skipped_count,
        "errors": error_count,
        "ignored": ignored_count,
        "new_run_ids": new_run_ids,
        "needs_repair": needs_repair_count,
        "same_identity_groups": same_identity_groups,
        "missing_export": missing_export_count,
        "missing_export_paths": missing_export_paths,
    }


def main():
    parser = argparse.ArgumentParser(description="Ingest raw GC CSVs into the SQLite database.")
    parser.add_argument("folder", nargs="?", default="RAWs", help="Folder of raw CSVs (default: RAWs)")
    parser.add_argument("--db", dest="db_path", default="gc_data.sqlite3", help="Path to SQLite DB file")
    args = parser.parse_args()

    summary = ingest(args.folder, args.db_path)
    print(
        f"Ingested {summary['new']} new run(s) "
        f"({summary['duplicates']} flagged as duplicates, {summary['needs_repair']} need repair), "
        f"skipped {summary['skipped']} already-ingested file(s), "
        f"{summary['ignored']} previously-deleted file(s) skipped, "
        f"{summary['errors']} failed to parse, "
        f"{summary['missing_export']} run folder(s) missing their CSV export."
    )


if __name__ == "__main__":
    main()

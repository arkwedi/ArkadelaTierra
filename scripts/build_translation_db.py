#!/usr/bin/env python3
"""
build_translation_db.py

Reads every .json file in a folder (DQX Clarity-style JA->EN dialogue files,
where each file looks like: {"1938": {"ja text": "en text"}, ...}) and builds
a single SQLite database with one row per translation entry, ready to be
edited (ES column) and later exported back to JSON or imported into Supabase.

Usage:
    python build_translation_db.py /path/to/json_folder
    python build_translation_db.py /path/to/json_folder --output translations.db
    python build_translation_db.py /path/to/json_folder --overwrite

Schema (table: entries):
    id          INTEGER PRIMARY KEY AUTOINCREMENT
    file        TEXT      -- source filename, without .json extension
    entry_id    TEXT      -- the numeric key from the original JSON
    ja          TEXT      -- Japanese source text
    en          TEXT      -- English reference text
    es          TEXT      -- Spanish translation (empty until filled in)
    status      TEXT      -- 'pendiente' | 'traducido' | 'revisado'
    updated_by  TEXT      -- who last touched this row (null until edited)
    updated_at  TEXT      -- ISO timestamp of last edit (null until edited)

    UNIQUE(file, entry_id)  -- lets you re-run this script safely as a refresh
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file        TEXT NOT NULL,
    entry_id    TEXT NOT NULL,
    ja          TEXT NOT NULL,
    en          TEXT NOT NULL,
    es          TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pendiente',
    updated_by  TEXT,
    updated_at  TEXT,
    UNIQUE(file, entry_id)
);
"""


def iter_json_files(folder: Path):
    for path in sorted(folder.rglob("*.json")):
        yield path


def load_entries(path: Path):
    """Yield (entry_id, ja, en) tuples from one DQX-style JSON file.

    Expected shape: { "1938": { "ja text": "en text" }, ... }
    Skips and warns on malformed blocks instead of crashing the whole run.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"  [!] skipping {path.name}: could not parse ({e})", file=sys.stderr)
        return

    if not isinstance(data, dict):
        print(f"  [!] skipping {path.name}: top level is not an object", file=sys.stderr)
        return

    for entry_id, block in data.items():
        if not isinstance(block, dict) or not block:
            print(f"  [!] {path.name} #{entry_id}: unexpected shape, skipped", file=sys.stderr)
            continue
        # Each block is normally a single {ja: en} pair. Handle the rare
        # case of more than one pair defensively rather than dropping data.
        for ja, en in block.items():
            yield entry_id, ja, en


def build_database(json_folder: Path, db_path: Path, overwrite: bool):
    if db_path.exists():
        if overwrite:
            db_path.unlink()
        else:
            print(
                f"[!] {db_path} already exists. Use --overwrite to replace it, "
                f"or --output to pick a different path.",
                file=sys.stderr,
            )
            sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)

    total_rows = 0
    total_files = 0
    skipped_duplicates = 0

    for json_path in iter_json_files(json_folder):
        file_key = json_path.stem  # filename without .json
        rows = list(load_entries(json_path))
        if not rows:
            continue

        total_files += 1
        for entry_id, ja, en in rows:
            try:
                conn.execute(
                    "INSERT INTO entries (file, entry_id, ja, en) VALUES (?, ?, ?, ?)",
                    (file_key, entry_id, ja, en),
                )
                total_rows += 1
            except sqlite3.IntegrityError:
                # (file, entry_id) already present -- likely re-running the
                # script over the same folder. Don't overwrite existing rows
                # so in-progress ES translations are never lost.
                skipped_duplicates += 1

        #print(f"  {json_path.name}: {len(rows)} entries")

    conn.commit()
    conn.close()

    print()
    print(f"Done: {total_files} files processed, {total_rows} new rows inserted "
          f"into {db_path}")
    if skipped_duplicates:
        print(f"({skipped_duplicates} rows already existed and were left untouched)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_folder", type=Path, help="Folder containing the .json files")
    parser.add_argument(
        "--output", "-o", type=Path, default=Path("translations.db"),
        help="Path for the resulting SQLite database (default: translations.db)",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Delete and rebuild the database from scratch instead of merging into it",
    )
    args = parser.parse_args()

    if not args.json_folder.is_dir():
        print(f"[!] {args.json_folder} is not a folder", file=sys.stderr)
        sys.exit(1)

    build_database(args.json_folder, args.output, args.overwrite)


if __name__ == "__main__":
    main()

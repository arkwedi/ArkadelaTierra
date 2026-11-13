#!/usr/bin/env python3
"""
export_translations.py

Pulls the current state of the `entries` table from Supabase and rebuilds
the original DQX JSON files, choosing which language goes in the "value"
side of each {ja_text: value_text} pair.

Since the Spanish translation is a work in progress, --lang es applies a
fallback chain so you never ship a blank line to the in-game dialogue box:
    es (if filled)  ->  en (if filled)  ->  ja (last resort)

OPTIONAL: this can also push your ES translations directly into Clarity's
LOCAL glossary/dialogue SQLite databases, matching by exact JA text --
this is what makes translations show up in the dynamic overworld/battle
text (DeepL-driven), not just the static ETP files. Two different update
strategies depending on the table's schema:

  - glossary.db (table: glossary) and clarity_dialog.db (tables:
    m00_strings, glossary) only have ja/en columns -- there's no dedicated
    slot for Spanish, so the only option is to OVERWRITE the en column.
  - clarity_dialog.db (tables: dialog, quests, story_so_far, walkthrough)
    already ship with a dedicated `es` column (part of a full DeepL
    target-language column set: bg, cs, da, de, ..., es, ..., zh) that is
    currently empty. We fill THAT column and leave en untouched -- the
    non-destructive option, and probably what those columns are there for.

Only rows with a REAL (non-fallback) ES translation are pushed into these
local DBs -- fallback EN/JA values are never written into `es` columns or
used to overwrite `en`, since that would misrepresent untranslated lines
as translated.

Usage:
    export DATABASE_URL="postgresql://...supabase connection string..."

    # one file (game JSON only, same as before)
    python export_translations.py --lang es --file eventTextCsW11Client --output out/

    # every file
    python export_translations.py --lang es --all --output out/

    # no fallback -- only rows that actually have an ES translation get one
    python export_translations.py --lang es --all --output out/ --no-fallback

    # ALSO push ES into Clarity's local DBs after exporting the JSON:
    python export_translations.py --lang es --all --output out/ \\
        --clarity-glossary-db "C:\\path\\to\\glossary.db" \\
        --clarity-dialog-db "C:\\path\\to\\clarity_dialog.db"

    # Build glossary.db/clarity_dialog.db from scratch, in a SEPARATE
    # folder from the JSON (recommended -- keeps the .db files out of the
    # tree that etp.exe rebuild reads):
    python export_translations.py --lang es --all --output json/_lang/en \\
        --build-clarity-dbs --clarity-db-output clarity_dbs/
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

try:
    import psycopg2
except ImportError:
    print(
        "[!] psycopg2 is not installed. Run:\n"
        "    pip install psycopg2-binary --break-system-packages",
        file=sys.stderr,
    )
    sys.exit(1)

# Rows that sync_json_updates.py had to "park" because their entry_id was needed
# by another line (they still hold a translation, so they're kept, not deleted).
# They don't exist in the game's current data, so they must never be exported.
# Keep in sync with PARKED_PREFIX in sync_json_updates.py.
PARKED_PREFIX = "__stale__"

# Tables that only have ja/en -- no dedicated Spanish slot, so we overwrite en.
# Format: (db_arg_name, table)
OVERWRITE_EN_TARGETS = [
    ("glossary_db", "glossary"),
    ("dialog_db", "m00_strings"),
    ("dialog_db", "glossary"),
]

# Tables that already have a dedicated `es` column -- fill it, leave en alone.
FILL_ES_COLUMN_TARGETS = [
    ("dialog_db", "dialog"),
    ("dialog_db", "quests"),
    ("dialog_db", "story_so_far"),
    ("dialog_db", "walkthrough"),
]

GLOSSARY_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS "glossary" (
    "ja"    TEXT,
    "en"    TEXT,
    PRIMARY KEY("ja")
);
CREATE UNIQUE INDEX IF NOT EXISTS "glossary_index" ON "glossary" ("ja");
"""

# Verbatim from Clarity's own schema.sql (the file that builds clarity_dialog.db).
CLARITY_DIALOG_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS "dialog" (
    "ja"    TEXT NOT NULL UNIQUE,
    "npc_name"  TEXT,
    "en"    TEXT,
    PRIMARY KEY("ja")
);

CREATE TABLE IF NOT EXISTS "fixed_dialog_template" (
    "ja"    TEXT NOT NULL UNIQUE,
    "en"    TEXT,
    "bad_string"    INTEGER,
    PRIMARY KEY("ja")
);

CREATE TABLE IF NOT EXISTS "bad_strings" (
    "ja"    TEXT NOT NULL UNIQUE,
    "en"    TEXT,
    PRIMARY KEY("ja")
);

CREATE TABLE IF NOT EXISTS "player" (
    "type"  TEXT NOT NULL,
    "name"  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS "quests" (
    "ja"    TEXT NOT NULL UNIQUE,
    "en"    TEXT,
    PRIMARY KEY("ja")
);

CREATE TABLE IF NOT EXISTS "story_so_far" (
    "ja"    TEXT NOT NULL UNIQUE,
    "en"    TEXT
);

CREATE TABLE IF NOT EXISTS "story_so_far_template" (
    "ja"    TEXT NOT NULL UNIQUE,
    "en"    TEXT
);

CREATE TABLE IF NOT EXISTS "walkthrough" (
    "ja"    TEXT NOT NULL UNIQUE,
    "en"    TEXT,
    PRIMARY KEY("ja")
);

CREATE TABLE IF NOT EXISTS "m00_strings" (
    "ja"    TEXT NOT NULL,
    "en"    TEXT,
    "file"  TEXT
);

CREATE TABLE IF NOT EXISTS "glossary" (
    "ja"    TEXT,
    "en"    TEXT,
    PRIMARY KEY("ja")
);

CREATE UNIQUE INDEX IF NOT EXISTS "dialog_index" ON "dialog" ("ja");
CREATE UNIQUE INDEX IF NOT EXISTS "quests_index" ON "quests" ("ja");
CREATE UNIQUE INDEX IF NOT EXISTS "story_so_far_index" ON "story_so_far" ("ja");
CREATE UNIQUE INDEX IF NOT EXISTS "walkthrough_index" ON "walkthrough" ("ja");
CREATE INDEX IF NOT EXISTS "m00_strings_index" ON "m00_strings" ("ja");
CREATE UNIQUE INDEX IF NOT EXISTS "glossary_index" ON "glossary" ("ja");
CREATE UNIQUE INDEX IF NOT EXISTS "bad_strings_index" ON "bad_strings" ("ja");
"""

DEFAULT_GLOSSARY_MAX_LEN = 60  # fallback safety net only -- see CLARITY_JSON_CATEGORY_MAP below,
                                 # which is the real, exact way we now categorize rows.

# Straight from Clarity's own common/constants.py: these six source files are
# term/name lists (monsters, npcs, items, key items, quest titles, cutscene
# names), and get tagged into m00_strings/glossary with these exact category
# names. Everything else is treated as regular dialogue -> only `dialog`.
CLARITY_JSON_CATEGORY_MAP = {
    "subPackage02Client": "monsters",
    "smldt_msg_pkg_NPC_DB": "npcs",
    "subPackage05Client": "items",
    "subPackage41Client": "key_items",
    "eventTextSysQuestaClient": "quests",
    "eventTextSysEventaClient": "story_names",
}


def resolve_value(row, lang, use_fallback):
    """row = (file, entry_id, ja, en, es). Returns the text to use as the
    JSON value for this entry, given the requested target language."""
    ja, en, es = row[2], row[3], row[4]

    if lang == "ja":
        return ja
    if lang == "en":
        return en
    if lang == "es":
        if not use_fallback:
            return es
        if es:
            return es
        if en:
            return en
        return ja  # last resort: better than an empty dialogue line

    raise ValueError(f"Unknown lang: {lang}")


def fetch_rows(conn, file_filter=None):
    sql = "SELECT file, entry_id, ja, en, es FROM entries WHERE left(entry_id, %s) <> %s"
    params = (len(PARKED_PREFIX), PARKED_PREFIX)
    if file_filter:
        sql += " AND file = %s"
        params += (file_filter,)
    sql += " ORDER BY file, entry_id"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for row in batch:
                yield row


def overwrite_en_column(sqlite_path, table, ja_to_es):
    """For tables with only ja/en: overwrite en with the Spanish text for
    every row whose ja matches a real Supabase translation."""
    conn = sqlite3.connect(sqlite_path)
    cur = conn.execute(f"SELECT rowid, ja FROM {table}")
    updates = [(ja_to_es[ja], rowid) for rowid, ja in cur.fetchall() if ja in ja_to_es]

    if updates:
        conn.executemany(f"UPDATE {table} SET en = ? WHERE rowid = ?", updates)
        conn.commit()
    conn.close()
    return len(updates)


def fill_es_column(sqlite_path, table, ja_to_es):
    """For tables with a dedicated es column: fill es, leave en untouched."""
    conn = sqlite3.connect(sqlite_path)
    cur = conn.execute(f"SELECT rowid, ja FROM {table}")
    updates = [(ja_to_es[ja], rowid) for rowid, ja in cur.fetchall() if ja in ja_to_es]

    if updates:
        conn.executemany(f"UPDATE {table} SET es = ? WHERE rowid = ?", updates)
        conn.commit()
    conn.close()
    return len(updates)


def apply_to_clarity_dbs(ja_to_es, glossary_db, dialog_db):
    db_paths = {"glossary_db": glossary_db, "dialog_db": dialog_db}

    print(f"\nAplicando {len(ja_to_es)} traducciones ES a las bases de datos locales de Clarity...")

    for db_key, table in OVERWRITE_EN_TARGETS:
        path = db_paths[db_key]
        if not path:
            continue
        count = overwrite_en_column(path, table, ja_to_es)
        print(f"  {os.path.basename(path)} / {table} (overwrite en): {count} filas actualizadas")

    for db_key, table in FILL_ES_COLUMN_TARGETS:
        path = db_paths[db_key]
        if not path:
            continue
        count = fill_es_column(path, table, ja_to_es)
        print(f"  {os.path.basename(path)} / {table} (fill es): {count} filas actualizadas")


def build_clarity_dbs(entries_rows, output_dir, glossary_max_len=DEFAULT_GLOSSARY_MAX_LEN):
    """Builds glossary.db and clarity_dialog.db FROM SCRATCH using Clarity's
    own schema -- no need to have Clarity's GitHub-sourced seed files at
    all. entries_rows = list of (file, ja, value) using the resolved
    (es -> en -> ja fallback) value, one per Supabase row.

    Rows are routed using CLARITY_JSON_CATEGORY_MAP: the six known
    name/term source files go into glossary/m00_strings (tagged with
    Clarity's own category name), everything else is regular dialogue and
    goes into `dialog` only."""
    glossary_path = os.path.join(output_dir, "glossary.db")
    dialog_path = os.path.join(output_dir, "clarity_dialog.db")

    for path in (glossary_path, dialog_path):
        if os.path.exists(path):
            os.remove(path)

    term_rows = []      # (ja, value, category) -- goes to glossary/m00_strings
    dialogue_rows = {}  # ja -> value, deduped -- goes to dialog only
    long_terms_warned = 0

    for file_key, ja, value in entries_rows:
        category = CLARITY_JSON_CATEGORY_MAP.get(file_key)
        if category:
            term_rows.append((ja, value, category))
            if len(ja) > glossary_max_len:
                long_terms_warned += 1
        else:
            dialogue_rows[ja] = value

    # dedupe term_rows by ja for the ja-primary-key glossary tables (last wins)
    dedup_terms = {ja: (value, category) for ja, value, category in term_rows}

    print(f"\nConstruyendo bases de datos de Clarity desde cero en '{output_dir}'...")
    print(f"  {len(dedup_terms)} terminos (monsters/npcs/items/key_items/quests/story_names), "
          f"{len(dialogue_rows)} lineas de dialogo")
    if long_terms_warned:
        print(f"  [!] Aviso: {long_terms_warned} 'terminos' superan los {glossary_max_len} caracteres "
              f"-- revisa si de verdad son nombres/titulos cortos.")

    # --- glossary.db ---
    conn = sqlite3.connect(glossary_path)
    conn.executescript(GLOSSARY_DB_SCHEMA)
    conn.executemany(
        "INSERT OR REPLACE INTO glossary (ja, en) VALUES (?, ?)",
        [(ja, value) for ja, (value, category) in dedup_terms.items()],
    )
    conn.commit()
    conn.close()
    print(f"  glossary.db / glossary: {len(dedup_terms)} filas")

    # --- clarity_dialog.db ---
    conn = sqlite3.connect(dialog_path)
    conn.executescript(CLARITY_DIALOG_DB_SCHEMA)

    conn.executemany(
        "INSERT OR REPLACE INTO glossary (ja, en) VALUES (?, ?)",
        [(ja, value) for ja, (value, category) in dedup_terms.items()],
    )
    print(f"  clarity_dialog.db / glossary: {len(dedup_terms)} filas")

    m00_rows = [(ja, value, category) for ja, (value, category) in dedup_terms.items()]
    conn.executemany("INSERT INTO m00_strings (ja, en, file) VALUES (?, ?, ?)", m00_rows)
    print(f"  clarity_dialog.db / m00_strings: {len(m00_rows)} filas (tagged by category, e.g. 'monsters')")

    # dialog: narrative dialogue only, excludes the six term/name source
    # files. Per the schema.sql you provided, this table only has
    # ja/npc_name/en (no es column -- that must come from a migration
    # elsewhere in Clarity's codebase, not this creation script), so we
    # write the resolved value into en directly.
    dialog_rows = list(dialogue_rows.items())
    conn.executemany("INSERT OR REPLACE INTO dialog (ja, en) VALUES (?, ?)", dialog_rows)
    print(f"  clarity_dialog.db / dialog: {len(dialog_rows)} filas")

    # quests, story_so_far, walkthrough, fixed_dialog_template, bad_strings,
    # player: left empty (schema created, no rows) -- see conversation notes
    # on why these are skipped for now. Note: the 'quests' TABLE here is a
    # different, manually-curated thing from the 'quests' CATEGORY tagged
    # into m00_strings above -- they're unrelated despite the shared name.

    conn.commit()
    conn.close()

    print(f"\nListo: glossary.db y clarity_dialog.db creadas en '{output_dir}'.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", choices=["ja", "en", "es"], required=True,
                         help="Which language goes on the value side of each pair")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="Export only this file (without .json)")
    group.add_argument("--all", action="store_true", help="Export every file")
    parser.add_argument("--output", "-o", default="export", help="Output folder (default: export)")
    parser.add_argument("--no-fallback", action="store_true",
                         help="For --lang es, disable the es->en->ja fallback and leave empty ES as empty")
    parser.add_argument("--clarity-glossary-db", dest="glossary_db", default=None,
                         help="Path to Clarity's local glossary.db -- if given, also updates it with real ES translations")
    parser.add_argument("--clarity-dialog-db", dest="dialog_db", default=None,
                         help="Path to Clarity's local clarity_dialog.db -- if given, also updates it with real ES translations")
    parser.add_argument("--build-clarity-dbs", action="store_true",
                         help="Build glossary.db and clarity_dialog.db FROM SCRATCH (no existing Clarity install "
                              "needed), using es->en->ja fallback for every row")
    parser.add_argument("--clarity-db-output", default=None,
                         help="Where to write glossary.db/clarity_dialog.db. Defaults to --output, but you almost "
                              "always want this to be a SEPARATE folder -- if --output is the same folder that "
                              "etp.exe rebuild later reads (e.g. json\\_lang\\en), these two .db files end up "
                              "sitting inside your translation source tree, which is not where they belong.")
    parser.add_argument("--glossary-max-len", type=int, default=DEFAULT_GLOSSARY_MAX_LEN,
                         help=f"Max ja character length to count as a 'term' for glossary/m00_strings "
                              f"(default: {DEFAULT_GLOSSARY_MAX_LEN})")
    args = parser.parse_args()

    if (args.glossary_db or args.dialog_db) and args.lang != "es":
        print("[!] --clarity-glossary-db/--clarity-dialog-db only make sense with --lang es", file=sys.stderr)
        sys.exit(1)
    if args.build_clarity_dbs and args.lang != "es":
        print("[!] --build-clarity-dbs only makes sense with --lang es", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[!] DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output, exist_ok=True)
    use_fallback = not args.no_fallback

    conn = psycopg2.connect(db_url)
    file_filter = args.file if args.file else None

    files_written = defaultdict(dict)
    ja_to_es = {}         # only REAL (non-fallback) es translations -- used by --clarity-*-db (update-in-place mode)
    entries_rows = []     # (file, ja, resolved_value) for every row -- used by --build-clarity-dbs
    fallback_used = 0
    total = 0

    for row in fetch_rows(conn, file_filter):
        file_key, entry_id, ja, en, es = row
        value = resolve_value(row, args.lang, use_fallback)
        total += 1
        if args.lang == "es" and use_fallback and not es and value:
            fallback_used += 1
        if args.lang == "es" and es:
            ja_to_es[ja] = es
        if args.lang == "es" and args.build_clarity_dbs:
            entries_rows.append((file_key, ja, value))

        # Original shape: {"1938": {"ja text": "value text"}}
        files_written[file_key][entry_id] = {ja: value}

    conn.close()

    if not files_written:
        print(f"[!] No rows found" + (f" for file '{args.file}'" if args.file else ""), file=sys.stderr)
        sys.exit(1)

    for file_key, entries in files_written.items():
        out_path = os.path.join(args.output, f"{file_key}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)

    print(f"Done: {len(files_written)} file(s), {total} entries written to '{args.output}'")
    if args.lang == "es" and use_fallback:
        print(f"({fallback_used} entries used the EN/JA fallback because ES was still empty)")

    if args.glossary_db or args.dialog_db:
        apply_to_clarity_dbs(ja_to_es, args.glossary_db, args.dialog_db)

    if args.build_clarity_dbs:
        clarity_db_output = args.clarity_db_output or args.output
        os.makedirs(clarity_db_output, exist_ok=True)
        build_clarity_dbs(entries_rows, clarity_db_output, args.glossary_max_len)


if __name__ == "__main__":
    main()

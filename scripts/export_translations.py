#!/usr/bin/env python3
"""
export_translations.py

Pulls the current state of the `entries` table from Supabase and rebuilds
the original DQX JSON files, choosing which language goes in the "value"
side of each {ja_text: value_text} pair.

Since the Spanish translation is a work in progress, --lang es applies a
fallback chain so you never ship a blank line to the in-game dialogue box:
    es (if filled)  ->  en (if filled)  ->  ja (last resort)

CUSTOM FILES (custom_*): server-side text that is NOT part of the game's ETP
files. They are never written to --output (etp.exe rebuild would pick them up
and bloat the ETP). With --lang es they have NO fallback: an entry without ES
is simply omitted, so Clarity machine-translates it instead of showing English.
They go to --clarity-pack-dir (see below) or to --custom-output.

CLARITY PACK: with --clarity-pack-dir DIR it also writes DIR/dqxclarity/, the
folder that lives inside the language pack (.clpk):
    dqxclarity/json/<six name files>
    dqxclarity/custom-translations/json/custom_*.json
    dqxclarity/custom-translations/csv/glossary.csv   (generated here)
    dqxclarity/custom-translations/csv/merge.xlsx     (from --merge-xlsx)
Everything uses the same resolved values (and --clean-encoding) as the main export,
so each package variant (vanilla / experimental) gets its own consistent set.

Usage:
    export DATABASE_URL="postgresql://...supabase connection string..."

    # one file
    python export_translations.py --lang es --file eventTextCsW11Client --output out/

    # every game file (custom_* are excluded from --output)
    python export_translations.py --lang es --all --output out/

    # + the dqxclarity/ folder for the language pack
    python export_translations.py --lang es --all --output json/_lang/en \\
        --clarity-pack-dir staging/exp --merge-xlsx sources/merge.xlsx \\
        --ignore-file sources/ignore.py

    # vanilla variant (restricted characters cleaned everywhere, merge.xlsx included)
    python export_translations.py --lang es --all --output json/_lang/en --clean-encoding \\
        --clarity-pack-dir staging/vanilla --merge-xlsx sources/merge.xlsx --ignore-file sources/ignore.py
"""

import argparse
import json
import os
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clarity_pack  # noqa: E402

# Rows that sync_json_updates.py had to "park" because their entry_id was needed
# by another line (they still hold a translation, so they're kept, not deleted).
# They don't exist in the game's current data, so they must never be exported.
# Keep in sync with PARKED_PREFIX in sync_json_updates.py.
PARKED_PREFIX = "__stale__"

# Diccionario de reemplazo para limpieza de hardware con restricción de encoding
REPLACE_MAP = {
    # Minúsculas acentuadas, con diéresis, tildes y cedilla
    'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
    'à': 'a', 'è': 'e', 'ì': 'i', 'ò': 'o', 'ù': 'u',
    'â': 'a', 'ê': 'e', 'î': 'i', 'ô': 'o', 'û': 'u',
    'ä': 'a', 'ë': 'e', 'ï': 'i', 'ö': 'o', 'ü': 'u', 'ÿ': 'y',
    'ã': 'a', 'õ': 'o', 'ç': 'c',
    
    # Mayúsculas acentuadas, con diéresis, tildes y cedilla
    'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ú': 'U',
    'À': 'A', 'È': 'E', 'Ì': 'I', 'Ò': 'O', 'Ù': 'U',
    'Â': 'A', 'Ê': 'E', 'Î': 'I', 'Ô': 'O', 'Û': 'U',
    'Ä': 'A', 'Ë': 'E', 'Ï': 'I', 'Ö': 'O', 'Ü': 'U',
    'Ã': 'A', 'Õ': 'O', 'Ç': 'C',

    # Casos particulares: Letra Ñ / ñ
    'ñ': 'ni', 'Ñ': 'NI',

    # Símbolos de apertura/interrogación/exclamación y ordinales
    '¿': '', '¡': '', 'º': '', 'ª': ''
}

def clean_restricted_text(text):
    if not text:
        return text
    for char, replacement in REPLACE_MAP.items():
        text = text.replace(char, replacement)
    return text

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


def write_entries_json(folder, files):
    """{file: {entry_id: {ja: value}}} -> <folder>/<file>.json (same layout as always)."""
    os.makedirs(folder, exist_ok=True)
    for file_key, entries in files.items():
        with open(os.path.join(folder, f"{file_key}.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", choices=["ja", "en", "es"], required=True,
                         help="Which language goes on the value side of each pair")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="Export only this file (without .json)")
    group.add_argument("--all", action="store_true", help="Export every file")
    parser.add_argument("--output", "-o", default="export", help="Output folder for the game files (default: export)")
    parser.add_argument("--no-fallback", action="store_true",
                         help="For --lang es, disable the es->en->ja fallback and leave empty ES as empty")
    parser.add_argument("--clean-encoding", action="store_true",
                         help="Aplica REPLACE_MAP para restringir caracteres compatibles con el juego")
    parser.add_argument("--custom-prefix", default=clarity_pack.CUSTOM_PREFIX,
                         help=f"Files starting with this are server-side custom text (default: {clarity_pack.CUSTOM_PREFIX})")
    parser.add_argument("--custom-output", default=None,
                         help="Write the custom_* JSON here (when not using --clarity-pack-dir)")
    parser.add_argument("--clarity-pack-dir", default=None,
                         help="Builds <dir>/dqxclarity/ (six name files, custom_*, glossary.csv, merge.xlsx)")
    parser.add_argument("--merge-xlsx", default=None,
                         help="merge.xlsx to include in the pack (an empty template is created if omitted)")
    parser.add_argument("--ignore-file", default=None,
                         help="upstream's ignore.py (IGNORE = {...}): keys excluded from the glossary")
    parser.add_argument("--glossary-overrides", default=None,
                         help="CSV with ja,es columns: terms forced into the glossary")
    args = parser.parse_args()

    if args.clarity_pack_dir and args.lang != "es":
        print("[!] --clarity-pack-dir only makes sense with --lang es", file=sys.stderr)
        sys.exit(1)
    if args.file and args.file.startswith(args.custom_prefix) and not (args.clarity_pack_dir or args.custom_output):
        print(f"[!] '{args.file}' is a custom file: pass --custom-output or --clarity-pack-dir.", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[!] DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    use_fallback = not args.no_fallback
    conn = psycopg2.connect(db_url)
    file_filter = args.file if args.file else None

    files_written = defaultdict(dict)    # game files -> --output
    custom_written = defaultdict(dict)   # custom_* -> pack / --custom-output
    fallback_used = 0
    custom_skipped = 0
    total = 0

    for row in fetch_rows(conn, file_filter):
        file_key, entry_id, ja, en, es = row
        is_custom = file_key.startswith(args.custom_prefix)

        if is_custom and args.lang == "es":
            if not es:                   # no fallback: Clarity machine-translates what is missing
                custom_skipped += 1
                continue
            value = es
        else:
            value = resolve_value(row, args.lang, use_fallback)
            if args.lang == "es" and use_fallback and not es and value and not is_custom:
                fallback_used += 1
        if args.clean_encoding:
            value = clean_restricted_text(value)
        total += 1

        # Original shape: {"1938": {"ja text": "value text"}}
        target = custom_written if is_custom else files_written
        target[file_key][entry_id] = {ja: value}

    conn.close()

    if not files_written and not custom_written:
        print(f"[!] No rows found" + (f" for file '{args.file}'" if args.file else ""), file=sys.stderr)
        sys.exit(1)

    write_entries_json(args.output, files_written)
    n_entries = sum(len(v) for v in files_written.values())
    print(f"Done: {len(files_written)} game file(s), {n_entries} entries written to '{args.output}'")
    if args.lang == "es" and use_fallback:
        print(f"({fallback_used} entries used the EN/JA fallback because ES was still empty)")

    n_custom = sum(len(v) for v in custom_written.values())
    if custom_written or custom_skipped:
        print(f"Custom: {len(custom_written)} file(s), {n_custom} entries with ES"
              + (f" ({custom_skipped} without ES omitted)" if custom_skipped else ""))

    if args.clarity_pack_dir:
        clean = clean_restricted_text if args.clean_encoding else None
        summary = clarity_pack.write_pack(
            args.clarity_pack_dir, files_written, custom_written,
            merge_xlsx=args.merge_xlsx, ignore_file=args.ignore_file,
            overrides_file=args.glossary_overrides, clean=clean)
        g = summary["glossary"]
        print(f"Pack dqxclarity/ -> {summary['root']}")
        print(f"  json/: {summary['name_files']}/6 name files"
              + (f"  [!] missing: {', '.join(summary['missing_name_files'])}" if summary["missing_name_files"] else ""))
        print(f"  custom-translations/json: {summary['custom_files']} file(s)")
        print(f"  csv/glossary.csv: {g['terms']} terms ({g['untranslated']} untranslated skipped)"
              + (f"  [!] missing sources: {', '.join(g['missing_sources'])}" if g["missing_sources"] else ""))
        if g["comma_keys"]:
            print(f"  [!] {len(g['comma_keys'])} ja keys with a comma skipped (would break the CSV)")
        if summary["merge"] == "template":
            print("  [!] csv/merge.xlsx: no --merge-xlsx given, empty template created")
        else:
            print(f"  csv/merge.xlsx: {summary['merge']}")
    elif custom_written:
        if args.custom_output:
            n = clarity_pack.write_custom_json(args.custom_output, custom_written)
            print(f"Custom JSON ({n} file(s)) written to '{args.custom_output}'")
        else:
            print("[i] custom_* rows were skipped (no --clarity-pack-dir / --custom-output given).")


if __name__ == "__main__":
    main()

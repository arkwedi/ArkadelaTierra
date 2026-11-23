#!/usr/bin/env python3
"""
build_clpk.py

Builds a .clpk language pack directly from a zip file, replicating the
exact binary format Clarity's own "Build language pack (CLPK)" GUI tool
produces. Reverse-engineered from a real .clpk file -- no Clarity source
code involved.

Format (confirmed by parsing a real .clpk byte-for-byte):
    bytes 0-3   : magic b"CLPK"
    byte  4     : version, 1 byte (currently 1)
    bytes 5-6   : length N of the metadata JSON, uint16 BIG-endian (0-65535)
    next N bytes: metadata JSON (UTF-8)
                  {"sha": <sha256 hex of the zip payload>, "language": "...",
                   "author": "...", "builtAt": <unix timestamp>, "download_url": "...",
                   optional (only written when set, same as dqxclarity's own build-clpk.yml):
                   "disable_text_normalization": true, "max_chars_per_line": 10-200,
                   "max_lines_per_box": 1-20, "custom_translations_url": "https://github.com/<org>/<repo>"}
    rest of file: the original .zip, byte-for-byte, unmodified

Usage:
    python build_clpk.py common.zip --output espanol.clpk \\
        --author "ArkadelaTierra" --language es \\
        --download-url "https://github.com/arkwedi/dqx-hispano/releases/download/nightly/common.zip"

    --max-chars-per-line N / --max-lines-per-box N / --custom-translations-url URL
                                  optional header fields, validated with dqxclarity's ranges.
    --disable-text-normalization  adds "disable_text_normalization": true (experimental pack, which ships the
                                  plugin that draws accents). Without the flag the key is omitted, which
                                  Clarity treats as false (vanilla pack)."""

import argparse
import hashlib
import json
import struct
import sys
import time
import zipfile
from pathlib import Path

MAGIC = b"CLPK"
VERSION = 1
MAX_METADATA = 0xFFFF


def build_clpk(zip_path: Path, output_path: Path, author: str, language: str, download_url: str,
               disable_text_normalization: bool = False, max_chars_per_line=None,
               max_lines_per_box=None, custom_translations_url=""):
    zip_data = zip_path.read_bytes()

    # Sanity check: make sure this is actually a valid zip before wrapping it --
    # a corrupt/incomplete zip would otherwise silently become a corrupt .clpk.
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad_file = zf.testzip()
            if bad_file:
                print(f"[!] {zip_path} parece corrupto (falla en: {bad_file})", file=sys.stderr)
                sys.exit(1)
            entry_count = len(zf.namelist())
    except zipfile.BadZipFile:
        print(f"[!] {zip_path} no es un zip valido.", file=sys.stderr)
        sys.exit(1)

    sha = hashlib.sha256(zip_data).hexdigest()
    metadata = {
        "sha": sha,
        "language": language,
        "author": author,
        "builtAt": int(time.time()),
        "download_url": download_url,
    }
    if disable_text_normalization:
        metadata["disable_text_normalization"] = True
    if max_chars_per_line is not None:
        if not 10 <= max_chars_per_line <= 200:
            sys.exit("[!] --max-chars-per-line debe estar entre 10 y 200")
        metadata["max_chars_per_line"] = max_chars_per_line
    if max_lines_per_box is not None:
        if not 1 <= max_lines_per_box <= 20:
            sys.exit("[!] --max-lines-per-box debe estar entre 1 y 20")
        metadata["max_lines_per_box"] = max_lines_per_box
    if custom_translations_url and custom_translations_url.strip():
        metadata["custom_translations_url"] = custom_translations_url.strip()

    def encode(m):
        return json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    metadata_bytes = encode(metadata)

    if len(metadata_bytes) > MAX_METADATA:
        print(f"[!] El JSON de metadata mide {len(metadata_bytes)} bytes (max {MAX_METADATA}).", file=sys.stderr)
        sys.exit(1)

    with open(output_path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<B", VERSION))
        f.write(struct.pack(">H", len(metadata_bytes)))
        f.write(metadata_bytes)
        f.write(zip_data)

    print(f"CLPK creado: {output_path}")
    print(f"  zip origen: {zip_path} ({entry_count} entradas, {len(zip_data):,} bytes)")
    print(f"  sha256: {sha}")
    print(f"  metadata: {metadata_bytes.decode('utf-8')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("zip_file", type=Path, help="Path to common.zip")
    parser.add_argument("--output", "-o", type=Path, required=True, help="Output .clpk path")
    parser.add_argument("--author", required=True, help="Author name shown in the language pack list")
    parser.add_argument("--language", required=True, help="Language code, e.g. es")
    parser.add_argument("--download-url", required=True,
                         help="URL Clarity will poll for updates (e.g. your nightly Release asset URL)")
    parser.add_argument("--disable-text-normalization", action="store_true",
                         help='Agrega "disable_text_normalization": true a la metadata (paquete experimental con plugin)')
    parser.add_argument("--max-chars-per-line", type=int, default=None, help="Caracteres por linea de dialogo (10-200; default de Clarity 46)")
    parser.add_argument("--max-lines-per-box", type=int, default=None, help="Lineas por caja de dialogo (1-20; default de Clarity 3)")
    parser.add_argument("--custom-translations-url", default="", help="Repo de GitHub alternativo a dqx-custom-translations (https://github.com/<org>/<repo>)")
    args = parser.parse_args()

    if not args.zip_file.exists():
        print(f"[!] {args.zip_file} no existe", file=sys.stderr)
        sys.exit(1)

    build_clpk(args.zip_file, args.output, args.author, args.language, args.download_url,
               args.disable_text_normalization, args.max_chars_per_line, args.max_lines_per_box,
               args.custom_translations_url)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
build_clpk.py

Builds a .clpk language pack directly from a zip file, replicating the
exact binary format Clarity's own "Build language pack (CLPK)" GUI tool
produces. Reverse-engineered from a real .clpk file -- no Clarity source
code involved.

Format (confirmed by parsing a real .clpk byte-for-byte):
    bytes 0-3   : magic b"CLPK"
    bytes 4-5   : version, uint16 little-endian (currently 1)
    byte  6     : length of the metadata JSON, as a single byte (0-255)
    next N bytes: metadata JSON (UTF-8), where N = the byte above
                  {"sha": <sha256 hex of the zip payload>,
                   "author": "...", "language": "...",
                   "builtAt": <unix timestamp>, "download_url": "...",
                   optional: "disable_text_normalization": true}
    rest of file: the original .zip, byte-for-byte, unmodified

Usage:
    python build_clpk.py common.zip --output espanol.clpk \\
        --author "ArkadelaTierra" --language es \\
        --download-url "https://github.com/arkwedi/dqx-hispano/releases/download/nightly/common.zip"

    --disable-text-normalization  adds "disable_text_normalization": true (experimental pack, which ships the
                                  plugin that draws accents). Without the flag the key is omitted, which
                                  Clarity treats as false (vanilla pack).
    --allow-drop-url              if the metadata exceeds 255 bytes, drops download_url (with a warning)
                                  instead of failing.
"""

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


def build_clpk(zip_path: Path, output_path: Path, author: str, language: str, download_url: str,
               disable_text_normalization: bool = False, allow_drop_url: bool = False):
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
        "author": author,
        "language": language,
        "builtAt": int(time.time()),
        "download_url": download_url,
    }
    if disable_text_normalization:
        metadata["disable_text_normalization"] = True

    def encode(m):
        return json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    metadata_bytes = encode(metadata)

    if len(metadata_bytes) > 255 and allow_drop_url and metadata.get("download_url"):
        print(f"[!] AVISO: la metadata mide {len(metadata_bytes)} bytes (max 255); se omite download_url "
              f"({len(download_url)} caracteres). Clarity no podra actualizar este paquete por URL.", file=sys.stderr)
        metadata["download_url"] = ""
        metadata_bytes = encode(metadata)

    if len(metadata_bytes) > 255:
        print(
            f"[!] El JSON de metadata mide {len(metadata_bytes)} bytes, pero el formato solo "
            f"reserva 1 byte (max 255) para su longitud. Acorta --author/--download-url (o usa --allow-drop-url).",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(output_path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<H", VERSION))
        f.write(struct.pack("<B", len(metadata_bytes)))
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
    parser.add_argument("--allow-drop-url", action="store_true",
                         help="Si la metadata supera 255 bytes, omite download_url con un aviso en vez de fallar")
    args = parser.parse_args()

    if not args.zip_file.exists():
        print(f"[!] {args.zip_file} no existe", file=sys.stderr)
        sys.exit(1)

    build_clpk(args.zip_file, args.output, args.author, args.language, args.download_url,
               args.disable_text_normalization, args.allow_drop_url)


if __name__ == "__main__":
    main()

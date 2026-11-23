#!/usr/bin/env python3
"""Descarga las fuentes externas del pack dqxclarity (con copia de la ultima version buena).

  1. dqx-custom-translations (zip de GitHub): json/custom_*.json y generate_glossary/ignore.py
  2. merge.xlsx desde un Google Sheet publico (URL de exportacion .xlsx)

Cada fuente se valida; si falla, se usa la ultima copia buena de --cache-dir. Si tampoco hay copia,
la fuente queda ausente (el pipeline sigue; con --strict termina con error).

Salida (--out-dir):
  custom_json/custom_*.json   merge.xlsx (si hay)   ignore.py (si hay)
"""
import argparse
import io
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_REPO_ZIP = "https://codeload.github.com/dqx-translation-project/dqx-custom-translations/zip/refs/heads/main"
REQUIRED_SHEETS = ["Dialogue", "Walkthrough", "Quests", "Story So Far"]


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "arkadelatierra-pipeline"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sheet_export_url(url):
    """Acepta la URL normal del Sheet (/d/<id>/edit...) o ya la de exportacion."""
    if "/export" in url:
        return url
    m = re.search(r"/spreadsheets/d/([\w-]+)", url)
    if not m:
        raise ValueError("URL de Google Sheet no reconocida")
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"


def validate_json_entries(data):
    if not isinstance(data, dict) or not data:
        return False
    return all(isinstance(v, dict) for v in data.values())


def fetch_upstream(repo_zip, out_dir, cache_dir, stats):
    j_out, j_cache = out_dir / "custom_json", cache_dir / "custom_json"
    ign_out, ign_cache = out_dir / "ignore.py", cache_dir / "ignore.py"
    j_out.mkdir(parents=True, exist_ok=True)
    j_cache.mkdir(parents=True, exist_ok=True)
    ok = True
    try:
        z = zipfile.ZipFile(io.BytesIO(http_get(repo_zip, 120)))
        good = {}
        for name in z.namelist():
            parts = name.split("/")
            if len(parts) == 3 and parts[1] == "json" and parts[2].startswith("custom_") and parts[2].endswith(".json"):
                try:
                    data = json.loads(z.read(name).decode("utf-8"))
                except Exception as e:
                    print(f"  [!] {parts[2]}: JSON invalido ({e}); se conserva la copia anterior", file=sys.stderr)
                    ok = False
                    continue
                if not validate_json_entries(data):
                    print(f"  [!] {parts[2]}: forma inesperada; se conserva la copia anterior", file=sys.stderr)
                    ok = False
                    continue
                good[parts[2]] = z.read(name)
            elif len(parts) == 3 and parts[1] == "generate_glossary" and parts[2] == "ignore.py":
                ign_cache.write_bytes(z.read(name))
        if not good:
            raise RuntimeError("el zip no trae ningun custom_*.json valido")
        for fn, raw in good.items():
            (j_cache / fn).write_bytes(raw)
        stats["upstream"] = f"descargado ({len(good)} archivos)"
    except Exception as e:
        ok = False
        print(f"  [!] descarga de upstream fallo: {e}", file=sys.stderr)
        stats["upstream"] = "copia anterior" if any(j_cache.glob("custom_*.json")) else "NO DISPONIBLE"
    # copiar cache -> out
    for p in j_cache.glob("custom_*.json"):
        shutil.copyfile(p, j_out / p.name)
    if ign_cache.exists():
        shutil.copyfile(ign_cache, ign_out)
    return ok and any(j_out.glob("custom_*.json"))


def fetch_sheet(sheet_url, out_dir, cache_dir, stats):
    from openpyxl import load_workbook
    cache = cache_dir / "merge.xlsx"
    out = out_dir / "merge.xlsx"
    ok = False
    if sheet_url:
        try:
            raw = http_get(sheet_export_url(sheet_url), 120)
            tmp = cache_dir / "merge.download.xlsx"
            tmp.write_bytes(raw)
            wb = load_workbook(tmp, read_only=True)
            missing = [s for s in REQUIRED_SHEETS if s not in wb.sheetnames]
            wb.close()
            if missing:
                raise ValueError(f"faltan hojas: {missing}")
            shutil.move(str(tmp), cache)
            stats["merge"] = "descargado"
            ok = True
        except Exception as e:
            print(f"  [!] Google Sheet fallo: {e}", file=sys.stderr)
            (cache_dir / "merge.download.xlsx").unlink(missing_ok=True)
    else:
        stats["merge"] = "sin URL configurada"
    if cache.exists():
        shutil.copyfile(cache, out)
        if not ok and sheet_url:
            stats["merge"] = "copia anterior"
    elif sheet_url:
        stats["merge"] = "NO DISPONIBLE"
    return ok or not sheet_url


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--cache-dir", type=Path, required=True)
    ap.add_argument("--repo-zip", default=DEFAULT_REPO_ZIP)
    ap.add_argument("--sheet-url", default="", help="URL del Google Sheet con el merge (cualquiera con el enlace puede ver)")
    ap.add_argument("--strict", action="store_true", help="error si alguna fuente no se pudo refrescar o falta")
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    stats = {}
    ok1 = fetch_upstream(a.repo_zip, a.out_dir, a.cache_dir, stats)
    ok2 = fetch_sheet(a.sheet_url, a.out_dir, a.cache_dir, stats)
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if a.strict and not (ok1 and ok2):
        sys.exit(1)


if __name__ == "__main__":
    main()

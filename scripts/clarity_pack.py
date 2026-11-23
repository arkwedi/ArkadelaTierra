"""
clarity_pack.py

Arma la carpeta `dqxclarity/` que va DENTRO del paquete de idioma (.clpk), segun el
esquema acordado con el dev de dqxclarity:

    dqxclarity/
      json/                              <- los 6 archivos de nombres (monstruos, npc, objetos, ...)
      custom-translations/               <- mismo diseno que el repo dqx-custom-translations
        json/custom_*.json
        csv/merge.xlsx
        csv/glossary.csv

Todo se genera a partir de las filas de Supabase que export_translations.py ya tiene en
memoria (con el respaldo es -> en -> ja y el --clean-encoding de la variante aplicados),
asi que no hay pasos manuales ni archivos intermedios.

Las funciones son puras (no tocan la base de datos) para poder probarlas con datos falsos.
"""

import ast
import csv
import json
import os
import re
import shutil

CUSTOM_PREFIX = "custom_"

# Los 6 archivos que Clarity espera con estos nombres exactos (-> etiqueta de m00_strings).
NAME_FILES = {
    "subPackage02Client": "monsters",
    "smldt_msg_pkg_NPC_DB": "npcs",
    "subPackage05Client": "items",
    "subPackage41Client": "key_items",
    "eventTextSysQuestaClient": "quests",
    "eventTextSysEventaClient": "story_names",
}

# Los 15 archivos que alimentan el glosario (los mismos que generate_glossary/main.py de
# dqx-custom-translations). El orden importa: si una clave se repite, gana el ultimo.
GLOSSARY_SOURCES = [
    "subPackage91Client",
    "eventTextSysQuestaClient",
    "subPackage05Client",
    "subPackage41Client",
    "smldt_msg_pkg_STAGE_ID",
    "smldt_msg_pkg_LOCATIONTITLE",
    "subPackage02Client",
    "smldt_msg_pkg_NPC_DB",
    "subPackage68Client",
    "smldt_msg_pkg_PC_SAVE_POPPOINT_NAME",
    "subPackage127Client",
    "subPackage128Client",
    "subPackage61Client",
    "subPackage40Client",
    "subPackage12Client",
]

# Hojas que Clarity exige en merge.xlsx y sus encabezados exactos (los de upstream).
MERGE_SHEETS = {
    "Dialogue": ["Japanese Text", "NPC Name", "Fixed English Text", "Notes", "Original Source String"],
    "Walkthrough": ["JP Text (DO NOT CHANGE)", "NPC Name", "Fixed English Text", "Notes"],
    "Quests": ["JP Text (DO NOT CHANGE)", "NPC Name", "Fixed English Text", "Notes"],
    "Story So Far": ["JP Text (DO NOT CHANGE)", "Original DeepL Derp", "Fixed English Translation",
                     "Notes", "Japanese Bytes (approximate)", "English Bytes"],
}

TAG_RE = re.compile(r"<%.*?>")


def natural_key(entry_id):
    """'2' < '10' (el orden de texto de la BD pondria '10' antes que '2')."""
    return (0, int(entry_id), "") if str(entry_id).isdigit() else (1, 0, str(entry_id))


# ---------------------------------------------------------------------------
# Entradas opcionales
# ---------------------------------------------------------------------------

def load_ignore(path):
    """IGNORE = { "texto ja": "motivo", ... }  (ignore.py de upstream) -> set de claves."""
    if not path or not os.path.isfile(path):
        return set()
    text = open(path, encoding="utf-8").read()
    m = re.search(r"IGNORE\s*=\s*(\{.*?\n\})", text, re.DOTALL) or re.search(r"IGNORE\s*=\s*(\{.*?\})", text, re.DOTALL)
    if not m:
        return set()
    return set(ast.literal_eval(m.group(1)).keys())


def load_overrides(path):
    """CSV con cabecera ja,es -> {ja: es}. Para forzar terminos concretos en el glosario."""
    if not path or not os.path.isfile(path):
        return {}
    res = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            ja = (row.get("ja") or "").strip("\r\n")
            es = (row.get("es") or "").strip("\r\n")
            if ja and es:
                res[ja] = es
    return res


# ---------------------------------------------------------------------------
# Glosario
# ---------------------------------------------------------------------------

def build_glossary(files_written, ignore=frozenset(), overrides=None, clean=None):
    """files_written: {file: {entry_id: {ja: valor}}} con el valor YA resuelto (es->en->ja)
    y limpio segun la variante. Replica los filtros de generate_glossary/main.py y devuelve
    (filas, stats) con filas = [(ja, valor)] ordenadas como Clarity las necesita: clave mas larga
    primero (bytes UTF-8)."""
    stats = {"missing_sources": [], "untranslated": 0, "comma_keys": [], "terms": 0}
    merged = {}
    for name in GLOSSARY_SOURCES:
        entries = files_written.get(name)
        if entries is None:
            stats["missing_sources"].append(name)
            continue
        for entry_id in sorted(entries, key=natural_key):
            item = entries[entry_id]
            if isinstance(item, dict) and item:
                ja, val = next(iter(item.items()))
                merged[ja] = val

    rows = {}
    for key, value in merged.items():
        key = TAG_RE.sub("", key)
        value = TAG_RE.sub("", value or "").replace("\r", "")
        if "\n" in key or "\n" in value:
            continue
        if key in ignore or not key or not value or value == " ":
            continue
        if key.endswith("。") or value.startswith("'") or key.startswith("は　"):
            continue
        if key == value:                 # sin traducir (cayo hasta el japones): no aporta
            stats["untranslated"] += 1
            continue
        if "," in key:                   # el CSV se corta en la primera coma: la fila quedaria rota
            stats["comma_keys"].append(key)
            continue
        rows[key] = value

    for key, value in (overrides or {}).items():
        if "," in key or "\n" in key or "\n" in value:
            continue
        rows[key] = clean(value) if clean else value

    out = sorted(rows.items(), key=lambda kv: (-len(kv[0].encode("utf-8")), kv[0]))
    stats["terms"] = len(out)
    return out, stats


def write_glossary(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(f"{k},{v}" for k, v in rows))


# ---------------------------------------------------------------------------
# merge.xlsx
# ---------------------------------------------------------------------------

def _make_merge_template(dest):
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for sheet, headers in MERGE_SHEETS.items():
        wb.create_sheet(sheet).append(headers)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    wb.save(dest)


def prepare_merge_xlsx(src, dest, clean=None):
    """Copia merge.xlsx al paquete. Si hay `clean` (variante vanilla), lo aplica a todo el texto
    traducido: cada celda de texto salvo la columna A (la clave en japones) y las formulas.
    Sin `src` crea una plantilla vacia (solo encabezados). Devuelve 'copied', 'cleaned' o 'template'."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not src:
        _make_merge_template(dest)
        return "template"
    from openpyxl import load_workbook
    wb = load_workbook(src)
    missing = [s for s in MERGE_SHEETS if s not in wb.sheetnames]
    if missing:
        raise ValueError(f"merge.xlsx no tiene las hojas requeridas: {', '.join(missing)}")
    if clean is None:
        shutil.copyfile(src, dest)
        return "copied"
    for ws in wb.worksheets:
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if cell.column == 1:
                    continue
                v = cell.value
                if isinstance(v, str) and not v.startswith("="):
                    cell.value = clean(v)
    wb.save(dest)
    return "cleaned"


# ---------------------------------------------------------------------------
# Ensamblado
# ---------------------------------------------------------------------------

def _write_json(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ordered = {k: entries[k] for k in sorted(entries, key=natural_key)}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)


def write_custom_json(folder, custom_written):
    for name, entries in custom_written.items():
        _write_json(os.path.join(folder, name + ".json"), entries)
    return len(custom_written)


def write_pack(pack_dir, files_written, custom_written, merge_xlsx=None, ignore_file=None,
               overrides_file=None, clean=None):
    """Escribe <pack_dir>/dqxclarity/... y devuelve un resumen (dict)."""
    root = os.path.join(pack_dir, "dqxclarity")
    if os.path.isdir(root):
        shutil.rmtree(root)

    summary = {"name_files": 0, "missing_name_files": [], "custom_files": 0}

    for name in NAME_FILES:
        entries = files_written.get(name)
        if not entries:
            summary["missing_name_files"].append(name)
            continue
        _write_json(os.path.join(root, "json", name + ".json"), entries)
        summary["name_files"] += 1

    custom_root = os.path.join(root, "custom-translations")
    summary["custom_files"] = write_custom_json(os.path.join(custom_root, "json"), custom_written)

    rows, gstats = build_glossary(files_written, load_ignore(ignore_file), load_overrides(overrides_file), clean)
    write_glossary(os.path.join(custom_root, "csv", "glossary.csv"), rows)
    summary["glossary"] = gstats

    summary["merge"] = prepare_merge_xlsx(merge_xlsx, os.path.join(custom_root, "csv", "merge.xlsx"), clean)
    summary["root"] = root
    return summary

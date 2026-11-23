#!/usr/bin/env python3
"""
bulk_mt_translate_google.py  (v3 -- Google Cloud Translation)

Rellena traducciones ES vacias en Supabase con un BORRADOR de Google Cloud
Translation. Nunca las marca como revisadas por humanos: las filas quedan con
status='borrador_mt'.

Que conserva de la version DeepL (v2), todo basado en el flujo de dqxclarity:
  * Etiquetas <...>: solo las variables conocidas (<pc>, <npc>, colores...) viajan
    a Google, protegidas. Las demas (<yesno>, <close>, <select..>, <voice..>,
    desconocidas) cortan el texto en fragmentos y vuelven intactas.
  * Puntos suspensivos: "..." / "…" / "……" siempre quedan como "...".
  * Cajas de dialogo: 46 columnas, 3 lineas por caja y un <br> en su propia fila.
  * Textos de una sola linea en el original NO se envuelven.
  * Nombres del glosario se mantienen en ingles.
  * Si Google falla o rompe una etiqueta, la fila se omite (nunca se guarda el
    texto original como si fuera traduccion).

Que cambia respecto a DeepL:
  * Google no acepta instrucciones de estilo, asi que las reglas se aplican
    antes (proteccion de etiquetas/nombres) y despues (limpieza y formato).
  * Las etiquetas y nombres se protegen con <span class="notranslate"> (modo
    HTML), el mecanismo oficial de Google. Con --markup text se usan tokens
    simples, como hace Clarity.
  * Por defecto el texto se guarda con acentos, ñ y ¿¡ (el export los convierte
    con REPLACE_MAP). El ancho de linea se mide como lo vera el juego, o sea
    DESPUES de ese reemplazo (ñ -> ni suma una letra). --ascii-db guarda ya
    limpio en la base, como hacia la v2.
  * Google COBRA al pasar la cuota gratuita (DeepL Free se detenia solo), por eso
    existe --max-chars como tope de seguridad.

Ediciones:
  --api basic     (por defecto)  v2 por REST con API key (GOOGLE_API_KEY), igual
                                 que el backend "google" de Clarity.
  --api advanced                 v3, requiere google-cloud-translate y
                                 credenciales de cuenta de servicio.
Modelos:
  --model nmt     (por defecto)  Neural Machine Translation.
  --model tllm                   Translation LLM (requiere --project).

Antes de traducir en serio, prueba tu combinacion API/modelo/markup sin tocar la
base de datos:
    python bulk_mt_translate_google.py --probe

Requiere: pip install psycopg2-binary   (y google-cloud-translate solo con --api advanced)
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import socket
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None
    execute_values = None

# ─────────────────────────────────────────────────────────────────────────────
# Configuracion
# ─────────────────────────────────────────────────────────────────────────────

LINE_WIDTH = 46             # Ancho maximo de linea (caracteres visibles EN EL JUEGO)
LINES_PER_PAGE = 3          # Lineas por caja de dialogo antes de necesitar <br>
BATCH_SIZE = 40             # Filas por lote
API_DELAY = 0.2             # Segundos entre lotes
MAX_CHAR = 4000             # Un fragmento mas largo que esto se omite (no se trunca)
UPDATED_BY_USER = "bulk_script"

GOOGLE_ENDPOINT = "https://translation.googleapis.com"
MAX_TEXTS_PER_REQUEST = 100        # v2 admite 128 segmentos; margen de seguridad
MAX_CHARS_PER_REQUEST = 25_000     # Advanced/TLLM: 30.000 por request; Basic: 100 KB
HTTP_TIMEOUT = 60
MAX_RETRIES = 5
DEFAULT_MAX_CHARS = 500_000        # tope por corrida (= cuota gratuita mensual de NMT)

# Un <br> del original que cierra una caja LLENA (3 lineas) se considera
# paginacion automatica: se recalcula segun el largo del texto en espanol.
# Un <br> tras una caja corta se conserva como pausa intencional.
MERGE_FULL_PAGES = True

# Nombres del glosario que se dejan en ingles. Solo se protegen los que tienen
# alguna mayuscula (nombres propios) para no congelar palabras comunes.
PROTECT_ONLY_CAPITALIZED = True
MIN_NAME_LEN = 3

# ─────────────────────────────────────────────────────────────────────────────
# Reemplazos para el juego (mismo diccionario que export_translations.py)
# ─────────────────────────────────────────────────────────────────────────────

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

    # Símbolos de apertura/interrogación/exclamación y ordinales (reemplazados por vacío '')
    '¿': '', '¡': '', 'º': '', 'ª': ''
}
_REPLACE_TABLE = str.maketrans(REPLACE_MAP)


def to_game_charset(text: str) -> str:
    """Aplica REPLACE_MAP (lo que hara el export). Normaliza a NFC antes."""
    return unicodedata.normalize("NFC", text).translate(_REPLACE_TABLE)


# ─────────────────────────────────────────────────────────────────────────────
# Etiquetas del juego
# ─────────────────────────────────────────────────────────────────────────────

BR_TAG = "<br>"
BR_PH = "<&br_page>"

# Variables que se expanden en texto DENTRO de una frase (misma tabla que
# PlaceholderTags de dqxclarity). El LARGO del placeholder es el ancho aproximado
# que ocupa la variable en pantalla: se usa para el wrap.
INLINE_TAGS = {
    "<pc_hiryu>": "<&13_aaaaaaa>",
    "<cs_pchero_hiryu>": "<&13_aaaaaab>",
    "<cs_pchero_race>": "<&8_aaa>",
    "<cs_pchero>": "<&13_aaaaaac>",
    "<kyodai_rel1>": "<&7_aa>",
    "<kyodai_rel2>": "<&7_ab>",
    "<kyodai_rel3>": "<&7_ac>",
    "<pc_hometown>": "<&8_aab>",
    "<pc_race>": "<&8_aac>",
    "<%sM_real_race>": "<&8_aad>",
    "<pc_rel1>": "<&7_ad>",
    "<pc_rel2>": "<&7_ae>",
    "<pc_rel3>": "<&7_af>",
    "<kyodai>": "<&13_aaaaaad>",
    "<pc>": "<&13_aaaaaae>",
    "<client_pcname>": "<&13_aaaaaaf>",
    "<heart>": "<&2a>",
    "<diamond>": "<&2b>",
    "<spade>": "<&2c>",
    "<clover>": "<&2d>",
    "<r_triangle>": "<&2e>",
    "<l_triangle>": "<&2f>",
    "<half_star>": "<&2g>",
    "<null_star>": "<&2h>",
    "<npc>": "<&13_aaaaaag>",
    "<pc_syokugyo>": "<&13_aaaaaah>",
    "<pc_original>": "<&13_aaaaaai>",
    "<log_pc>": "<&13_aaaaaaj>",
    "<%sM_NAME>": "<&13_aaaaaak>",
    "<%sM_BEFORE_NAME>": "<&13_aaaaaal>",
    "<%sM_OWNER_OTHER>": "<&13_aaaaaam>",
    "<%sM_OWNER>": "<&13_aaaaaan>",
    "<%sM_SAMA>": "<&6_a>",
    "<1st_title>": "<&20_aaaaaaaaaaaaaa>",
    "<2nd_title>": "<&20_aaaaaaaaaaaaab>",
    "<3rd_title>": "<&20_aaaaaaaaaaaaac>",
    "<4th_title>": "<&20_aaaaaaaaaaaaad>",
    "<5th_title>": "<&20_aaaaaaaaaaaaae>",
    "<6th_title>": "<&20_aaaaaaaaaaaaaf>",
    "<7th_title>": "<&20_aaaaaaaaaaaaag>",
}
TAG_WIDTH = {tag: len(ph) for tag, ph in INLINE_TAGS.items()}   # colores y demas: ancho 0

TAG_RE = re.compile(r"<[A-Za-z%&/][^<>]*>")
TAG_SPLIT_RE = re.compile(r"(<[A-Za-z%&/][^<>]*>)")
COLOR_RE = re.compile(r"<color_\w+>")
SELECT_RE = re.compile(r"<select", re.I)
STRICT_PH_RE = re.compile(r"<&(\w+)>")
STRICT_SPLIT_RE = re.compile(r"(<&\w+>)")
# Tolerante: a veces el traductor se come el '<' o el '>' de un placeholder.
LOOSE_PH_RE = re.compile(r"<&(\w+)>?|(?<![\w<])&(\w+)>")
LETTER_RE = re.compile(r"[^\W\d_]")
JP_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
ELLIPSIS_RUN_RE = re.compile(r"[…‥]+|\.{4,}")


class _Tokens:
    """Traduce placeholder -> texto original (etiquetas fijas, colores y nombres)."""

    def __init__(self):
        self.static = {ph: tag for tag, ph in INLINE_TAGS.items()}
        self.static[BR_PH] = BR_TAG
        self.names: dict[str, str] = {}

    def original(self, canon: str):
        if canon in self.static:
            return self.static[canon]
        if canon.startswith("<&color_"):
            return "<" + canon[2:]
        return self.names.get(canon)


TOKENS = _Tokens()


def classify(tag: str) -> str:
    if tag == BR_TAG:
        return "br"
    if tag in INLINE_TAGS or COLOR_RE.fullmatch(tag):
        return "inline"
    return "opaque"          # <yesno>, <close>, <select..>, <voice..>, desconocidas...


def inline_token(tag: str) -> str:
    return INLINE_TAGS.get(tag) or "<&" + tag[1:]


# ─────────────────────────────────────────────────────────────────────────────
# Glosario local (nombres que se quedan en ingles)
# ─────────────────────────────────────────────────────────────────────────────

def load_local_glossary(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT ja, en FROM glossary WHERE ja != '' AND en != ''")
        result = dict(cur.fetchall())
    finally:
        conn.close()
    print(f"Glosario local cargado: {len(result):,} terminos")
    return result


def _key_head_bytes(ja: str) -> int:
    """Como Clarity: se ordena por el largo UTF-8 de la clave hasta la primera coma."""
    return len(ja.split(",", 1)[0].encode("utf-8"))


class Glossary:
    def __init__(self, ja2en: dict):
        pairs = [(ja, en) for ja, en in ja2en.items() if ja and en]
        pairs.sort(key=lambda kv: _key_head_bytes(kv[0]), reverse=True)   # claves largas primero
        self.count = len(pairs)
        self.ja_items = []
        self.name_tokens: dict[str, str] = {}
        for ja, en in pairs:
            token = None
            if self._is_name(en):
                token = self.name_tokens.get(en)
                if token is None:
                    token = f"<&9_nm{len(self.name_tokens):05d}>"
                    self.name_tokens[en] = token
                    TOKENS.names[token] = en
            self.ja_items.append((ja, token, en))
        self.name_re = None
        if self.name_tokens:
            ordered = sorted(self.name_tokens, key=len, reverse=True)
            self.name_re = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in ordered) + r")(?!\w)")

    @staticmethod
    def _is_name(en: str) -> bool:
        if not (MIN_NAME_LEN <= len(en) <= 60) or "<" in en or ">" in en:
            return False
        if not re.search(r"[A-Za-z]", en):
            return False
        return any(c.isupper() for c in en) if PROTECT_ONLY_CAPITALIZED else True

    def protect(self, text: str, lang: str) -> str:
        if lang == "JA":
            for ja, token, en in self.ja_items:
                if ja in text:
                    # nombres -> placeholder (se quedan en ingles);
                    # palabras comunes -> su equivalente en ingles, que Google traduce al espanol.
                    text = text.replace(ja, token if token else f" {en} ")
            return text
        if self.name_re:
            return self.name_re.sub(lambda m: self.name_tokens[m.group(0)], text)
        return text


# ─────────────────────────────────────────────────────────────────────────────
# Ajustes de ejecucion
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Settings:
    width: int = LINE_WIDTH
    lines_per_page: int = LINES_PER_PAGE
    wrap: str = "auto"                 # auto | always | never
    nowrap_files: set = field(default_factory=set)
    ascii_db: bool = False             # True: guardar ya limpio (como la v2); False: lo limpia el export
    merge_full_pages: bool = MERGE_FULL_PAGES
    glossary: Glossary | None = None
    opaque_seen: Counter = field(default_factory=Counter)


# ─────────────────────────────────────────────────────────────────────────────
# Limpieza del texto fuente
# ─────────────────────────────────────────────────────────────────────────────

_HONORIFICS = ["さま", "君", "どの", "ちゃん", "くん", "様", "さーん", "殿", "さん"]
_NAME_TAGS = ["<pc>", "<cs_pchero>", "<kyodai>"]


def clean_source(text: str, lang: str) -> str:
    if lang == "JA":
        for odd in ("「", "～", "♪"):
            text = text.replace(odd, "")
        text = text.replace("…。", ".").replace("。", ".")
        text = text.replace("\n　", "\n")
        for tag in _NAME_TAGS:
            for hon in _HONORIFICS:
                text = text.replace(f"{tag}{hon}", tag)
        return ELLIPSIS_RUN_RE.sub("…", text)
    return ELLIPSIS_RUN_RE.sub("...", text)


def _flatten(text: str, lang: str) -> str:
    """Los saltos de linea del original son wraps: se vuelven a calcular."""
    if lang == "JA":
        return text.replace("\n", "")
    return re.sub(r"\s*\n\s*", " ", text)


# ─────────────────────────────────────────────────────────────────────────────
# Preparacion de filas: texto -> piezas (etiquetas intactas + fragmentos a traducir)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Piece:
    kind: str                  # tag | raw | unit | list
    text: str = ""             # tag/raw: se copia tal cual
    key: str = ""              # unit: texto con placeholders que se manda a Google
    lead: str = ""             # unit: espacio/salto de linea inicial original
    trail: str = ""            # unit: espacio/salto de linea final original
    multiline: bool = False    # unit: aplicar wrap + <br>
    items: list = field(default_factory=list)   # list: [("raw"|"unit", valor)]


@dataclass
class PreparedRow:
    source: str
    lang: str
    pieces: list = field(default_factory=list)
    src_tags: Counter = field(default_factory=Counter)

    def keys(self):
        for p in self.pieces:
            if p.kind == "unit":
                yield (self.lang, p.key)
            elif p.kind == "list":
                for kind, val in p.items:
                    if kind == "unit":
                        yield (self.lang, val)


def prepare_row(text: str, lang: str, st: Settings, nowrap: bool = False) -> PreparedRow:
    text = clean_source(text, lang)
    prep = PreparedRow(source=text, lang=lang)
    prep.src_tags = Counter(t for t in TAG_RE.findall(text) if t != BR_TAG)

    unit: list = []
    after_select = False

    def flush():
        if unit:
            prep.pieces.append(_make_unit(list(unit), lang, st, nowrap))
            unit.clear()

    for i, tok in enumerate(TAG_SPLIT_RE.split(text)):
        if i % 2 == 0:                      # texto
            if not tok:
                continue
            if after_select:
                flush()
                prep.pieces.append(_make_list(tok, lang, st))
                after_select = False
            else:
                unit.append(("text", tok))
            continue
        kind = classify(tok)                # etiqueta
        if kind in ("inline", "br"):
            unit.append((kind, tok))
            after_select = False
        else:
            flush()
            prep.pieces.append(Piece("tag", text=tok))
            st.opaque_seen[tok] += 1
            after_select = bool(SELECT_RE.match(tok))
    flush()
    return prep


def _protect(text: str, lang: str, st: Settings) -> str:
    return st.glossary.protect(text, lang) if st.glossary else text


def _make_unit(tokens: list, lang: str, st: Settings, nowrap: bool) -> Piece:
    raw = "".join(v for _, v in tokens)
    parts: list = []
    page_raw: list = []
    has_br = False

    for idx, (kind, val) in enumerate(tokens):
        if kind == "br":
            has_br = True
            prev = "".join(page_raw)
            lines = len([ln for ln in prev.split("\n") if ln.strip()])
            has_next = any(k != "br" and (k == "inline" or v.strip()) for k, v in tokens[idx + 1:])
            if st.merge_full_pages and prev.strip() and has_next and lines >= st.lines_per_page:
                parts.append(" ")                    # caja llena: paginacion automatica
            else:
                parts.append(f" {BR_PH} ")           # pausa intencional: se conserva
            page_raw = []
        elif kind == "inline":
            parts.append(inline_token(val))
            page_raw.append(val)
        else:
            parts.append(_protect(_flatten(val, lang), lang, st))
            page_raw.append(val)

    key = " ".join("".join(parts).split())
    if not LETTER_RE.search(STRICT_PH_RE.sub("", key)):
        return Piece("raw", text=raw)                # nada que traducir (solo variables/simbolos)

    # Se conserva lo que habia junto a las etiquetas: salto de linea o un espacio.
    lead_ws = raw[:len(raw) - len(raw.lstrip())]
    trail_ws = raw[len(raw.rstrip()):]
    lead = "\n" if "\n" in lead_ws else (" " if lead_ws else "")
    trail = "\n" if "\n" in trail_ws else (" " if trail_ws else "")
    if nowrap or st.wrap == "never":
        multiline = False
    elif st.wrap == "always":
        multiline = True
    else:
        multiline = has_br or "\n" in raw.strip()
    return Piece("unit", key=key, lead=lead, trail=trail, multiline=multiline)


def _make_list(text: str, lang: str, st: Settings) -> Piece:
    """Opciones de un <select>: una por linea, sin wrap ni <br>."""
    items = []
    for line in text.split("\n"):
        key = " ".join(_protect(line, lang, st).split())
        if line.strip() and LETTER_RE.search(STRICT_PH_RE.sub("", key)):
            items.append(("unit", key))
        else:
            items.append(("raw", line))
    return Piece("list", items=items)


# ─────────────────────────────────────────────────────────────────────────────
# Restauracion de placeholders y validacion
# ─────────────────────────────────────────────────────────────────────────────

def restore_tokens(text: str):
    """Placeholders -> original. Devuelve (texto, Counter de placeholders vistos)."""
    found: Counter = Counter()

    def repl(m):
        canon = f"<&{m.group(1) or m.group(2)}>"
        found[canon] += 1
        orig = TOKENS.original(canon)
        return orig if orig is not None else m.group(0)

    return LOOSE_PH_RE.sub(repl, text), found


def restore_and_check(key: str, translated: str):
    """(ok, texto_restaurado). ok=False si Google perdio/duplico/invento etiquetas."""
    expected = Counter(f"<&{b}>" for b in STRICT_PH_RE.findall(key))
    restored, found = restore_tokens(translated)
    if found != expected:
        return False, restored
    src_restored, _ = restore_tokens(key)
    if Counter(TAG_RE.findall(restored)) != Counter(TAG_RE.findall(src_restored)):
        return False, restored
    return True, restored


# ─────────────────────────────────────────────────────────────────────────────
# Postprocesamiento de espanol
# ─────────────────────────────────────────────────────────────────────────────

_QUOTE_MAP = str.maketrans({
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'", "‹": "'", "›": "'", "´": "'",
})


def postprocess_es(text: str, ascii_db: bool = False) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_QUOTE_MAP)                       # « » “ ” ’ -> " '
    for dash, repl in (("—", "--"), ("―", "--"), ("–", "-"), ("‐", "-"), ("‑", "-")):
        text = text.replace(dash, repl)
    text = ELLIPSIS_RUN_RE.sub("...", text)                 # … …… ....... -> ...
    text = text.replace("　", " ").replace(" ", " ")
    if ascii_db:
        text = to_game_charset(text)
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c)).encode("ascii", "ignore").decode()
    return re.sub(r"[ \t]+", " ", text).strip()


@dataclass
class UnitResult:
    text: str | None
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def finalize(restored: str, st: Settings, warnings: list | None = None) -> UnitResult:
    errors, warns = [], list(warnings or [])
    plain = TAG_RE.sub("", restored)
    if JP_RE.search(plain):
        errors.append("quedaron caracteres japoneses sin traducir")
    text = postprocess_es(restored, ascii_db=st.ascii_db)
    if not text:
        errors.append("traduccion vacia")
    leftovers = sorted({c for c in to_game_charset(TAG_RE.sub("", text)) if ord(c) > 127})
    if leftovers:
        warns.append("caracteres que REPLACE_MAP no cubre: " + " ".join(leftovers))
    return UnitResult(text, errors, warns)


# ─────────────────────────────────────────────────────────────────────────────
# Formato de dialogo: wrap por ancho visible + <br> cada N lineas
# ─────────────────────────────────────────────────────────────────────────────

def visible_len(s: str) -> int:
    """Largo tal como lo vera el juego: las variables cuentan lo que ocupan
    (<pc> ~ 13), los colores 0, y el texto se mide DESPUES de REPLACE_MAP."""
    total, pos = 0, 0
    for m in TAG_RE.finditer(s):
        total += len(to_game_charset(s[pos:m.start()])) + TAG_WIDTH.get(m.group(), 0)
        pos = m.end()
    return total + len(to_game_charset(s[pos:]))


def wrap_words(text: str, width: int) -> list:
    lines, cur, cur_len = [], [], 0
    for word in text.split():           # solo separa por espacios: nunca parte "<pc>!" en dos
        wl = visible_len(word)
        if cur and cur_len + 1 + wl > width:
            lines.append(" ".join(cur))
            cur, cur_len = [word], wl
        else:
            cur_len += (1 if cur else 0) + wl
            cur.append(word)
    if cur:
        lines.append(" ".join(cur))
    return lines


def paginate(lines: list, per_page: int) -> list:
    """Inserta un <br> en su propia fila tras cada `per_page` lineas."""
    out = []
    for i, line in enumerate(lines):
        if i and i % per_page == 0:
            out.append(BR_TAG)
        out.append(line)
    return out


def layout(text: str, multiline: bool, st: Settings) -> str:
    lines = []
    for i, page in enumerate(text.split(BR_TAG)):
        if i:
            lines.append(BR_TAG)
        page = " ".join(page.split())
        if not page:
            continue
        if multiline:
            lines.extend(paginate(wrap_words(page, st.width), st.lines_per_page))
        else:
            lines.append(page)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Google Cloud Translation
# ─────────────────────────────────────────────────────────────────────────────

class FatalApiError(Exception):
    """Credenciales/facturacion/cuota: no tiene sentido seguir."""


class BadRequest(Exception):
    """Google rechazo ESTE request (400): se parte en mitades para aislar el texto culpable."""


class TransientError(Exception):
    """Error reintentable (429, 5xx, red)."""


TOKEN_FMT = "QX{}Z"
TOKEN_RE = re.compile(r"QX(\d+)Z")
SPAN_RE = re.compile(r"<span\b[^>]*>(.*?)</span\s*>", re.I | re.S)
OTHER_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")      # no toca los placeholders (<&...>)
SPAN_OPEN = '<span class="notranslate">'
SPAN_CLOSE = "</span>"
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}
FATAL_REASONS = {"keyInvalid", "accessNotConfigured", "billingNotEnabled", "dailyLimitExceeded",
                 "forbidden", "ipRefererBlocked", "SERVICE_DISABLED", "API_KEY_INVALID"}


class Transport:
    """Convierte claves canonicas (con placeholders <&..>) hacia/desde lo que
    entiende Google.

    html: etiquetas y nombres van en <span class="notranslate">...</span>, el
          mecanismo oficial (solo funciona si el texto se envia como HTML).
          Los nombres del glosario van con su texto real (Google ve el nombre en
          contexto); las variables van como un token QX{n}Z.
    text: todo placeholder se vuelve un token QX{n}Z suelto, sin HTML.
    """

    def __init__(self, markup: str = "html"):
        if markup not in ("html", "text"):
            raise ValueError("markup debe ser 'html' o 'text'")
        self.markup = markup
        self.idx_by_ph: dict[str, int] = {}
        self.ph_by_idx: dict[int, str] = {}

    def _idx(self, ph: str) -> int:
        if ph not in self.idx_by_ph:
            n = len(self.idx_by_ph)
            self.idx_by_ph[ph] = n
            self.ph_by_idx[n] = ph
        return self.idx_by_ph[ph]

    def to_payload(self, key: str) -> str:
        out = []
        for i, part in enumerate(STRICT_SPLIT_RE.split(key)):
            if i % 2 == 0:
                out.append(html.escape(part, quote=False) if self.markup == "html" else part)
                continue
            if self.markup == "html":
                name = TOKENS.names.get(part)
                inner = html.escape(name, quote=False) if name else TOKEN_FMT.format(self._idx(part))
                out.append(f"{SPAN_OPEN}{inner}{SPAN_CLOSE}")
            else:
                out.append(TOKEN_FMT.format(self._idx(part)))
        return "".join(out)

    def _ph_from_inner(self, inner: str):
        m = TOKEN_RE.fullmatch(inner)
        if m:
            return self.ph_by_idx.get(int(m.group(1)))
        for ph, name in TOKENS.names.items():
            if name == inner:
                return ph
        return None

    def from_payload(self, resp: str) -> str:
        if self.markup == "html":
            def span_sub(m):
                inner = html.unescape(m.group(1)).strip()
                return self._ph_from_inner(inner) or m.group(1)

            resp = SPAN_RE.sub(span_sub, resp)
            resp = OTHER_TAG_RE.sub("", resp)               # <i>, <b>, estilos que Google pudiera agregar
        resp = TOKEN_RE.sub(lambda m: self.ph_by_idx.get(int(m.group(1)), m.group(0)), resp)
        if self.markup == "html":
            parts = STRICT_SPLIT_RE.split(resp)
            resp = "".join(html.unescape(p) if i % 2 == 0 else p for i, p in enumerate(parts))
        return resp


class GoogleBackend:
    """Base: trocea, reintenta y aisla textos problematicos. Las subclases
    implementan _call(batch, source, target) -> list[str]."""

    name = "google"

    def __init__(self, markup: str = "html"):
        self.tr = Transport(markup)
        self.chars_sent = 0
        self.requests = 0
        self.debug = False
        self.last_error = ""

    # --- API publica ---------------------------------------------------------
    def payload(self, key: str) -> str:
        return self.tr.to_payload(key)

    def translate(self, keys: list, source: str, target: str) -> list:
        """Devuelve una lista alineada con `keys`: texto con placeholders
        canonicos, o None si ese texto no se pudo traducir."""
        payloads = [self.payload(k) for k in keys]
        out: list = [None] * len(keys)
        for idxs in self._chunks(payloads):
            self._run(idxs, payloads, out, source, target)
        return [self.tr.from_payload(o) if o is not None else None for o in out]

    # --- internos ------------------------------------------------------------
    @staticmethod
    def _chunks(payloads: list):
        cur, size = [], 0
        for i, p in enumerate(payloads):
            if cur and (len(cur) >= MAX_TEXTS_PER_REQUEST or size + len(p) > MAX_CHARS_PER_REQUEST):
                yield cur
                cur, size = [], 0
            cur.append(i)
            size += len(p)
        if cur:
            yield cur

    def _run(self, idxs, payloads, out, source, target):
        batch = [payloads[i] for i in idxs]
        try:
            res = self._retry(lambda: self._call(batch, source, target))
            if len(res) != len(batch):
                raise BadRequest("la respuesta trae otra cantidad de textos que el request")
        except BadRequest as e:
            if len(idxs) > 1:                               # bisecar para aislar al culpable
                mid = len(idxs) // 2
                self._run(idxs[:mid], payloads, out, source, target)
                self._run(idxs[mid:], payloads, out, source, target)
            else:
                self.last_error = str(e)
                print(f"  [!] Google rechazo un texto: {e} :: {batch[0][:80]!r}")
            return
        if self.debug:
            for sent, got in zip(batch, res):
                print(f"  [raw] ->  {sent!r}\n  [raw] <-  {got!r}")
        for i, r in zip(idxs, res):
            out[i] = r

    def _retry(self, fn):
        last = None
        for attempt in range(MAX_RETRIES):
            try:
                return fn()
            except TransientError as e:
                last = e
                wait = min(2 ** attempt * 2, 60)
                print(f"  Error temporal ({e}); reintento {attempt + 1}/{MAX_RETRIES} en {wait}s")
                time.sleep(wait)
        raise FatalApiError(f"Sin respuesta de Google tras {MAX_RETRIES} intentos: {last}")

    def _call(self, batch, source, target) -> list:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


def tllm_model_path(project: str, location: str) -> str:
    return f"projects/{project}/locations/{location}/models/general/translation-llm"


def _parse_google_error(raw: str):
    try:
        err = json.loads(raw).get("error", {})
    except Exception:
        return raw[:300], set(), ""
    reasons = {e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict)}
    for d in err.get("details", []):
        if isinstance(d, dict) and d.get("reason"):
            reasons.add(d["reason"])
    return err.get("message", raw[:300]), reasons, err.get("status", "")


class GoogleBasicBackend(GoogleBackend):
    """Cloud Translation - Basic (v2) por REST con API key."""

    def __init__(self, api_key: str, model: str = "nmt", project: str | None = None,
                 location: str = "us-central1", markup: str = "html", endpoint: str = GOOGLE_ENDPOINT):
        super().__init__(markup)
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.model_label = model
        self.model_param = None
        if model == "tllm":
            if not project:
                raise ValueError("--model tllm necesita --project (o GOOGLE_CLOUD_PROJECT)")
            self.model_param = tllm_model_path(project, location)

    def describe(self) -> str:
        return f"Cloud Translation Basic (v2), modelo {self.model_label}, markup {self.tr.markup}"

    def _call(self, batch, source, target):
        body = {
            "q": batch,
            "source": source.lower(),
            "target": target.lower(),
            "format": "html" if self.tr.markup == "html" else "text",
        }
        if self.model_param:
            body["model"] = self.model_param
        self.requests += 1
        self.chars_sent += sum(len(p) for p in batch)
        data = self._post(f"{self.endpoint}/language/translate/v2", body)
        try:
            return [t["translatedText"] for t in data["data"]["translations"]]
        except (KeyError, TypeError) as e:
            raise TransientError(f"respuesta inesperada de Google: {str(data)[:120]} ({e})")

    def _post(self, url, body):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json; charset=utf-8", "X-goog-api-key": self.api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            msg, reasons, status = _parse_google_error(e.read().decode("utf-8", "replace"))
            code = e.code
            if code == 429 or code >= 500 or (code == 403 and reasons & RATE_REASONS):
                raise TransientError(f"HTTP {code}: {msg}")
            if code in (401, 403) or reasons & FATAL_REASONS or (code == 400 and "API key" in msg):
                raise FatalApiError(f"HTTP {code}: {msg}")
            if code == 400:
                raise BadRequest(msg)
            raise FatalApiError(f"HTTP {code}: {msg}")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            raise TransientError(f"{type(e).__name__}: {e}")


class GoogleAdvancedBackend(GoogleBackend):
    """Cloud Translation - Advanced (v3). Credenciales por ADC / cuenta de servicio
    (GOOGLE_APPLICATION_CREDENTIALS). No admite API keys."""

    def __init__(self, project: str, model: str = "nmt", location: str | None = None,
                 markup: str = "html", client=None):
        super().__init__(markup)
        if not project:
            raise ValueError("--api advanced necesita --project (o GOOGLE_CLOUD_PROJECT)")
        self.model_label = model
        self.location = location or ("us-central1" if model == "tllm" else "global")
        self.parent = f"projects/{project}/locations/{self.location}"
        self.model_param = tllm_model_path(project, self.location) if model == "tllm" else None
        try:
            from google.api_core import exceptions as gexc
            self.gexc = gexc
            if client is None:
                from google.cloud import translate_v3
                client = translate_v3.TranslationServiceClient()
        except ImportError:
            sys.exit("Falta la libreria de Google. Ejecuta:  pip install google-cloud-translate")
        self.client = client

    def describe(self) -> str:
        return f"Cloud Translation Advanced (v3), modelo {self.model_label}, markup {self.tr.markup}, parent {self.parent}"

    def build_request(self, batch, source, target) -> dict:
        req = {
            "parent": self.parent,
            "contents": batch,
            "mime_type": "text/html" if self.tr.markup == "html" else "text/plain",
            "source_language_code": source.lower(),
            "target_language_code": target.lower(),
        }
        if self.model_param:
            req["model"] = self.model_param
        return req

    def _call(self, batch, source, target):
        g = self.gexc
        self.requests += 1
        self.chars_sent += sum(len(p) for p in batch)
        try:
            resp = self.client.translate_text(request=self.build_request(batch, source, target))
        except g.InvalidArgument as e:
            raise BadRequest(str(e))
        except (g.ResourceExhausted, g.ServiceUnavailable, g.InternalServerError,
                g.DeadlineExceeded, g.TooManyRequests) as e:
            raise TransientError(f"{type(e).__name__}: {e}")
        except (g.PermissionDenied, g.Unauthenticated, g.NotFound, g.FailedPrecondition) as e:
            raise FatalApiError(f"{type(e).__name__}: {e}")
        return [t.translated_text for t in resp.translations]


# ─────────────────────────────────────────────────────────────────────────────
# Traduccion por lotes con validacion y reintento por fragmentos
# ─────────────────────────────────────────────────────────────────────────────

def needs_api(lang: str, key: str) -> bool:
    rest = STRICT_PH_RE.sub("", key)
    if not LETTER_RE.search(rest):
        return False
    if lang == "JA" and not JP_RE.search(rest):
        return False                       # ya viene en ingles: no gastar cuota
    return True


def translate_keys(backend, lang, keys, st, target_lang, cache):
    todo = []
    for key in keys:
        if (lang, key) in cache:
            continue
        if not needs_api(lang, key):
            restored, _ = restore_tokens(key)
            cache[(lang, key)] = finalize(restored, st)
        elif len(key) > MAX_CHAR:
            cache[(lang, key)] = UnitResult(None, [f"fragmento de mas de {MAX_CHAR} caracteres"])
        else:
            todo.append(key)
    if not todo:
        return

    raw = backend.translate(todo, lang, target_lang)
    failed = []
    for key, translated in zip(todo, raw):
        if translated is None:
            cache[(lang, key)] = UnitResult(None, ["Google no devolvio traduccion"])
            continue
        ok, restored = restore_and_check(key, translated)
        if ok:
            cache[(lang, key)] = finalize(restored, st)
        else:
            failed.append(key)

    if failed:
        _retry_by_fragments(backend, lang, failed, st, target_lang, cache)


def _retry_by_fragments(backend, lang, failed, st, target_lang, cache):
    """Google rompio algun placeholder: se reintenta traduciendo SOLO el texto entre
    placeholders (sin mandarlos). Menos contexto, pero estructura garantizada."""
    plans, flat = {}, []
    for key in failed:
        plan = []
        for part in STRICT_SPLIT_RE.split(key):
            if not part:
                continue
            if STRICT_PH_RE.fullmatch(part) or not LETTER_RE.search(part):
                plan.append((None, part, "", ""))
            else:
                stripped = part.strip()
                lead = part[:len(part) - len(part.lstrip())]
                trail = part[len(part.rstrip()):]
                plan.append((len(flat), stripped, lead, trail))
                flat.append(stripped)
        plans[key] = plan

    raw = backend.translate(flat, lang, target_lang)
    for key, plan in plans.items():
        rebuilt, bad = [], False
        for idx, val, lead, trail in plan:
            if idx is None:
                rebuilt.append(val)
            elif raw[idx] is None:
                bad = True
                break
            else:
                rebuilt.append(lead + raw[idx].strip() + trail)
        if bad:
            cache[(lang, key)] = UnitResult(None, ["fallo la traduccion por fragmentos"])
            continue
        ok, restored = restore_and_check(key, "".join(rebuilt))
        if not ok:
            cache[(lang, key)] = UnitResult(None, ["no se pudo conservar las etiquetas"])
            continue
        cache[(lang, key)] = finalize(restored, st, ["traducido por fragmentos (menos contexto)"])


# ─────────────────────────────────────────────────────────────────────────────
# Armado de la fila final
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RowResult:
    text: str | None
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.text is not None and not self.errors


def build_row(prep: PreparedRow, cache: dict, st: Settings) -> RowResult:
    out, errors, warnings = [], [], []

    def get(key):
        res = cache.get((prep.lang, key))
        if res is None or res.text is None:
            errors.extend(res.errors if res else ["sin traduccion"])
            return None
        errors.extend(res.errors)
        warnings.extend(res.warnings)
        return res

    for p in prep.pieces:
        if p.kind in ("tag", "raw"):
            out.append(p.text)
        elif p.kind == "unit":
            res = get(p.key)
            if res is None:
                return RowResult(None, list(dict.fromkeys(errors)), warnings)
            laid = layout(res.text, p.multiline, st)
            if p.multiline:
                for ln in laid.split("\n"):
                    if ln != BR_TAG and visible_len(ln) > st.width:
                        warnings.append(f"linea de {visible_len(ln)} caracteres (limite {st.width})")
            out.append(p.lead + laid + p.trail)
        else:  # list
            lines = []
            for kind, val in p.items:
                if kind == "raw":
                    lines.append(val)
                else:
                    res = get(val)
                    if res is None:
                        return RowResult(None, list(dict.fromkeys(errors)), warnings)
                    lines.append(" ".join(res.text.split()))
            out.append("\n".join(lines))

    text = "".join(out)
    out_tags = Counter(t for t in TAG_RE.findall(text) if t != BR_TAG)
    if out_tags != prep.src_tags:
        errors.append(f"etiquetas distintas (faltan {dict(prep.src_tags - out_tags)}, "
                      f"sobran {dict(out_tags - prep.src_tags)})")
    if not text.strip():
        errors.append("resultado vacio")
    return RowResult(text, list(dict.fromkeys(errors)), list(dict.fromkeys(warnings)))


def process_batch(backend, preps, st, target_lang, cache):
    by_lang: dict = {}
    for prep in preps:
        for lang, key in prep.keys():
            by_lang.setdefault(lang, {})[key] = None
    for lang, keys in by_lang.items():
        translate_keys(backend, lang, list(keys), st, target_lang, cache)
    return [build_row(p, cache, st) for p in preps]


# ─────────────────────────────────────────────────────────────────────────────
# Supabase
# ─────────────────────────────────────────────────────────────────────────────

def fetch_pending_rows(pg_conn, file_filter=None, files_filter=None, filter_text=None):
    sql = "SELECT file, entry_id, ja, en, status FROM entries WHERE es = '' AND status = 'pendiente'"
    params = []
    if file_filter:
        sql += " AND file = %s"
        params.append(file_filter)
    elif files_filter:
        sql += " AND file = ANY(%s)"
        params.append(files_filter)
    if filter_text:
        sql += " AND en LIKE %s"
        params.append(f"%{filter_text}%")
    sql += " ORDER BY file, entry_id"

    rows = []
    with pg_conn.cursor() as cur:
        cur.execute(sql, params)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            rows.extend(batch)
    return rows


def write_drafts_batch(pg_conn, results):
    """results: lista de (es, file, entry_id). Solo toca filas que SIGAN pendientes
    y vacias, para no pisar lo que alguien edite mientras corre el script."""
    if not results:
        return
    with pg_conn.cursor() as cur:
        execute_values(
            cur,
            "UPDATE entries AS e SET es = v.es, status = 'borrador_mt', "
            f"updated_by = '{UPDATED_BY_USER}', updated_at = now() "
            "FROM (VALUES %s) AS v(es, file, entry_id) "
            "WHERE e.file = v.file AND e.entry_id = v.entry_id "
            "AND e.es = '' AND e.status = 'pendiente'",
            results,
        )
    pg_conn.commit()


def load_input_csv(path, file_filter=None, files_filter=None, filter_text=None):
    """Modo de prueba: filas simuladas desde un CSV (columnas file, entry_id, ja, en; el resto se ignora).
    Devuelve tuplas como fetch_pending_rows. No toca Supabase ni necesita DATABASE_URL."""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"file", "entry_id", "ja", "en"} - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"[!] {path}: faltan columnas {sorted(missing)} (se esperan file, entry_id, ja, en)")
        for r in reader:
            file_key = (r["file"] or "").strip()
            if file_filter and file_key != file_filter:
                continue
            if files_filter and file_key not in files_filter:
                continue
            if filter_text and filter_text not in (r["en"] or ""):
                continue
            rows.append((file_key, str(r["entry_id"]).strip(), r["ja"] or "", r["en"] or "", "pendiente"))
    return rows


def write_output_csv(output_path, rows_data):
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["file", "entry_id", "ja", "en", "es", "status", "avisos"])
        writer.writerows(rows_data)
    print(f"\n[csv] Archivo generado en: {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def pick_source(row, preferred: str):
    """Elige texto e idioma de origen POR FILA (si no hay 'en', cae a japones)."""
    _file, _entry_id, ja, en, _status = row
    if preferred == "EN" and en and en.strip():
        return en, "EN"
    return (ja or ""), "JA"


def print_tag_report(st: Settings):
    if not st.opaque_seen:
        return
    print("\nEtiquetas que se conservan intactas (no se traducen ni se mueven):")
    for tag, n in st.opaque_seen.most_common(30):
        print(f"  {n:>6,}  {tag}")
    if len(st.opaque_seen) > 30:
        print(f"  ... y {len(st.opaque_seen) - 30} distintas mas")


def cost_hint(model: str, chars: int) -> str:
    """Precio de lista; la cuota gratuita (500.000 car./mes de NMT) es mensual y compartida."""
    if model == "tllm":
        return f"~US${chars * 20 / 1e6:,.2f} (TLLM: US$10/millon de entrada + US$10/millon de salida; verifica en tu consola)"
    return (f"hasta ~US${max(0, chars - 500_000) * 20 / 1e6:,.2f} si ya gastaste los 500.000 gratis de este mes "
            f"(NMT: US$20/millon; verifica en tu consola)")


PROBE_SAMPLES = [
    "Welcome to Alltrades Abbey, <pc>! Please wait here...\n<yesno>\n<close>",
    "Hmm...... I wonder where he went...<br>\nOh, there you are, <pc>! Did you bring the <color_yellow>Medicinal Herb<color_white>? It's the senor's favorite.",
    "This is a long line that should be wrapped across several rows of the dialogue box because it keeps going.\n"
    "And this second line continues the story for a while longer so the page break has to appear.\n"
    "Third line of text here to fill the box.\nFourth line should land after a page break.",
]


def make_backend(args):
    markup = args.markup
    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if args.api == "advanced":
        try:
            return GoogleAdvancedBackend(project, model=args.model, location=args.location, markup=markup)
        except ValueError as e:
            sys.exit(f"[!] {e}")
    api_key = args.api_key or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("[!] Falta la API key de Google (--api-key o variable GOOGLE_API_KEY).")
    try:
        return GoogleBasicBackend(api_key, model=args.model, project=project,
                                  location=args.location or "us-central1", markup=markup,
                                  endpoint=args.endpoint or GOOGLE_ENDPOINT)
    except ValueError as e:
        sys.exit(f"[!] {e}")


def run_probe(backend, st: Settings, target_lang: str):
    print(f"\n=== PROBE: {backend.describe()} ===")
    if st.glossary is None:
        st.glossary = Glossary({"__probe__": "Alltrades Abbey"})
    backend.debug = True
    for n, sample in enumerate(PROBE_SAMPLES, 1):
        print(f"\n--- Muestra {n} ---\nORIGINAL:\n{sample}\n")
        prep = prepare_row(sample, "EN", st)
        res = process_batch(backend, [prep], st, target_lang, {})[0]
        print(f"\nRESULTADO ({'OK' if res.ok else 'OMITIDA'}):\n{res.text}")
        if res.errors:
            print("ERRORES:", "; ".join(res.errors))
        if res.warnings:
            print("AVISOS :", "; ".join(res.warnings))
    print(f"\nCaracteres enviados en el probe: {backend.chars_sent:,}")
    print("Revisa que <pc>, <yesno>, <close>, los colores y 'Alltrades Abbey' hayan vuelto intactos "
          "y que no aparezca ningun aviso de 'fragmentos'. Si los span no se respetan con tu modelo, "
          "prueba --markup text.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--file", help="Traducir solo este archivo fuente")
    scope.add_argument("--files", nargs="+", metavar="FILE", help="Traducir este conjunto de archivos")
    scope.add_argument("--all", action="store_true", help="Traducir todo lo pendiente -- requiere --force")

    parser.add_argument("--force", action="store_true", help="Requerido con --all; tambien ignora el tope --max-chars.")
    parser.add_argument("--dry-run", action="store_true", help="Solo estima caracteres y costo, y sale (no llama a Google).")
    parser.add_argument("--probe", action="store_true",
                        help="Prueba 3 textos de ejemplo con tu API/modelo/markup, mostrando lo enviado y recibido. No usa Supabase.")
    parser.add_argument("--glossary", default=None, help="Ruta al glossary.db local")
    parser.add_argument("--source", default="EN", help="Idioma de origen preferido: EN (default) o JA")
    parser.add_argument("--lang", default="es", help="Idioma destino (default: es; tambien vale es-419)")
    parser.add_argument("--filter-text", default=None, help="Solo lineas cuyo 'en' contenga esta cadena")
    parser.add_argument("--limit", type=int, default=None, help="Procesar solo las primeras N filas (para pruebas)")
    parser.add_argument("--input-csv", default=None, metavar="CSV",
                        help="MODO PRUEBA: lee filas simuladas de un CSV (file, entry_id, ja, en) en vez de Supabase. "
                             "No necesita DATABASE_URL ni escribe en la base; el resultado va a --output-csv "
                             "(por defecto <input>_mt.csv). --file/--files/--filter-text/--limit filtran el CSV.")
    parser.add_argument("--output-csv", default=None,
                        help="Genera un CSV con los resultados y pregunta antes de escribir en Supabase")

    g = parser.add_argument_group("Google")
    g.add_argument("--api", choices=["basic", "advanced"], default="basic",
                   help="basic: v2 con API key (default). advanced: v3 con cuenta de servicio.")
    g.add_argument("--model", choices=["nmt", "tllm"], default="nmt", help="nmt (default) o tllm (Translation LLM)")
    g.add_argument("--api-key", default=None, help="API key de Google (o variable GOOGLE_API_KEY). Solo --api basic.")
    g.add_argument("--project", default=None, help="ID del proyecto (o GOOGLE_CLOUD_PROJECT). Obligatorio con tllm o advanced.")
    g.add_argument("--location", default=None, help="Region (tllm: us-central1 por defecto; nmt advanced: global)")
    g.add_argument("--markup", choices=["html", "text"], default="html",
                   help="html: protege con <span class=notranslate> (default). text: tokens sueltos, como Clarity.")
    g.add_argument("--endpoint", default=None, help=argparse.SUPPRESS)   # solo para pruebas
    g.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                   help=f"Tope de caracteres a enviar en esta corrida (default {DEFAULT_MAX_CHARS:,}). --force lo ignora.")

    f = parser.add_argument_group("Formato")
    f.add_argument("--wrap", choices=["auto", "always", "never"], default="auto",
                   help="auto: envuelve solo si el original tiene varias lineas o <br> (default)")
    f.add_argument("--nowrap-files", nargs="+", default=[], metavar="FILE",
                   help="Archivos que nunca se envuelven (nombres, items, menus...)")
    f.add_argument("--width", type=int, default=LINE_WIDTH, help=f"Ancho de linea en el juego (default {LINE_WIDTH})")
    f.add_argument("--lines-per-page", type=int, default=LINES_PER_PAGE,
                   help=f"Lineas por caja antes del <br> (default {LINES_PER_PAGE})")
    f.add_argument("--ascii-db", action="store_true",
                   help="Guarda el texto YA limpio (REPLACE_MAP + solo ASCII) en la base, como hacia la v2. "
                        "Por defecto se guarda con acentos/ñ/¿¡ y lo limpia el export.")

    args = parser.parse_args(argv)

    if not args.probe and not args.input_csv and not (args.file or args.files or args.all):
        parser.error("indica --file, --files o --all (o usa --probe)")
    if args.input_csv and not os.path.isfile(args.input_csv):
        sys.exit(f"[!] No existe el CSV de entrada: {args.input_csv}")
    if args.input_csv and not args.output_csv:
        base, _ = os.path.splitext(args.input_csv)
        args.output_csv = base + "_mt.csv"
    if args.all and not args.force:
        sys.exit("[!] --all requires --force.")

    preferred = args.source.upper()
    target_lang = args.lang.lower()

    st = Settings(
        width=args.width,
        lines_per_page=args.lines_per_page,
        wrap=args.wrap,
        nowrap_files=set(args.nowrap_files),
        ascii_db=args.ascii_db,
    )
    if args.glossary:
        if not os.path.exists(args.glossary):
            sys.exit(f"[!] No existe el glosario: {args.glossary}")
        st.glossary = Glossary(load_local_glossary(args.glossary))

    backend = make_backend(args)

    if args.probe:
        run_probe(backend, st, target_lang)
        return

    if args.input_csv:
        pg_conn = None
        print(f"[modo prueba] Filas simuladas desde {args.input_csv}; no se usa Supabase.")
        rows = load_input_csv(args.input_csv, args.file, args.files, args.filter_text)
    else:
        if psycopg2 is None:
            sys.exit("psycopg2 no instalado. Ejecuta:  pip install psycopg2-binary")
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            sys.exit("[!] DATABASE_URL environment variable is not set.")

        print("Conectando a Supabase...")
        pg_conn = psycopg2.connect(db_url)
        pg_conn.autocommit = False

        rows = fetch_pending_rows(pg_conn, args.file, args.files, args.filter_text)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("No hay filas pendientes en el alcance elegido.")
        if pg_conn:
            pg_conn.close()
        return

    # Preparacion (local, sin gastar cuota)
    items = []          # (row, prep | None)
    for row in rows:
        text, lang = pick_source(row, preferred)
        prep = prepare_row(text, lang, st, nowrap=row[0] in st.nowrap_files) if text.strip() else None
        items.append((row, prep))

    unique_keys = {k for _, prep in items if prep for k in prep.keys() if needs_api(*k) and len(k[1]) <= MAX_CHAR}
    est_chars = sum(len(backend.payload(key)) for _, key in unique_keys)
    print(f"\nFilas pendientes seleccionadas: {len(rows):,}")
    print(f"Backend: {backend.describe()}")
    print(f"Caracteres estimados a enviar (sin repetidos, incluye el marcado): {est_chars:,}")
    print(f"Costo estimado: {cost_hint(args.model, est_chars)}")
    print_tag_report(st)

    if est_chars > args.max_chars and not args.force:
        if pg_conn:
            pg_conn.close()
        sys.exit(f"\n[!] La estimacion ({est_chars:,}) supera --max-chars ({args.max_chars:,}). "
                 f"Sube el tope o usa --force.")
    if args.dry_run:
        print("\n[dry-run] Saliendo sin llamar a Google ni modificar Supabase.")
        if pg_conn:
            pg_conn.close()
        return

    print(f"\nTraduciendo {len(rows):,} filas en lotes de {BATCH_SIZE}...")
    cache: dict = {}
    csv_rows, ok_rows, skipped = [], [], []
    aborted = False

    for start in range(0, len(items), BATCH_SIZE):
        if not args.force and backend.chars_sent >= args.max_chars:
            print(f"\n[!] Se alcanzo --max-chars ({args.max_chars:,}). Se detiene; lo ya traducido se conserva.")
            aborted = True
            break
        batch = items[start:start + BATCH_SIZE]
        preps = [prep for _, prep in batch if prep]
        try:
            results = iter(process_batch(backend, preps, st, target_lang, cache))
        except FatalApiError as e:
            print(f"\n[!] Google respondio con un error que impide continuar: {e}\n    Se detiene; lo ya traducido se conserva.")
            aborted = True
            break

        batch_ok = []
        for row, prep in batch:
            file_key, entry_id, ja, en, _status = row
            if prep is None:
                skipped.append((file_key, entry_id, "sin texto fuente"))
                csv_rows.append([file_key, entry_id, ja, en, "", "error", "sin texto fuente"])
                continue
            res = next(results)
            notes = "; ".join(res.errors + res.warnings)
            if res.ok:
                batch_ok.append((res.text, file_key, entry_id))
                csv_rows.append([file_key, entry_id, ja, en, res.text, "borrador_mt", notes])
            else:
                skipped.append((file_key, entry_id, "; ".join(res.errors)))
                csv_rows.append([file_key, entry_id, ja, en, "", "error", notes])

        ok_rows.extend(batch_ok)
        if not args.output_csv:
            write_drafts_batch(pg_conn, batch_ok)

        end = start + len(batch)
        sample = batch_ok[-1][0].replace("\n", " / ")[:55] if batch_ok else ""
        print(f"  [{end:>5}/{len(rows)}] {end / len(rows) * 100:>5.1f}%  ->  {sample}")
        time.sleep(API_DELAY)

    if args.output_csv:
        write_output_csv(args.output_csv, csv_rows)
        print(f"[?] {len(ok_rows):,} filas listas, {len(skipped):,} omitidas.")
        if ok_rows and pg_conn is not None:
            answer = input("¿Escribir estas entradas en Supabase ahora? [s/N]: ").strip().lower()
            if answer in ("s", "si", "sí", "y", "yes"):
                write_drafts_batch(pg_conn, ok_rows)
                print(f"¡Listo! {len(ok_rows):,} borradores escritos con updated_by='{UPDATED_BY_USER}'.")
            else:
                print("[cancelado] No se modifico Supabase. Los datos quedan solo en el CSV.")

    if pg_conn:
        pg_conn.close()

    print(f"\nResumen: {len(ok_rows):,} traducidas, {len(skipped):,} omitidas"
          + (" (ejecucion interrumpida)" if aborted else "") + ".")
    print(f"Enviado a Google: {backend.chars_sent:,} caracteres en {backend.requests:,} requests.")
    for file_key, entry_id, reason in skipped[:20]:
        print(f"  omitida {file_key}#{entry_id}: {reason}")
    if len(skipped) > 20:
        print(f"  ... y {len(skipped) - 20} mas (revisa el CSV con --output-csv)")


if __name__ == "__main__":
    main()

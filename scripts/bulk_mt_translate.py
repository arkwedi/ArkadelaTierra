#!/usr/bin/env python3
"""
bulk_mt_translate.py  (v2)

Rellena traducciones ES vacias en Supabase con un BORRADOR de DeepL. Nunca las
marca como revisadas por humanos: las filas quedan con status='borrador_mt'.

Que hace distinto a la v1 (ideas tomadas del flujo de dqxclarity):
  * Etiquetas <...>: NINGUNA etiqueta llega a DeepL salvo las variables conocidas
    (<pc>, <npc>, colores...), que viajan como placeholders. Las demas
    (<yesno>, <close>, <select..>, <voice..>, desconocidas) cortan el texto en
    fragmentos y se reinsertan tal cual. Imposible que <yesno> -> <si-no>.
  * Puntos suspensivos: "..." / "…" / "……" se normalizan siempre a "...".
  * Cajas de dialogo: ancho 46 (contando lo que ocupa cada variable en pantalla),
    3 lineas por caja y un <br> en su propia fila para pasar a la siguiente.
  * Textos de una sola linea en el original NO se envuelven (los ajustas tu).
  * Nombres del glosario se mantienen en ingles (protegidos como placeholders).
  * Las instrucciones ahora SI llegan a DeepL (custom_instructions).
  * Si DeepL falla o rompe una etiqueta, la fila se omite; NUNCA se guarda el
    texto original como si fuera traduccion.

Requiere: pip install -U deepl psycopg2-binary
"""

from __future__ import annotations

import argparse
import csv
import inspect
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

try:
    import deepl
except ImportError:
    deepl = None

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None
    execute_values = None

# ─────────────────────────────────────────────────────────────────────────────
# Configuracion
# ─────────────────────────────────────────────────────────────────────────────

LINE_WIDTH = 46             # Ancho maximo de linea (caracteres visibles)
LINES_PER_PAGE = 3          # Lineas por caja de dialogo antes de necesitar <br>
BATCH_SIZE = 40             # Filas por lote
DEEPL_MAX_TEXTS = 50        # Limite de DeepL: textos por request
DEEPL_MAX_CHARS_CALL = 30_000   # Margen bajo el limite de 128 KiB por request
API_DELAY = 0.35            # Segundos entre lotes
MAX_CHAR = 4000             # Truncar fragmentos absurdos antes de enviar
UPDATED_BY_USER = "bulk_script"

# Un <br> del original que cierra una caja LLENA (3 lineas) se considera
# paginacion automatica: se recalcula segun el largo del texto en espanol.
# Un <br> tras una caja corta se conserva como pausa intencional.
MERGE_FULL_PAGES = True

# Nombres del glosario que se dejan en ingles. Solo se protegen los que tienen
# alguna mayuscula (nombres propios) para no congelar palabras comunes.
PROTECT_ONLY_CAPITALIZED = True
MIN_NAME_LEN = 3

# ─────────────────────────────────────────────────────────────────────────────
# Instrucciones para DeepL (custom_instructions: max 10, max 300 caracteres c/u)
# ─────────────────────────────────────────────────────────────────────────────

INSTRUCTIONS = [
    "You are an expert video game localizer. Translate this Dragon Quest X text into natural Spanish, "
    "preserving the tone, humor, personality and emotion of each speaker.",
    "Tokens shaped like <&...> (for example <&13_aaaaaae>) are game variables and names. Copy each one "
    "exactly as written. Never translate, alter, split or remove them.",
    "Never invent or translate anything between < and >. Do not use guillemets (« »), inverted "
    "punctuation (¿ ¡), typographic quotes, em dashes or the single ellipsis character. "
    "Use straight quotes and three plain dots.",
    "Avoid words that contain the letter ñ (año, niño, señor, mañana...). Pick a natural synonym or "
    "rephrase the sentence so the letter is not needed. Accents on vowels are fine.",
    "Spanish tends to be longer than the source. Be concise and stay close to the original length so "
    "the text fits a small dialogue box. Do not add sentences, explanations or extra paragraphs.",
    "Adapt idioms, cultural references and wordplay so they sound natural to Spanish speakers while "
    "keeping the fantasy RPG setting. Avoid profanity and do not repeat the same word over and over.",
    "Keep character voices, terminology and naming consistent across the whole game, and respect "
    "established Dragon Quest localization choices.",
    "Do not translate proper nouns: names of characters, places, monsters, skills and spells. "
    "Keep game-specific jargon and technical terms as in the source.",
    "Adjust formal or informal address to the speaker's role and status in the game world, and keep "
    "it consistent within the same text.",
]
assert len(INSTRUCTIONS) <= 10 and all(len(i) <= 300 for i in INSTRUCTIONS), "custom_instructions fuera de limites"

# ─────────────────────────────────────────────────────────────────────────────
# Etiquetas del juego
# ─────────────────────────────────────────────────────────────────────────────

BR_TAG = "<br>"
BR_PH = "<&br_page>"

# Variables que se expanden en texto DENTRO de una frase. Viajan a DeepL como
# placeholders (misma tabla que dqxclarity). El LARGO del placeholder es el
# ancho aproximado que ocupa la variable en pantalla: se usa para el wrap.
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
# Tolerante: DeepL a veces se come el '<' o el '>' de un placeholder.
LOOSE_PH_RE = re.compile(r"<&(\w+)>?|(?<![\w<])&(\w+)>")
LETTER_RE = re.compile(r"[^\W\d_]")
JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")
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


class Glossary:
    def __init__(self, ja2en: dict):
        pairs = [(ja, en) for ja, en in ja2en.items() if ja and en]
        # claves largas primero (igual que dqxclarity: por bytes UTF-8)
        pairs.sort(key=lambda kv: len(kv[0].encode("utf-8")), reverse=True)
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
                    # palabras comunes -> su equivalente en ingles, que DeepL traduce al espanol.
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
    ascii_only: bool = True
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
    key: str = ""              # unit: texto con placeholders que se manda a DeepL
    lead: str = ""             # unit: salto de linea inicial original
    trail: str = ""            # unit: salto de linea final original
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
    """(ok, texto_restaurado). ok=False si DeepL perdio/duplico/invento etiquetas."""
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

_TILDE_MAP = str.maketrans("áéíóúÁÉÍÓÚüÜ", "aeiouAEIOUuU")
_QUOTE_MAP = str.maketrans({
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'", "‚": "'", "‹": "'", "›": "'", "´": "'",
})

_N_TILDE_FIXES = [(re.compile(p, re.I), r) for p, r in [
    (r"\bniñas\b", "chicas"),
    (r"\bniños\b", "chicos"),
    (r"\bniña\b", "chica"),
    (r"\bniño\b", "chico"),
    (r"\bextraños\b", "extranas"),
    (r"\bextrañas\b", "extranas"),
    (r"\bextraño\b", "extrano"),
    (r"\bextraña\b", "extrana"),
    (r"\bhace\s+(\w+)\s+años\b", r"hace \1 tiempo"),
    (r"\b(\d+)\s+años\b", r"\1 primaveras"),
    (r"\baños\b", "ciclos"),
    (r"\baño\b", "ciclo"),
    (r"\bsueños\b", "visiones"),
    (r"\bsueño\b", "vision"),
    (r"\bdaños\b", "perjuicios"),
    (r"\bdaño\b", "perjuicio"),
    (r"\bseñorita\b", "jovencita"),
    (r"\bseñoras\b", "damas"),
    (r"\bseñora\b", "dama"),
    (r"\bseñores\b", "lords"),
    (r"\bseñor\b", "lord"),
    (r"\besta mañana\b", "esta manana"),
    (r"\bla mañana\b", "la manana"),
    (r"\bmañana\b", "al dia siguiente"),
    (r"\bpequeñas\b", "pequenas"),
    (r"\bpequeños\b", "pequenos"),
    (r"\bpequeña\b", "pequena"),
    (r"\bpequeño\b", "pequeno"),
    (r"\bdueños\b", "amos"),
    (r"\bdueño\b", "amo"),
    (r"\bmontañas\b", "montanas"),
    (r"\bmontaña\b", "montana"),
    (r"\bcañon\b", "canon"),
    (r"ñ", "n"),
]]


def _sub_keep_case(pattern, repl: str, text: str) -> str:
    def fix(m):
        out = m.expand(repl)
        src = m.group(0)
        if len(src) > 1 and src.isupper():
            return out.upper()
        if src[:1].isupper():
            return out[:1].upper() + out[1:]
        return out
    return pattern.sub(fix, text)


def _to_ascii(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.encode("ascii", "ignore").decode()


def postprocess_es(text: str, ascii_only: bool = True) -> str:
    text = text.translate(_QUOTE_MAP)                       # « » “ ” -> "
    for dash, repl in (("—", "--"), ("―", "--"), ("–", "-"), ("‐", "-"), ("‑", "-")):
        text = text.replace(dash, repl)
    text = ELLIPSIS_RUN_RE.sub("...", text)                 # … …… ....... -> ...
    text = text.replace("\u3000", " ").replace("\u00a0", " ")
    if ascii_only:
        for pattern, repl in _N_TILDE_FIXES:
            text = _sub_keep_case(pattern, repl, text)
        text = text.translate(_TILDE_MAP)
        text = re.sub(r"[¿¡]", "", text)
        text = _to_ascii(text)
    return re.sub(r"[ \t]+", " ", text).strip()


@dataclass
class UnitResult:
    text: str | None
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def finalize(restored: str, st: Settings, warnings: list | None = None) -> UnitResult:
    errors = []
    if JP_RE.search(TAG_RE.sub("", restored)):
        errors.append("quedaron caracteres japoneses sin traducir")
    text = postprocess_es(restored, ascii_only=st.ascii_only)
    if not text:
        errors.append("traduccion vacia")
    return UnitResult(text, errors, list(warnings or []))


# ─────────────────────────────────────────────────────────────────────────────
# Formato de dialogo: wrap por ancho visible + <br> cada N lineas
# ─────────────────────────────────────────────────────────────────────────────

def visible_len(s: str) -> int:
    """Largo en pantalla: las variables cuentan lo que ocupan (<pc> ~ 13), los colores 0."""
    total, pos = 0, 0
    for m in TAG_RE.finditer(s):
        total += (m.start() - pos) + TAG_WIDTH.get(m.group(), 0)
        pos = m.end()
    return total + len(s) - pos


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
# DeepL
# ─────────────────────────────────────────────────────────────────────────────

def _deepl_call(translator, chunk, source_lang, target_lang, instructions):
    """Un request a DeepL con reintentos. Devuelve lista de str o None."""
    for attempt in range(3):
        try:
            res = translator.translate_text(
                chunk,
                source_lang=source_lang,
                target_lang=target_lang,
                preserve_formatting=True,
                model_type="prefer_quality_optimized",
                custom_instructions=instructions,
            )
            return [r.text for r in res]
        except Exception as e:
            if deepl is not None and isinstance(e, (deepl.QuotaExceededException, deepl.AuthorizationException)):
                raise
            print(f"  DeepL ERROR (intento {attempt + 1}/3): {e}")
            time.sleep(2 * (attempt + 1))
    return None


def deepl_translate(translator, texts, source_lang, target_lang, instructions):
    """Traduce respetando los limites de DeepL. Devuelve lista alineada (None si fallo)."""
    out = [None] * len(texts)
    i = 0
    while i < len(texts):
        idxs, size = [], 0
        while (i < len(texts) and len(idxs) < DEEPL_MAX_TEXTS
               and (not idxs or size + len(texts[i][:MAX_CHAR]) <= DEEPL_MAX_CHARS_CALL)):
            idxs.append(i)
            size += len(texts[i][:MAX_CHAR])
            i += 1
        res = _deepl_call(translator, [texts[j][:MAX_CHAR] for j in idxs], source_lang, target_lang, instructions)
        if res is not None and len(res) == len(idxs):
            for j, r in zip(idxs, res):
                out[j] = r
    return out


def needs_api(lang: str, key: str) -> bool:
    rest = STRICT_PH_RE.sub("", key)
    if not LETTER_RE.search(rest):
        return False
    if lang == "JA" and not JP_RE.search(rest):
        return False                       # ya viene en ingles: no gastar cuota
    return True


def translate_keys(translator, lang, keys, st, target_lang, instructions, cache):
    todo = []
    for key in keys:
        if (lang, key) in cache:
            continue
        if not needs_api(lang, key):
            restored, _ = restore_tokens(key)
            cache[(lang, key)] = finalize(restored, st)
        else:
            todo.append(key)
    if not todo:
        return

    raw = deepl_translate(translator, todo, lang, target_lang, instructions)
    failed = []
    for key, translated in zip(todo, raw):
        if translated is None:
            cache[(lang, key)] = UnitResult(None, ["DeepL no devolvio traduccion"])
            continue
        ok, restored = restore_and_check(key, translated)
        if ok:
            cache[(lang, key)] = finalize(restored, st)
        else:
            failed.append(key)

    if failed:
        _retry_by_fragments(translator, lang, failed, st, target_lang, instructions, cache)


def _retry_by_fragments(translator, lang, failed, st, target_lang, instructions, cache):
    """DeepL rompio algun placeholder: se reintenta traduciendo SOLO el texto entre
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

    raw = deepl_translate(translator, flat, lang, target_lang, instructions)
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


def process_batch(translator, preps, st, target_lang, instructions, cache):
    by_lang: dict = {}
    for prep in preps:
        for lang, key in prep.keys():
            by_lang.setdefault(lang, {})[key] = None
    for lang, keys in by_lang.items():
        translate_keys(translator, lang, list(keys), st, target_lang, instructions, cache)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--file", help="Traducir solo este archivo fuente")
    scope.add_argument("--files", nargs="+", metavar="FILE", help="Traducir este conjunto de archivos")
    scope.add_argument("--all", action="store_true", help="Traducir todo lo pendiente -- requiere --force")

    parser.add_argument("--force", action="store_true", help="Requerido con --all; tambien ignora el chequeo de cuota.")
    parser.add_argument("--dry-run", action="store_true", help="Solo estima caracteres y sale (no llama a DeepL).")
    parser.add_argument("--api-key", default=None, help="API key de DeepL")
    parser.add_argument("--glossary", default=None, help="Ruta al glossary.db local")
    parser.add_argument("--source", default="EN", help="Idioma de origen preferido: EN (default) o JA")
    parser.add_argument("--lang", default="ES", help="Idioma destino (default: ES; tambien vale ES-419)")
    parser.add_argument("--filter-text", default=None, help="Solo lineas cuyo 'en' contenga esta cadena")
    parser.add_argument("--limit", type=int, default=None, help="Procesar solo las primeras N filas (para pruebas)")
    parser.add_argument("--output-csv", default=None,
                        help="Genera un CSV con los resultados y pregunta antes de escribir en Supabase")

    parser.add_argument("--wrap", choices=["auto", "always", "never"], default="auto",
                        help="auto: envuelve solo si el original tiene varias lineas o <br> (default)")
    parser.add_argument("--nowrap-files", nargs="+", default=[], metavar="FILE",
                        help="Archivos que nunca se envuelven (nombres, items, menus...)")
    parser.add_argument("--width", type=int, default=LINE_WIDTH, help=f"Ancho de linea (default {LINE_WIDTH})")
    parser.add_argument("--lines-per-page", type=int, default=LINES_PER_PAGE,
                        help=f"Lineas por caja antes del <br> (default {LINES_PER_PAGE})")
    parser.add_argument("--spanish-chars", action="store_true",
                        help="Conserva tildes, n con tilde y signos de apertura (por defecto se eliminan)")

    args = parser.parse_args()

    if args.all and not args.force:
        sys.exit("[!] --all requires --force.")
    if deepl is None:
        sys.exit("deepl no instalado. Ejecuta:  pip install -U deepl")
    if psycopg2 is None:
        sys.exit("psycopg2 no instalado. Ejecuta:  pip install psycopg2-binary")

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("[!] DATABASE_URL environment variable is not set.")
    api_key = args.api_key or os.environ.get("DEEPL_API_KEY")
    if not api_key:
        sys.exit("[!] No DeepL API key found.")

    preferred = args.source.upper()
    target_lang = args.lang.upper()

    st = Settings(
        width=args.width,
        lines_per_page=args.lines_per_page,
        wrap=args.wrap,
        nowrap_files=set(args.nowrap_files),
        ascii_only=not args.spanish_chars,
    )
    if args.glossary:
        if not os.path.exists(args.glossary):
            sys.exit(f"[!] No existe el glosario: {args.glossary}")
        st.glossary = Glossary(load_local_glossary(args.glossary))

    print("Conectando a Supabase...")
    pg_conn = psycopg2.connect(db_url)
    pg_conn.autocommit = False

    rows = fetch_pending_rows(pg_conn, args.file, args.files, args.filter_text)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("No hay filas pendientes (es='' y status='pendiente') en el alcance elegido.")
        pg_conn.close()
        return

    # Preparacion (local, sin gastar cuota)
    items = []          # (row, prep | None)
    for row in rows:
        text, lang = pick_source(row, preferred)
        prep = prepare_row(text, lang, st, nowrap=row[0] in st.nowrap_files) if text.strip() else None
        items.append((row, prep))

    unique_keys = {k for _, prep in items if prep for k in prep.keys() if needs_api(*k)}
    est_chars = sum(len(key[:MAX_CHAR]) for _, key in unique_keys)
    print(f"\nFilas pendientes seleccionadas: {len(rows):,}")
    print(f"Caracteres estimados a enviar (sin repetidos): {est_chars:,}")
    print_tag_report(st)

    print("\nConectando a DeepL...")
    client_cls = getattr(deepl, "DeepLClient", deepl.Translator)
    translator = client_cls(api_key)
    if "custom_instructions" not in inspect.signature(translator.translate_text).parameters:
        sys.exit("[!] Tu libreria deepl es antigua y no soporta custom_instructions. Ejecuta: pip install -U deepl")
    try:
        usage = translator.get_usage()
        remaining = usage.character.limit - usage.character.count
        print(f"Cuota DeepL: {usage.character.count:,} / {usage.character.limit:,} usados, {remaining:,} restantes")
    except deepl.AuthorizationException:
        sys.exit("[!] DeepL API key invalida.")

    if est_chars > remaining and not args.force:
        sys.exit(f"\n[!] La estimacion ({est_chars:,}) supera tu cuota restante ({remaining:,}). Usa --force.")
    if args.dry_run:
        print("\n[dry-run] Saliendo sin llamar a DeepL ni modificar Supabase.")
        pg_conn.close()
        return

    print(f"\nTraduciendo {len(rows):,} filas en lotes de {BATCH_SIZE}...")
    cache: dict = {}
    csv_rows, ok_rows, skipped = [], [], []
    aborted = False

    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start:start + BATCH_SIZE]
        preps = [prep for _, prep in batch if prep]
        try:
            results = iter(process_batch(translator, preps, st, target_lang, INSTRUCTIONS, cache))
        except deepl.QuotaExceededException:
            print("\n[!] Se agoto la cuota de DeepL. Se detiene; lo ya traducido se conserva.")
            aborted = True
            break
        except deepl.AuthorizationException:
            print("\n[!] DeepL rechazo la API key. Se detiene.")
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
        if ok_rows:
            answer = input("¿Escribir estas entradas en Supabase ahora? [s/N]: ").strip().lower()
            if answer in ("s", "si", "sí", "y", "yes"):
                write_drafts_batch(pg_conn, ok_rows)
                print(f"¡Listo! {len(ok_rows):,} borradores escritos con updated_by='{UPDATED_BY_USER}'.")
            else:
                print("[cancelado] No se modifico Supabase. Los datos quedan solo en el CSV.")

    pg_conn.close()

    print(f"\nResumen: {len(ok_rows):,} traducidas, {len(skipped):,} omitidas"
          + (" (ejecucion interrumpida)" if aborted else "") + ".")
    for file_key, entry_id, reason in skipped[:20]:
        print(f"  omitida {file_key}#{entry_id}: {reason}")
    if len(skipped) > 20:
        print(f"  ... y {len(skipped) - 20} mas (revisa el CSV con --output-csv)")

    try:
        usage = translator.get_usage()
        print(f"Uso final DeepL: {usage.character.count:,} / {usage.character.limit:,}")
    except Exception:
        pass


if __name__ == "__main__":
    main()

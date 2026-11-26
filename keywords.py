"""
Keyword rules: decide whether a video title, channel name or channel
description mentions one of a parent's blocked words.

Matching is forgiving about the ways people write the same thing:

  * case, accents, punctuation and spacing: "GTA6", "gta 6", "#GTA_6", "G.T.A. 6"
  * numbers as digits, Roman numerals or words: "GTA 6", "GTA VI", "GTA Six"
  * a few well-known game abbreviations: "GTA 6" also catches
    "Grand Theft Auto 6" (and the other way round)

but strict about word edges, so "gta6" does not match "GTA 60" or "vegta6",
and "GTA VI" does not match "GTA Vice City".
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

MAX_KEYWORDS = 50
MAX_LEN = 60

# Short form -> long form. Either one in a keyword also matches the other.
ALIASES = {
    "gta": "grand theft auto",
    "cod": "call of duty",
    "fnaf": "five nights at freddys",
    "rdr": "red dead redemption",
    "csgo": "counter strike global offensive",
    "pubg": "playerunknowns battlegrounds",
    "botw": "breath of the wild",
    "totk": "tears of the kingdom",
}

NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
                "eighteen", "nineteen", "twenty"]


def _roman(n: int) -> str:
    out = ""
    for value, sym in ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out, n = out + sym, n - value
    return out


ROMAN = {_roman(n): n for n in range(1, 31)}


def normalize(text: str) -> str:
    """Lower case, no accents, apostrophes dropped, everything else that isn't
    a letter or digit turned into single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def clean_keyword(kw: str) -> str:
    """How a keyword is stored: trimmed, single spaces, at most MAX_LEN characters."""
    return re.sub(r"\s+", " ", str(kw or "")).strip()[:MAX_LEN]


def tokens(kw: str) -> list[str]:
    """'GTA6' -> ['gta', '6'];  'Grand Theft Auto VI' -> ['grand', 'theft', 'auto', 'vi']."""
    return re.findall(r"[a-z]+|\d+", normalize(kw))


def _number_of(tok: str, first: bool) -> int | None:
    if tok.isdigit():
        return int(tok)
    if first:  # "six flags" or "vi" at the start is a word, not a number
        return None
    if tok in ROMAN:
        return ROMAN[tok]
    if tok in NUMBER_WORDS:
        return NUMBER_WORDS.index(tok)
    return None


def _forms(tok: str, first: bool) -> list[str]:
    """Every way one keyword token can be written."""
    n = _number_of(tok, first)
    if n is None:
        return [tok]
    forms = [str(n)]
    if 1 <= n <= 30:
        forms.append(_roman(n))
    if n < len(NUMBER_WORDS):
        forms.append(NUMBER_WORDS[n])
    return forms


def _expand_aliases(toks: list[str]) -> list[list[str]]:
    """The token list, plus a copy with each known abbreviation swapped for
    its long form (or the long form for the abbreviation)."""
    variants = [toks]
    for short, long in ALIASES.items():
        lt = long.split()
        for v in list(variants):
            out, i, changed = [], 0, False
            while i < len(v):
                if v[i] == short:
                    out += lt
                    i, changed = i + 1, True
                elif v[i:i + len(lt)] == lt:
                    out.append(short)
                    i, changed = i + len(lt), True
                else:
                    out.append(v[i])
                    i += 1
            if changed and out not in variants:
                variants.append(out)
    return variants


def _edge_before(form: str) -> str:
    return r"(?<![a-z])" if form[0].isalpha() else r"(?<!\d)"


def _edge_after(form: str) -> str:
    return r"(?![a-z])" if form[-1].isalpha() else r"(?!\d)"


def _variant_regex(toks: list[str]) -> str:
    parts = []
    for i, tok in enumerate(toks):
        forms = _forms(tok, first=(i == 0))
        alts = []
        for f in forms:
            # Each letter of a short abbreviation may be followed by a space,
            # so "G.T.A." (normalised to "g t a") still matches "gta".
            body = r"\s?".join(re.escape(c) for c in f) if f.isalpha() and len(f) <= 4 and f in ALIASES else re.escape(f)
            pre = _edge_before(f) if i == 0 else ""
            post = _edge_after(f) if i == len(toks) - 1 else ""
            alts.append(pre + body + post)
        parts.append("(?:" + "|".join(alts) + ")")
    return r"\s?".join(parts)


@lru_cache(maxsize=256)
def compile_keyword(kw: str) -> re.Pattern | None:
    toks = tokens(kw)
    if not toks:
        return None
    return re.compile("|".join("(?:" + _variant_regex(v) + ")" for v in _expand_aliases(toks)))


def find(keywords: list[str], text: str) -> str | None:
    """The first keyword that `text` mentions, or None."""
    norm = normalize(text)
    if not norm:
        return None
    for kw in keywords:
        rx = compile_keyword(kw)
        if rx and rx.search(norm):
            return kw
    return None


def examples(kw: str, limit: int = 6) -> list[str]:
    """A few ways of writing the keyword that it also catches, for the
    Settings screen ('GTA 6', 'GTA VI', 'Grand Theft Auto 6', ...)."""
    out: list[str] = []
    for v in _expand_aliases(tokens(kw)):
        options = [_forms(t, i == 0) for i, t in enumerate(v)]
        # vary one number position at a time, so the list stays short
        base = [o[0] for o in options]
        combos = [base]
        for i, o in enumerate(options):
            for alt in o[1:]:
                combos.append(base[:i] + [alt] + base[i + 1:])
        for c in combos:
            words = [w.upper() if (w in ALIASES or w in ROMAN) and len(w) <= 4 else w.capitalize()
                     for w in c]
            s = " ".join(words)
            if s not in out:
                out.append(s)
    return out[:limit]


def signature(keywords: list[str]) -> str:
    """Changes whenever the effective keyword list changes."""
    return "|".join(sorted({" ".join(tokens(k)) for k in keywords if tokens(k)}))

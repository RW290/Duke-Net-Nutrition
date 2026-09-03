"""Dish grouping: cluster category headers into buildable multi-part dishes.

CBORD gives no field linking the categories of one build-your-own dish. A
"Sashimi Bowl" is spread across four separate category headers:

    Sashimi Bowl (Base- Choose One)
    Sashimi Bowl (Fish - Choose One)
    Sashimi Bowl (Choose Toppings)
    Sashimi Bowl Dressing

The only signal is shared words in the header text, so this module clusters
categories by their longest shared leading word-prefix. That is fuzzy text
matching, not a guaranteed key, so a manual override map (dish_overrides.json)
takes precedence for the cases the matcher gets wrong — e.g. Marketplace's
abbreviated "LL Salad Cheese" / "LL Sal Veg Etc", which belong to "Leaf and
Ladle" but share no prefix with it.

IMPORTANT: the "(Choose One)" / "(Choose up to Two)" text is a human-readable
hint only. CBORD enforces nothing — every item row, in every category, has its
own independent add button. `selectionHint` is surfaced for UI affordances but
must never be treated as a constraint.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Optional

# A dish must share at least this many leading words. Two words keeps
# "Sushi Rolls" and "Sushi Burrito" apart (they share only "sushi") while still
# grouping "1892 Grille" with "1892 Grille Toppings".
MIN_PREFIX_WORDS = 2

OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "dish_overrides.json")

_PAREN_RE = re.compile(r"\(([^)]*)\)")
# Strips the "choose" directive itself (and its quantity words) while leaving
# the noun intact: "Choose Toppings" -> "Toppings", "Base- Choose One" -> "Base".
_CHOOSE_RE = re.compile(
    r"\b(?:choose|select|pick)\b(?:\s+your)?(?:\s+up\s+to\s+[\w\d]+)?"
    r"(?:\s+(?:one|\d+))?", re.I)
# Menus write the count both ways: "Choose One" and "Choose 1".
_PICK_ONE_RE = re.compile(r"\bchoose\s+(?:your\s+)?(?:one|1)\b", re.I)
_UP_TO_RE = re.compile(r"\bup\s+to\b", re.I)
_PLURAL_NOUN_RE = re.compile(r"\btoppings\b|\bsauces\b|\bvegetables\b", re.I)


def normalize_header(header: str) -> list[str]:
    """Lowercase word tokens of a header, with parentheticals removed.

    '&' normalizes to 'and' so 'Salmon & Tuna Tower' and 'Salmon and Tuna Tower
    Toppings' cluster together — a real case in Duke's Gyotaku menu.

    Accents are stripped too: Duke's menus spell the same venue both ways in one
    menu ('Trinity Cafe Pizza' next to 'Trinity Café Bakery'), which otherwise
    splits one venue into two mismatched groups.
    """
    text = _PAREN_RE.sub(" ", header or "")
    text = text.replace("&", " and ")
    # Decompose accented characters and drop the combining marks (é -> e).
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    return [t for t in text.lower().split() if t]


def _parenthetical(header: str) -> Optional[str]:
    m = _PAREN_RE.search(header or "")
    return m.group(1).strip() if m else None


def selection_hint(header: str) -> str:
    """'pick_one' or 'pick_any', inferred from header text.

    A DISPLAY HINT ONLY — CBORD does not enforce it (see module docstring).
    """
    inner = _parenthetical(header) or header or ""
    # An explicit "Choose One" wins over any noun heuristic: the header
    # "(Toppings Choose One)" means one topping, despite the plural noun.
    if _PICK_ONE_RE.search(inner):
        return "pick_one"
    if _UP_TO_RE.search(inner) or _PLURAL_NOUN_RE.search(inner):
        return "pick_any"
    return "pick_any"


# Boilerplate that carries no choice information once the dish name is removed,
# so "Salmon Poke Bowl Toppings and Sauces" can reduce to just "Salmon".
_GENERIC_SECTION_WORDS = {"toppings", "topping", "sauces", "sauce", "and", "or",
                          "etc", "choose", "your", "own"}


def _word_key(word: str) -> str:
    """Lowercase, punctuation-free form of a word, for set comparisons.

    Without this, "Sandwich," (from "Sandwich, Wrap or Flatbread") fails to
    match the dish token "sandwich" and leaks a stray comma into the role.
    """
    return re.sub(r"[^\w]", "", word or "").lower()


def section_role(header: str, prefix_words: int,
                 dish_tokens: Optional[list[str]] = None) -> str:
    """The part this category plays in its dish: 'Base', 'Salmon', 'Toppings'...

    Preference order:
      1. the header OUTSIDE any parenthetical, minus the dish's own words, any
         "Choose ..." directive and generic boilerplate
         ("Buffalo Chicken (Choose Your Ingredients)" -> Buffalo Chicken)
      2. the parenthetical, minus its directive, used when (1) is empty because
         the header outside the parens was just the dish name
         ("Sashimi Bowl (Base - Choose One)" -> Base)
      3. 'Main'
    """
    # Candidate 1: the header text OUTSIDE any parenthetical, minus the dish's
    # own words and any "Choose ..." directive. This is preferred because it
    # carries the section's identity — "Buffalo Chicken (Choose Your
    # Ingredients)" must read as "Buffalo Chicken", not "Ingredients", or all 15
    # of Red Mango's presets end up with the same label.
    bare = _CHOOSE_RE.sub(" ", _PAREN_RE.sub(" ", header or ""))
    words = bare.replace("&", " and ").split()
    if dish_tokens:
        dish_set = set(dish_tokens)
        without_dish = [w for w in words if _word_key(w) not in dish_set]
        if len(without_dish) == len(words):
            # None of the dish's words appear here, so there is no boilerplate
            # to strip either — keeps "Additional Toppings" intact rather than
            # reducing it to "Additional".
            outside = " ".join(words).strip()
        else:
            outside = " ".join(
                w for w in without_dish
                if _word_key(w) not in _GENERIC_SECTION_WORDS).strip()
    else:
        outside = " ".join(words[prefix_words:]).strip()
    outside = outside.strip(" ,;:-–")
    if outside:
        return outside.title()

    # Candidate 2: the parenthetical, minus its "Choose ..." directive. Used
    # when the header outside the parens is just the dish name —
    # "Sashimi Bowl (Base - Choose One)" -> "Base".
    inner = _parenthetical(header)
    if inner:
        role = _CHOOSE_RE.sub(" ", inner)
        role = re.sub(r"[:\-–]", " ", role)
        role = re.sub(r"\s+", " ", role).strip(" -–:")
        if role:
            return role.title()

    return "Main"


def load_overrides(path: str = OVERRIDES_PATH) -> dict:
    """Load the manual categoryId -> dishName map. Missing file is fine."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    return data.get("categoryToDish", data if isinstance(data, dict) else {})


def load_standalone_units(path: str = OVERRIDES_PATH) -> set[str]:
    """Unit NAMES whose categories are menu sections, never build-your-own dishes.

    Some venues prefix every category with their own name ('Marine Lab Hot
    Buffet Entrees', 'Marine Lab Beverages', ...), which makes the matcher sweep
    the entire venue into one bogus dish. Listing the venue here is far more
    maintainable than a null entry per category.

    Keyed by name, not unit id: Duke reassigns unit ids whenever a location is
    added — adding "Red Mango" and "Sazon" mid-week shifted every id above them
    — so an id-keyed list silently starts pointing at the wrong venue.
    Comparison is case-insensitive and accent-insensitive.
    """
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        data = json.load(f)
    return {_venue_key(u) for u in data.get("standaloneUnits", [])}


def _venue_key(name: str) -> str:
    """Normalized venue name for matching (lowercase, accents stripped)."""
    return " ".join(normalize_header(name or ""))


def group_categories(categories: list[dict],
                     overrides: Optional[dict] = None,
                     unit_name: Optional[str] = None,
                     standalone_units: Optional[set[str]] = None) -> dict:
    """Cluster parsed menu categories into dishes.

    `categories` is the list from parsers.parse_menu()["categories"].
    `overrides` maps categoryId -> dish name and wins over the matcher.

    Returns:
        {
          "dishes": [
            {"dishName": "Sashimi Bowl",
             "source": "matched" | "override" | "mixed",
             "sections": [{"categoryId", "header", "role", "selectionHint",
                           "items": [...]}]},
            ...
          ],
          "standalone": [ ...categories that belong to no multi-part dish... ]
        }
    """
    overrides = overrides if overrides is not None else load_overrides()
    standalone_units = (standalone_units if standalone_units is not None
                        else load_standalone_units())
    if unit_name and _venue_key(unit_name) in standalone_units:
        # This venue is menu sections all the way down — no build-your-own.
        return {"dishes": [], "standalone": list(categories)}

    # 1. Overrides first — they are authoritative.
    #    A null/empty value pins the category as standalone: it is withheld from
    #    the matcher entirely. Needed where many unrelated categories share a
    #    prefix — every Freeman Café section starts with "Freeman Café", and
    #    without pinning the matcher sweeps the whole cafe into one fake dish.
    forced: dict[str, list[int]] = {}
    pinned_standalone: set[int] = set()
    remaining: list[int] = []
    for idx, cat in enumerate(categories):
        cid = str(cat.get("categoryId"))
        if cid in overrides:
            dish = overrides[cid]
            if dish:
                forced.setdefault(dish, []).append(idx)
            else:
                pinned_standalone.add(idx)
        else:
            remaining.append(idx)

    tokens = {idx: normalize_header(categories[idx].get("header", "")) for idx in remaining}

    # 2. Score every candidate prefix by how many categories share it.
    #    Rank by member count first, then length: for "Leaf and Ladle" the
    #    4-word prefix "leaf and ladle salad" is longer but covers fewer
    #    categories, and picking it would split one dish into two.
    counts: dict[tuple, list[int]] = {}
    for idx, toks in tokens.items():
        for n in range(MIN_PREFIX_WORDS, len(toks) + 1):
            counts.setdefault(tuple(toks[:n]), []).append(idx)

    candidates = sorted(
        ((prefix, members) for prefix, members in counts.items() if len(members) >= 2),
        key=lambda kv: (len(kv[1]), len(kv[0])), reverse=True,
    )

    assigned: dict[int, str] = {}
    groups: dict[str, list[int]] = {}
    for prefix, members in candidates:
        free = [i for i in members if i not in assigned]
        if len(free) < 2:
            continue
        dish_name = _display_name(categories, free, len(prefix))
        for i in free:
            assigned[i] = dish_name
        groups.setdefault(dish_name, []).extend(free)

    # 3. Assemble output, overrides merged in.
    dishes: list[dict] = []
    for dish_name, idxs in forced.items():
        extra: list[int] = list(groups.pop(dish_name, []))
        # An auto-matched group whose name merely EXTENDS an override's dish
        # name is a sub-variant of it, not a separate dish — absorb it. Real
        # case: on menus where the plain "Leaf and Ladle" category is absent,
        # the matcher names its group "Leaf And Ladle Salad", which would
        # otherwise split the station in two alongside the overridden categories.
        dish_toks = normalize_header(dish_name)
        for gname in [g for g in groups
                      if normalize_header(g)[:len(dish_toks)] == dish_toks]:
            extra.extend(groups.pop(gname))
        merged = sorted(set(idxs) | set(extra))
        dishes.append(_build_dish(dish_name, merged, categories,
                                  "override" if not extra else "mixed"))
    for dish_name, idxs in groups.items():
        dishes.append(_build_dish(dish_name, sorted(set(idxs)), categories, "matched"))

    grouped_idxs = {i for d in dishes for i in d["_idxs"]}
    standalone = [categories[i] for i in range(len(categories)) if i not in grouped_idxs]
    for d in dishes:
        d.pop("_idxs", None)

    dishes.sort(key=lambda d: d["dishName"].lower())
    return {"dishes": dishes, "standalone": standalone}


def _display_name(categories: list[dict], idxs: list[int], prefix_words: int) -> str:
    """Human-facing dish name, taken from the shortest member's original text
    so capitalization and spelling match what the menu actually shows."""
    header = min((categories[i].get("header", "") for i in idxs), key=len)
    words = _PAREN_RE.sub(" ", header).replace("&", " and ").split()
    return " ".join(words[:prefix_words]).strip().title()


def _build_dish(dish_name: str, idxs: list[int], categories: list[dict],
                source: str) -> dict:
    dish_tokens = normalize_header(dish_name)
    sections = []
    for i in idxs:
        cat = categories[i]
        header = cat.get("header", "")
        # Overridden categories often don't literally start with the dish name
        # (e.g. "LL Salad Cheese" under "Leaf And Ladle"). Only strip the prefix
        # when the header actually has it; otherwise the whole header is the role.
        has_prefix = normalize_header(header)[:len(dish_tokens)] == dish_tokens
        sections.append({
            "categoryId": cat.get("categoryId"),
            "header": header,
            "role": section_role(header, len(dish_tokens),
                                 dish_tokens=None if has_prefix else dish_tokens),
            "selectionHint": selection_hint(header),
            "items": cat.get("items", []),
        })
    return {"dishName": dish_name, "source": source, "sections": sections,
            "_idxs": idxs}

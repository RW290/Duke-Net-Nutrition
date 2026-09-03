"""HTML parsing layer for Duke NetNutrition (CBORD) responses.

CBORD does not expose a JSON data API. Every meaningful response is an HTML
fragment. This module turns those fragments into the normalized JSON shapes the
REST layer serves. Three parsers:

    parse_units(landing_html)      -> [{"id", "name"}]       (from GET /Duke)
    parse_menu(item_panel_html)    -> {"categories": [...]}  (from Menu/SelectMenu)
    parse_nutrition_label(html)    -> NutritionLabel dict    (from ShowItemNutritionLabel)

All selectors here were written against real captured markup in
tests/fixtures/, not against guesses. See tests/test_parsers.py.
"""
from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

# Nutrient value strings look like: "13g", "460mg", "3.74mg", "< 5mg", "NA", "".
# Capture an optional "<" qualifier, the number, and an optional unit.
_VALUE_RE = re.compile(r"^\s*(?P<lt><)?\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>mcg|mg|g|kcal)?\s*$", re.I)


def _text(node) -> str:
    """Collapsed text of a node, with non-breaking spaces normalized to spaces."""
    if node is None:
        return ""
    return node.get_text(" ", strip=True).replace("\xa0", " ").strip()


def parse_measure(raw: str) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Parse one nutrient value string.

    Returns (value, unit, qualifier):
      "13g"    -> (13.0,  "g",  None)
      "< 5mg"  -> (5.0,   "mg", "<")
      "NA"     -> (None,  None, None)   # NA/blank stay None; never coerced to 0
      ""       -> (None,  None, None)

    The qualifier ("<") is kept separate so a scaled value can preserve it.
    """
    raw = (raw or "").replace("\xa0", " ").strip()
    if not raw or raw.upper() == "NA":
        return None, None, None
    m = _VALUE_RE.match(raw)
    if not m:
        return None, None, None
    value = float(m.group("num"))
    if value.is_integer():
        value = int(value)
    return value, (m.group("unit") or None), ("<" if m.group("lt") else None)


# Maps the human label found on the FDA-2018 nutrition label to our schema key.
# Labels are matched case-insensitively after collapsing whitespace; the italic
# "<i>Trans</i> Fat" markup flattens to "Trans Fat".
_LABEL_TO_FIELD = {
    "total fat": "totalFatG",
    "saturated fat": "saturatedFatG",
    "trans fat": "transFatG",
    "cholesterol": "cholesterolMg",
    "sodium": "sodiumMg",
    "total carbohydrate": "totalCarbG",
    "dietary fiber": "dietaryFiberG",
    "total sugars": "totalSugarsG",
    "added sugars": "addedSugarsG",
    "protein": "proteinG",
    "calcium": "calciumMg",
    "iron": "ironMg",
    "potas.": "potassiumMg",
    "potassium": "potassiumMg",
}

# The numeric nutrient fields, in the order they appear on the label. The REST
# layer and the scaler both rely on this being the authoritative list of
# scalable fields.
NUTRIENT_FIELDS = [
    "calories", "totalFatG", "saturatedFatG", "transFatG", "cholesterolMg",
    "sodiumMg", "totalCarbG", "dietaryFiberG", "totalSugarsG", "addedSugarsG",
    "proteinG", "calciumMg", "ironMg", "potassiumMg",
]

# The only macros the app actually surfaces. The full label is still parsed
# (it's free once the HTML is in hand), but responses project down to these.
MACRO_FIELDS = ["calories", "proteinG", "totalFatG", "totalCarbG"]


_PORTION_RE = re.compile(r"^\s*([\d.]+)\s*(oz|lb|g)\b", re.I)
_GRAMS_PER = {"oz": 28.3495, "lb": 453.592, "g": 1.0}
# Flag when the label's gram weight and its stated portion disagree by more than
# this factor either way. 3x is well beyond rounding or a loose "about a cup".
_MISMATCH_FACTOR = 3.0


def detect_serving_mismatch(label: dict) -> Optional[dict]:
    """Flag labels whose gram weight contradicts their stated portion.

    Some CBORD entries carry the nutrition for a whole bulk package while still
    describing the portion as an ounce or two — Red Mango's "Yogurt Chips" reads
    "1 oz Portion (4536g)" with 22,680 calories, i.e. a 10-lb bag. Logging that
    as a serving is wildly wrong, so callers need to know the data is suspect.

    In every observed case the grams agree with the nutrition and the "N oz"
    text is the incorrect part, so `suggestedQuantity` is the multiplier that
    converts the label down to the portion it claims to describe. It is a
    suggestion only — the macros themselves are never silently rewritten.
    """
    text = label.get("servingSizeText") or ""
    grams = label.get("servingSizeGrams")
    m = _PORTION_RE.match(text)
    if not m or not grams:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    implied = value * _GRAMS_PER[unit]
    if implied <= 0:
        return None
    ratio = grams / implied
    if 1 / _MISMATCH_FACTOR <= ratio <= _MISMATCH_FACTOR:
        return None
    return {
        "type": "serving_size_mismatch",
        "message": (f"NetNutrition lists this as '{m.group(0).strip()}' but the "
                    f"label's nutrition is for {grams:g}g — about {ratio:.0f}x "
                    f"different. The macros below are for {grams:g}g."),
        "statedPortion": m.group(0).strip(),
        "impliedGrams": round(implied, 1),
        "labelGrams": grams,
        "ratio": round(ratio, 2),
        "suggestedQuantity": round(implied / grams, 6),
    }


def to_macros(label: dict) -> dict:
    """Project a full parsed label down to the four macros the app cares about,
    carrying along item identity, serving info, and any '<' qualifiers.
    Numeric values stay float|int|None (None == label said 'NA')."""
    out = {
        "name": label.get("name"),
        "servingSizeText": label.get("servingSizeText"),
        "servingSizeGrams": label.get("servingSizeGrams"),
        "calories": label.get("calories"),
        "proteinG": label.get("proteinG"),
        "totalFatG": label.get("totalFatG"),
        "totalCarbG": label.get("totalCarbG"),
        "qualifiers": {k: v for k, v in (label.get("qualifiers") or {}).items()
                       if k in MACRO_FIELDS},
    }
    warning = detect_serving_mismatch(label)
    if warning:
        out["dataWarning"] = warning
    return out


def _parse_serving_grams(serving_text: str) -> Optional[float]:
    """Pull the gram weight out of e.g. '5 oz Portion (171g)' -> 171.0."""
    m = re.search(r"\(([\d.]+)\s*g\)", serving_text or "")
    return float(m.group(1)) if m else None


def parse_nutrition_label(html: str) -> dict:
    """Parse a ShowItemNutritionLabel HTML fragment into a normalized dict.

    Output shape (numeric fields are float | int | None; None means the label
    said "NA" and MUST stay None through any downstream scaling):

        {
          "name": "Edamame",
          "servingSizeText": "5 oz Portion (171g)",
          "servingSizeGrams": 171.0,
          "servingsPerContainer": 1.0,
          "calories": 240, "totalFatG": 13, "saturatedFatG": None, ...,
          "qualifiers": {"cholesterolMg": "<"},   # value shown as "< 5mg"
          "percentDV": {"totalFatG": 20, "sodiumMg": 19, ...},
          "ingredients": "Edamame (Soybeans), ...",
          "contains": "Soy"
        }
    """
    soup = BeautifulSoup(html, "html.parser")
    label = soup.select_one("#nutritionLabel") or soup

    out: dict = {field: None for field in NUTRIENT_FIELDS}
    out["qualifiers"] = {}
    out["percentDV"] = {}

    out["name"] = _text(label.select_one(".cbo_nn_LabelHeader")) or None

    # Serving size + servings-per-container live in the bordered header block.
    border = label.select_one(".cbo_nn_LabelBottomBorderLabel")
    serving_text = None
    servings_per = None
    if border:
        right = border.select_one(".inline-div-right")
        serving_text = _text(right) or None
        spc_text = _text(border.select_one("span"))
        m = re.search(r"([\d.]+)", spc_text or "")
        servings_per = float(m.group(1)) if m else None
    out["servingSizeText"] = serving_text
    out["servingSizeGrams"] = _parse_serving_grams(serving_text or "")
    out["servingsPerContainer"] = servings_per

    # Calories sit in their own sub-header (not a bordered nutrient row).
    cal = label.select_one(".cbo_nn_LabelSubHeader .inline-div-right")
    cal_val, _, _ = parse_measure(_text(cal))
    out["calories"] = cal_val

    # Every other nutrient is a bordered/no-border sub-header row with a
    # left (label + value) and right (%DV) column. Potassium uses the
    # NoBorder variant, so match both.
    rows = label.select(".cbo_nn_LabelBorderedSubHeader, .cbo_nn_LabelNoBorderSubHeader")
    for row in rows:
        left = row.select_one(".inline-div-left")
        if left is None:
            continue

        # Added Sugars is special: a single span reading "Include <value> Added Sugars".
        if "addedSugarRow" in (left.get("class") or []):
            m = re.search(r"Include\s+(.*?)\s+Added Sugars", _text(left), re.I)
            value, unit, qual = parse_measure(m.group(1) if m else "")
            out["addedSugarsG"] = value
            if qual:
                out["qualifiers"]["addedSugarsG"] = qual
            pct = _pct(row)
            if pct is not None:
                out["percentDV"]["addedSugarsG"] = pct
            continue

        spans = left.find_all("span")
        if len(spans) < 2:
            continue
        label_txt = spans[0].get_text(" ", strip=True).replace("\xa0", " ").strip().lower()
        field = _LABEL_TO_FIELD.get(label_txt)
        if field is None:
            continue
        value, unit, qual = parse_measure(_text(spans[1]))
        out[field] = value
        if qual:
            out["qualifiers"][field] = qual
        pct = _pct(row)
        if pct is not None:
            out["percentDV"][field] = pct

    # Ingredients + allergen "Contains:" line.
    out["ingredients"] = _text(label.select_one(".cbo_nn_LabelIngredients")) or None
    out["contains"] = _text(label.select_one(".cbo_nn_LabelAllergens")) or None

    return out


def _pct(row) -> Optional[float]:
    """Parse the %DV number from a nutrient row's right column ('' or 'NA' -> None)."""
    right = row.select_one(".inline-div-right")
    txt = _text(right).replace("%", "").strip()
    if not txt or txt.upper() == "NA":
        return None
    m = re.search(r"[\d.]+", txt)
    return float(m.group(0)) if m else None


# --- Menu (item panel) parsing -------------------------------------------------

_DETAILOID_RE = re.compile(r"getItemNutritionLabelOnClick\(event,\s*(\d+)\)")
_CATEGORY_RE = re.compile(r"toggleCourseItems\(this,\s*(\d+)\)")


def parse_menu(item_panel_html: str) -> dict:
    """Parse the itemPanel HTML (from Menu/SelectMenu or the unit selection
    response) into ordered categories, each with its items.

        {
          "categories": [
            {
              "categoryId": "429",
              "header": "Soups, Sides and Drinks",
              "items": [
                {"detailOid": "292785194", "name": "Edamame",
                 "servingSizeText": "5 oz Portion", "allergens": ["Soy"]},
                ...
              ]
            },
            ...
          ]
        }

    Note: the on-page expand/collapse of a category is purely client-side; the
    server returns every item up front, so a single parse yields the whole menu.
    Category "(Choose One)" style labels are human hints only and are NOT
    enforced here (every item has an independent add button).
    """
    soup = BeautifulSoup(item_panel_html, "html.parser")
    categories: list[dict] = []
    current: Optional[dict] = None

    # Item rows are matched by the data-categoryid attribute rather than by CSS
    # class. CBORD zebra-stripes them across at least two classes
    # (cbo_nn_itemPrimaryRow / cbo_nn_itemAlternateRow); keying on the class
    # silently dropped every alternate row — about half of every menu. Group
    # header rows carry no data-categoryid, so the two selectors stay disjoint.
    for row in soup.select("tr.cbo_nn_itemGroupRow, tr[data-categoryid]"):
        classes = row.get("class") or []
        if "cbo_nn_itemGroupRow" in classes:
            onclick = row.get("onclick", "")
            m = _CATEGORY_RE.search(onclick)
            # Header text is the div's text minus the caret icon.
            header_div = row.select_one("td div")
            header = _text(header_div)
            current = {
                "categoryId": m.group(1) if m else None,
                "header": header,
                "items": [],
            }
            categories.append(current)
        else:  # item row
            item = _parse_item_row(row)
            if item is None:
                continue
            if current is None:
                # Item before any header (shouldn't happen); bucket it.
                current = {"categoryId": None, "header": "", "items": []}
                categories.append(current)
            current["items"].append(item)

    return {"categories": categories}


def _parse_item_row(row) -> Optional[dict]:
    link = row.select_one("a[onclick*='getItemNutritionLabelOnClick']")
    button = row.select_one("button[data-detailoid]")
    detail_oid = None
    if button and button.get("data-detailoid"):
        detail_oid = button["data-detailoid"]
    elif link:
        m = _DETAILOID_RE.search(link.get("onclick", ""))
        detail_oid = m.group(1) if m else None
    if detail_oid is None:
        return None

    # Item name is the link's own text, excluding the allergen <img> icons.
    name = ""
    allergens: list[str] = []
    if link:
        for img in link.select("img"):
            title = img.get("title")
            if title:
                allergens.append(title)
        name = link.get_text(" ", strip=True).replace("\xa0", " ").strip()

    # Serving size text is the 3rd cell (index 2) in the row.
    cells = row.find_all("td", recursive=False)
    serving = _text(cells[2]) if len(cells) >= 3 else None

    return {
        "detailOid": detail_oid,
        "categoryId": row.get("data-categoryid"),
        "name": name,
        "servingSizeText": serving,
        "allergens": allergens,
    }


# --- Menu-period (meal) parsing -----------------------------------------------

_MENU_OID_RE = re.compile(r"menuListSelectMenu\((\d+)\)")


def parse_menu_periods(menu_panel_html: str) -> list[dict]:
    """Parse the meal-period links a multi-period unit shows after selection.

    The menuPanel groups links under a date header:

        <header class='card-title h4'>Tuesday, August 18, 2026</header>
        <a class='cbo_nn_menuLink' onclick="...menuListSelectMenu(9323240)">Breakfast</a>

    Returns [{"menuOid", "name", "date"}], where menuOid is the id to pass to
    Menu/SelectMenu. Single-period units yield an empty list (their items come
    back directly in the itemPanel instead).
    """
    soup = BeautifulSoup(menu_panel_html, "html.parser")
    periods: list[dict] = []
    for a in soup.select("a[onclick*='menuListSelectMenu']"):
        m = _MENU_OID_RE.search(a.get("onclick", ""))
        if not m:
            continue
        # The date is the nearest preceding header in document order.
        date = None
        header = a.find_previous(class_="card-title") or a.find_previous("header")
        if header:
            date = _text(header)
        periods.append({"menuOid": m.group(1), "name": _text(a), "date": date})
    return periods


# --- Units (landing page) parsing ---------------------------------------------

_UNIT_RE = re.compile(r"unitsSelectUnit\((\d+)\)")


def parse_units(landing_html: str) -> list[dict]:
    """Parse the live dining-unit list from the GET /Duke landing page.

    Units are server-rendered as <a onclick="...unitsSelectUnit(N)">Name</a>.
    This is the authoritative live list; the app must never hardcode units.
    """
    soup = BeautifulSoup(landing_html, "html.parser")
    units: list[dict] = []
    seen = set()
    for a in soup.select("a[onclick*='unitsSelectUnit']"):
        m = _UNIT_RE.search(a.get("onclick", ""))
        if not m:
            continue
        unit_id = m.group(1)
        if unit_id in seen:
            continue
        seen.add(unit_id)
        units.append({"id": unit_id, "name": _text(a)})
    return units

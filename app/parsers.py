from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

_VALUE_RE = re.compile(r"^\s*(?P<lt><)?\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>mcg|mg|g|kcal)?\s*$", re.I)


def _text(node) -> str:
    if node is None:
        return ""
    return node.get_text(" ", strip=True).replace("\xa0", " ").strip()


def parse_measure(raw: str) -> tuple[Optional[float], Optional[str], Optional[str]]:
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

NUTRIENT_FIELDS = [
    "calories", "totalFatG", "saturatedFatG", "transFatG", "cholesterolMg",
    "sodiumMg", "totalCarbG", "dietaryFiberG", "totalSugarsG", "addedSugarsG",
    "proteinG", "calciumMg", "ironMg", "potassiumMg",
]

MACRO_FIELDS = ["calories", "proteinG", "totalFatG", "totalCarbG"]


_PORTION_RE = re.compile(r"^\s*([\d.]+)\s*(oz|lb|g)\b", re.I)
_GRAMS_PER = {"oz": 28.3495, "lb": 453.592, "g": 1.0}
_MISMATCH_FACTOR = 3.0


def detect_serving_mismatch(label: dict) -> Optional[dict]:
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
    m = re.search(r"\(([\d.]+)\s*g\)", serving_text or "")
    return float(m.group(1)) if m else None


def parse_nutrition_label(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    label = soup.select_one("#nutritionLabel") or soup

    out: dict = {field: None for field in NUTRIENT_FIELDS}
    out["qualifiers"] = {}
    out["percentDV"] = {}

    out["name"] = _text(label.select_one(".cbo_nn_LabelHeader")) or None

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

    cal = label.select_one(".cbo_nn_LabelSubHeader .inline-div-right")
    cal_val, _, _ = parse_measure(_text(cal))
    out["calories"] = cal_val

    rows = label.select(".cbo_nn_LabelBorderedSubHeader, .cbo_nn_LabelNoBorderSubHeader")
    for row in rows:
        left = row.select_one(".inline-div-left")
        if left is None:
            continue

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

    out["ingredients"] = _text(label.select_one(".cbo_nn_LabelIngredients")) or None
    out["contains"] = _text(label.select_one(".cbo_nn_LabelAllergens")) or None

    return out


def _pct(row) -> Optional[float]:
    right = row.select_one(".inline-div-right")
    txt = _text(right).replace("%", "").strip()
    if not txt or txt.upper() == "NA":
        return None
    m = re.search(r"[\d.]+", txt)
    return float(m.group(0)) if m else None



_DETAILOID_RE = re.compile(r"getItemNutritionLabelOnClick\(event,\s*(\d+)\)")
_CATEGORY_RE = re.compile(r"toggleCourseItems\(this,\s*(\d+)\)")


def parse_menu(item_panel_html: str) -> dict:
    soup = BeautifulSoup(item_panel_html, "html.parser")
    categories: list[dict] = []
    current: Optional[dict] = None

    for row in soup.select("tr.cbo_nn_itemGroupRow, tr[data-categoryid]"):
        classes = row.get("class") or []
        if "cbo_nn_itemGroupRow" in classes:
            onclick = row.get("onclick", "")
            m = _CATEGORY_RE.search(onclick)
            header_div = row.select_one("td div")
            header = _text(header_div)
            current = {
                "categoryId": m.group(1) if m else None,
                "header": header,
                "items": [],
            }
            categories.append(current)
        else:
            item = _parse_item_row(row)
            if item is None:
                continue
            if current is None:
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

    name = ""
    allergens: list[str] = []
    if link:
        for img in link.select("img"):
            title = img.get("title")
            if title:
                allergens.append(title)
        name = link.get_text(" ", strip=True).replace("\xa0", " ").strip()

    cells = row.find_all("td", recursive=False)
    serving = _text(cells[2]) if len(cells) >= 3 else None

    return {
        "detailOid": detail_oid,
        "categoryId": row.get("data-categoryid"),
        "name": name,
        "servingSizeText": serving,
        "allergens": allergens,
    }



_MENU_OID_RE = re.compile(r"menuListSelectMenu\((\d+)\)")


def parse_menu_periods(menu_panel_html: str) -> list[dict]:
    soup = BeautifulSoup(menu_panel_html, "html.parser")
    periods: list[dict] = []
    for a in soup.select("a[onclick*='menuListSelectMenu']"):
        m = _MENU_OID_RE.search(a.get("onclick", ""))
        if not m:
            continue
        date = None
        header = a.find_previous(class_="card-title") or a.find_previous("header")
        if header:
            date = _text(header)
        periods.append({"menuOid": m.group(1), "name": _text(a), "date": date})
    return periods



_UNIT_RE = re.compile(r"unitsSelectUnit\((\d+)\)")


def parse_units(landing_html: str) -> list[dict]:
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

"""Parser tests against REAL captured NetNutrition fixtures.

Fixtures (tests/fixtures/) were captured live from Duke's NetNutrition:
  nutrition_edamame.html  -> ShowItemNutritionLabel for Edamame (detailOid 292785194)
  menu_gyotaku.html       -> itemPanel from selecting the Gyotaku unit
"""
import os
import re

from app import parsers
from app.scaling import scale_macros

HERE = os.path.dirname(__file__)


def _fixture(name: str) -> str:
    with open(os.path.join(HERE, "fixtures", name)) as f:
        return f.read()


def test_parse_nutrition_label_edamame():
    label = parsers.parse_nutrition_label(_fixture("nutrition_edamame.html"))
    assert label["name"] == "Edamame"
    assert label["servingSizeText"] == "5 oz Portion (171g)"
    assert label["servingSizeGrams"] == 171.0
    assert label["servingsPerContainer"] == 1.0
    assert label["calories"] == 240
    assert label["totalFatG"] == 13
    assert label["totalCarbG"] == 15
    assert label["proteinG"] == 20
    assert label["sodiumMg"] == 460
    assert label["potassiumMg"] == 720          # NoBorder row must be captured
    assert label["ironMg"] == 3.74
    # NA fields stay None, never 0
    assert label["saturatedFatG"] is None
    assert label["transFatG"] is None
    assert label["addedSugarsG"] is None
    # "< 5mg" -> value 5 with a preserved "<" qualifier
    assert label["cholesterolMg"] == 5
    assert label["qualifiers"]["cholesterolMg"] == "<"
    assert label["contains"] == "Soy"
    assert "Edamame" in label["ingredients"]


def test_macros_projection_and_scaling():
    label = parsers.parse_nutrition_label(_fixture("nutrition_edamame.html"))
    macros = parsers.to_macros(label)
    assert set(macros) >= {"name", "calories", "proteinG", "totalFatG", "totalCarbG"}

    half = scale_macros(macros, 0.5)
    assert half["calories"] == 120
    assert half["proteinG"] == 10.0
    assert half["totalFatG"] == 6.5
    assert half["quantity"] == 0.5

    quarter = scale_macros(macros, 0.25)
    assert quarter["totalCarbG"] == 3.75

    # NA stays NA through the macro path too (fat/carb/protein all present here,
    # so assert on a NA field via the full label scaling contract):
    assert macros.get("qualifiers", {}) == {}  # no macro-level "<" for these 4


def test_parse_menu_captures_every_item_row():
    """Guards against silently dropping rows by CSS class.

    CBORD zebra-stripes item rows across cbo_nn_itemPrimaryRow and
    cbo_nn_itemAlternateRow. Parsing only the primary class dropped ~half of
    every menu (real bug: 'White Rice', 'Greens' and even 'Marinated Salmon'
    went missing). The invariant is that one item is parsed for every row
    carrying a data-categoryid, whatever its class.
    """
    html = _fixture("menu_gyotaku.html")
    expected = len(re.findall(r"data-categoryid", html))
    menu = parsers.parse_menu(html)
    parsed = sum(len(c["items"]) for c in menu["categories"])
    assert expected > 0
    assert parsed == expected, f"parsed {parsed} of {expected} item rows"
    # Both stripe classes really are present in the fixture, so this is a
    # meaningful check rather than a tautology.
    assert "cbo_nn_itemPrimaryRow" in html
    assert "cbo_nn_itemAlternateRow" in html


def test_parse_menu_gyotaku():
    menu = parsers.parse_menu(_fixture("menu_gyotaku.html"))
    cats = menu["categories"]
    assert len(cats) >= 5
    # First category header from the live capture.
    assert cats[0]["header"].startswith("Soups, Sides and Drinks")
    # Every item must have a detailOid and a name.
    all_items = [it for c in cats for it in c["items"]]
    assert all_items, "expected at least one parsed item"
    assert all(it["detailOid"] and it["name"] for it in all_items)
    # Edamame is in this menu with its allergen tag.
    edamame = next(it for it in all_items if it["name"] == "Edamame")
    assert edamame["detailOid"] == "292785194"
    assert "Soy" in edamame["allergens"]


def test_scale_preserves_none():
    base = {"name": "x", "servingSizeText": None, "servingSizeGrams": None,
            "calories": 100, "proteinG": None, "totalFatG": 2, "totalCarbG": None,
            "qualifiers": {}}
    out = scale_macros(base, 2)
    assert out["calories"] == 200
    assert out["proteinG"] is None      # NA stays NA
    assert out["totalCarbG"] is None
    assert out["totalFatG"] == 4


def test_detects_bulk_package_mislabelled_as_a_portion():
    """Real data bug: Red Mango's 'Yogurt Chips' label reads '1 oz Portion
    (4536g)' with 22,680 calories — the nutrition for a 10-lb bag."""
    label = {"servingSizeText": "1 oz Portion (4536g)", "servingSizeGrams": 4536.0}
    w = parsers.detect_serving_mismatch(label)
    assert w and w["type"] == "serving_size_mismatch"
    assert w["ratio"] == 160.0
    assert w["statedPortion"] == "1 oz"
    # Multiplying by suggestedQuantity yields the portion the label claims.
    assert round(4536.0 * w["suggestedQuantity"]) == 28


def test_normal_servings_are_not_flagged():
    for text, grams in [("5 oz Portion (171g)", 171.0),
                        ("6 oz Portion (170g)", 170.0),
                        ("2.5 oz Portion (59g)", 59.0)]:
        assert parsers.detect_serving_mismatch(
            {"servingSizeText": text, "servingSizeGrams": grams}) is None
    # No parseable weight in the text at all -> nothing to compare against.
    assert parsers.detect_serving_mismatch(
        {"servingSizeText": "Sandwich (161g)", "servingSizeGrams": 161.0}) is None


def test_macros_carry_the_warning_and_values_are_not_rewritten():
    label = parsers.parse_nutrition_label(_fixture("nutrition_edamame.html"))
    label["servingSizeText"] = "1 oz Portion (4536g)"
    label["servingSizeGrams"] = 4536.0
    macros = parsers.to_macros(label)
    assert "dataWarning" in macros
    # The macros themselves are untouched — we flag, we never silently correct.
    assert macros["calories"] == 240

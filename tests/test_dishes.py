"""Dish-grouping tests, using REAL category headers captured from Duke menus."""
from app.dishes import group_categories, normalize_header, section_role, selection_hint

# Real Gyotaku headers (unit 7).
GYOTAKU = [
    "Soups, Sides and Drinks",
    "Salmon & Tuna Tower (Choose Your Protein)",
    "Salmon and Tuna Tower Toppings (Choose Your Toppings)",
    "Sashimi & Nigiri",
    "Sushi Rolls",
    "Sushi Burrito",
    "Yubu Sushi",
    "Sashimi Bowl (Base- Choose One)",
    "Sashimi Bowl (Fish - Choose One)",
    "Sashimi Bowl (Choose Toppings)",
    "Sashimi Bowl Dressing",
    "Make Your Own Roll (Base)",
    "Make Your Own Roll (Inside: Protein/Veggies Choose up to Three)",
    "Make Your Own Roll (Toppings Choose One)",
    "Make Your Own Roll (Sauce Choose up to Two)",
]

# Real Marketplace Lunch headers, with their true categoryIds.
MARKETPLACE = [
    ("253", "1892 Grille"),
    ("961", "1892 Grille Toppings"),
    ("80", "Wood Fired"),
    ("264", "Cucina Deli Proteins"),
    ("4345", "Cucina Deli Breads and Wraps"),
    ("4346", "Cucina Deli Cheese"),
    ("232", "Leaf and Ladle"),
    ("4350", "Leaf and Ladle Salad Leafy Greens"),
    ("4351", "Leaf and Ladle Salad Protein"),
    ("4352", "LL Salad Cheese"),
    ("962", "Leaf and Ladle Salad Dressings"),
]


def _cats(headers):
    return [{"categoryId": str(i), "header": h, "items": []}
            for i, h in enumerate(headers)]


def _by_name(result):
    return {d["dishName"]: d for d in result["dishes"]}


def test_sashimi_bowl_groups_four_categories():
    dishes = _by_name(group_categories(_cats(GYOTAKU), overrides={}))
    bowl = dishes["Sashimi Bowl"]
    assert len(bowl["sections"]) == 4
    roles = [s["role"] for s in bowl["sections"]]
    assert roles == ["Base", "Fish", "Toppings", "Dressing"]
    # "(Base- Choose One)" is an explicit single choice.
    assert bowl["sections"][0]["selectionHint"] == "pick_one"


def test_ampersand_and_spelled_and_cluster_together():
    # "Salmon & Tuna Tower" vs "Salmon and Tuna Tower Toppings" — real menu text.
    dishes = _by_name(group_categories(_cats(GYOTAKU), overrides={}))
    assert len(dishes["Salmon And Tuna Tower"]["sections"]) == 2


def test_distinct_dishes_sharing_one_word_stay_separate():
    # "Sushi Rolls" / "Sushi Burrito" / "Yubu Sushi" are different dishes.
    result = group_categories(_cats(GYOTAKU), overrides={})
    standalone = {c["header"] for c in result["standalone"]}
    assert {"Sushi Rolls", "Sushi Burrito", "Yubu Sushi"} <= standalone


def test_numeric_choose_directive_is_handled():
    # Freeman writes "(Choose 1)" with a numeral rather than the word "one".
    # Before this was handled, both sandwich sections got the role "1".
    assert selection_hint("Freeman Café Sandwich Base (Choose 1)") == "pick_one"
    base = section_role("Freeman Café Sandwich Base (Choose 1)", 2,
                        dish_tokens=["freeman", "sandwich"])
    sides = section_role("Freeman Café Sandwich Sides (Choose 1)", 2,
                         dish_tokens=["freeman", "sandwich"])
    assert base != sides and "1" not in (base, sides)
    assert base.endswith("Base") and sides.endswith("Sides")


def test_explicit_choose_one_beats_plural_noun():
    # "(Toppings Choose One)" must be pick_one despite the plural noun.
    assert selection_hint("Make Your Own Roll (Toppings Choose One)") == "pick_one"
    assert selection_hint("Make Your Own Roll (Sauce Choose up to Two)") == "pick_any"
    assert selection_hint("Sashimi Bowl (Choose Toppings)") == "pick_any"


def test_role_keeps_noun_after_stripping_choose_directive():
    assert section_role("Sashimi Bowl (Choose Toppings)", 2) == "Toppings"
    assert section_role("Salmon & Tuna Tower (Choose Your Protein)", 4) == "Protein"
    assert section_role("Sashimi Bowl Dressing", 2) == "Dressing"
    assert section_role("Leaf and Ladle", 3) == "Main"


def test_leaf_and_ladle_not_split_by_longer_subprefix():
    # "Leaf and Ladle Salad ..." shares a longer prefix than "Leaf and Ladle",
    # but grouping must prefer the prefix covering the MOST categories so the
    # station stays one dish.
    cats = [{"categoryId": cid, "header": h, "items": []} for cid, h in MARKETPLACE]
    dishes = _by_name(group_categories(cats, overrides={}))
    assert "Leaf And Ladle" in dishes
    assert not any(d.startswith("Leaf And Ladle Salad") for d in dishes)
    headers = {s["header"] for s in dishes["Leaf And Ladle"]["sections"]}
    assert "Leaf and Ladle Salad Protein" in headers
    # Without an override, the abbreviated "LL ..." category can't be matched.
    assert "LL Salad Cheese" not in headers


def test_manual_override_merges_unmatched_category():
    cats = [{"categoryId": cid, "header": h, "items": []} for cid, h in MARKETPLACE]
    dishes = _by_name(group_categories(cats, overrides={"4352": "Leaf And Ladle"}))
    ll = dishes["Leaf And Ladle"]
    assert ll["source"] == "mixed"        # matcher + override combined
    headers = {s["header"] for s in ll["sections"]}
    assert "LL Salad Cheese" in headers
    # An overridden header that lacks the dish prefix keeps its own text as role.
    cheese = next(s for s in ll["sections"] if s["header"] == "LL Salad Cheese")
    assert cheese["role"] == "Ll Salad Cheese"


# Real Ginger + Soy preset-bowl headers (unit 6): a shared base plus one
# topping category per named bowl. No leading prefix ties them together.
GINGER_SOY_POKE = [
    ("1446", "Every Day Poke Bowl (Base - Choose One)"),
    ("1447", "Salmon Poke Bowl Toppings and Sauces"),
    ("1453", "Spicy Tuna Poke Bowl Toppings and Sauces"),
    ("1456", "Tuna Poke Bowl Toppings and Sauces"),
]


def test_preset_bowls_group_via_overrides():
    cats = [{"categoryId": cid, "header": h, "items": []}
            for cid, h in GINGER_SOY_POKE]
    overrides = {cid: "Poke Bowl" for cid, _ in GINGER_SOY_POKE}
    dishes = _by_name(group_categories(cats, overrides=overrides))
    poke = dishes["Poke Bowl"]
    assert len(poke["sections"]) == 4
    assert poke["source"] == "override"


def test_preset_bowl_roles_reduce_to_the_actual_choice():
    # "Salmon Poke Bowl Toppings and Sauces" under dish "Poke Bowl" should read
    # as just "Salmon" — that's the choice the user is making.
    assert section_role("Salmon Poke Bowl Toppings and Sauces", 2,
                        dish_tokens=["poke", "bowl"]) == "Salmon"
    assert section_role("Spicy Tuna Poke Bowl Toppings and Sauces", 2,
                        dish_tokens=["poke", "bowl"]) == "Spicy Tuna"
    assert section_role("Hong Kong Bowl Toppings and Sauces", 2,
                        dish_tokens=["rice", "bowl"]) == "Hong Kong"


def test_matcher_alone_mis_groups_the_two_every_day_bases():
    # Without overrides, "Every Day Poke Bowl ..." and "Every Day Rice Bowl ..."
    # share the prefix "every day" and wrongly merge. This documents WHY the
    # override entries exist — if the matcher ever improves, revisit them.
    cats = [
        {"categoryId": "1446", "header": "Every Day Poke Bowl (Base - Choose One)", "items": []},
        {"categoryId": "714", "header": "Every Day Rice Bowl Base (Choose One)", "items": []},
    ]
    dishes = _by_name(group_categories(cats, overrides={}))
    assert "Every Day" in dishes                      # the false positive
    # Overrides split them into the correct two dishes.
    fixed = _by_name(group_categories(
        cats, overrides={"1446": "Poke Bowl", "714": "Rice Bowl"}))
    assert set(fixed) == {"Poke Bowl", "Rice Bowl"}


def test_override_absorbs_matched_subvariant_group():
    # On menus lacking the plain "Leaf and Ladle" category, the matcher names
    # its group "Leaf And Ladle Salad". That must merge into the overridden
    # "Leaf And Ladle" dish rather than appearing as a second station.
    cats = [
        {"categoryId": "4352", "header": "LL Salad Cheese", "items": []},
        {"categoryId": "4350", "header": "Leaf and Ladle Salad Leafy Greens", "items": []},
        {"categoryId": "4351", "header": "Leaf and Ladle Salad Protein", "items": []},
        {"categoryId": "962", "header": "Leaf and Ladle Salad Dressings", "items": []},
    ]
    result = group_categories(cats, overrides={"4352": "Leaf And Ladle"})
    dishes = _by_name(result)
    assert set(dishes) == {"Leaf And Ladle"}, "sub-variant should not be its own dish"
    assert len(dishes["Leaf And Ladle"]["sections"]) == 4
    assert dishes["Leaf And Ladle"]["source"] == "mixed"
    assert result["standalone"] == []


# Real Freeman Café headers: every one is prefixed "Freeman Café", so the
# matcher sweeps the whole cafe into a single bogus 11-section dish.
FREEMAN = [
    ("1470", "Freeman Café Soups"),
    ("1471", "Freeman Café Salads"),
    ("1475", "Freeman Café Salad Add Ons"),
    ("1476", "Freeman Café Salad Dressings"),
    ("2413", "Freeman Café Hot Entreés"),
    ("1474", "Freeman Café Desserts"),
]


def test_shared_prefix_across_unrelated_sections_creates_a_bogus_dish():
    # Documents the failure mode that null-pinning exists to fix.
    cats = [{"categoryId": c, "header": h, "items": []} for c, h in FREEMAN]
    dishes = _by_name(group_categories(cats, overrides={}))
    assert any(len(d["sections"]) >= 5 for d in dishes.values()), \
        "expected the matcher to over-merge the cafe"


def test_null_override_pins_category_as_standalone():
    cats = [{"categoryId": c, "header": h, "items": []} for c, h in FREEMAN]
    overrides = {
        # the real salad builder
        "1471": "Freeman Salad", "1475": "Freeman Salad", "1476": "Freeman Salad",
        # everything else withheld from the matcher
        "1470": None, "2413": None, "1474": None,
    }
    result = group_categories(cats, overrides=overrides)
    dishes = _by_name(result)
    assert set(dishes) == {"Freeman Salad"}
    assert len(dishes["Freeman Salad"]["sections"]) == 3
    pinned = {c["header"] for c in result["standalone"]}
    assert pinned == {"Freeman Café Soups", "Freeman Café Hot Entreés",
                      "Freeman Café Desserts"}


def test_shipped_overrides_file_is_valid():
    # The real file must parse and contain only string->string entries.
    from app.dishes import load_overrides
    overrides = load_overrides()
    assert overrides, "expected seeded overrides"
    assert all(isinstance(k, str) and (v is None or isinstance(v, str))
               for k, v in overrides.items())
    # Ginger + Soy preset bowls and Ramen are covered.
    assert overrides.get("1447") == "Poke Bowl"
    assert overrides.get("1444") == "Rice Bowl"
    assert overrides.get("428") == "Ramen"


def test_normalize_header_strips_parentheticals_and_punctuation():
    assert normalize_header("Sashimi Bowl (Base- Choose One)") == ["sashimi", "bowl"]
    assert normalize_header("Salmon & Tuna Tower") == ["salmon", "and", "tuna", "tower"]


def test_accent_variants_normalize_together():
    # Duke spells the same venue both ways inside one menu.
    assert normalize_header("Trinity Café Bakery") == normalize_header("Trinity Cafe Bakery")
    cats = [
        {"categoryId": "1", "header": "Trinity Cafe Pizza", "items": []},
        {"categoryId": "2", "header": "Trinity Café Bakery", "items": []},
        {"categoryId": "3", "header": "Trinity Café Salads", "items": []},
    ]
    dishes = _by_name(group_categories(cats, overrides={}))
    # One group, not an accented/unaccented pair.
    assert len(dishes) == 1


def test_standalone_unit_disables_grouping_for_a_venue():
    cats = [
        {"categoryId": "1", "header": "Marine Lab Hot Buffet Entrees", "items": []},
        {"categoryId": "2", "header": "Marine Lab Hot Buffet Sides", "items": []},
        {"categoryId": "3", "header": "Marine Lab Beverages", "items": []},
    ]
    # Normally these over-merge into one bogus "Marine Lab" dish...
    assert _by_name(group_categories(cats, overrides={}))
    # ...but listing the unit returns everything standalone.
    result = group_categories(cats, overrides={}, unit_name="Duke Marine Lab",
                              standalone_units={"duke marine lab"})
    assert result["dishes"] == []
    assert len(result["standalone"]) == 3
    # A different venue is unaffected.
    other = group_categories(cats, overrides={}, unit_name="The Farmstead",
                             standalone_units={"duke marine lab"})
    assert other["dishes"]
    # Matching ignores case and accents, since venue names are spelled loosely.
    accented = group_categories(cats, overrides={}, unit_name="Trinity Café",
                                standalone_units={"trinity cafe"})
    assert accented["dishes"] == []


def test_shipped_standalone_units_load():
    from app.dishes import load_standalone_units
    units = load_standalone_units()
    # Stored as names and normalized on load — never raw unit ids, which shift
    # whenever Duke adds a location.
    assert "duke marine lab" in units
    assert "trinity cafe" in units
    assert not any(u.isdigit() for u in units)


# Real Red Mango headers: one shared bread category plus many named presets that
# all end in the same generic "(Choose Your Ingredients)".
def test_named_presets_keep_their_own_identity_as_role():
    dish = ["sandwich", "wrap", "or", "flatbread"]
    assert section_role("Buffalo Chicken (Choose Your Ingredients)", 4,
                        dish_tokens=dish) == "Buffalo Chicken"
    assert section_role("Caprese (Choose Your Ingredients)", 4,
                        dish_tokens=dish) == "Caprese"
    # Distinct labels — the bug was every preset rendering as "Ingredients".
    roles = {section_role(h, 4, dish_tokens=dish) for h in [
        "Buffalo Chicken (Choose Your Ingredients)",
        "Caprese (Choose Your Ingredients)",
        "Philly Special (Choose Your Ingredients)"]}
    assert len(roles) == 3
    # The shared base is only the dish name, so it falls back to the parenthetical.
    assert section_role("Sandwich, Wrap or Flatbread (Choose Your Bread)", 4,
                        dish_tokens=dish) == "Bread"


def test_plain_choose_headers_drop_the_directive():
    # Sazon names sections "Choose Base" / "Choose Protein" with no parenthetical.
    dish = ["sazon", "build", "your", "own"]
    assert section_role("Choose Base", 4, dish_tokens=dish) == "Base"
    assert section_role("Choose Protein", 4, dish_tokens=dish) == "Protein"

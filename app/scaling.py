"""Fractional / arbitrary-quantity nutrition scaling.

CBORD's own site only offers whole-number serving multiples (1x-5x) and its
nutrition endpoint takes no quantity parameter at all. Nutrition values scale
linearly with quantity, so we fetch each item's label once as the "1x" base and
do all scaling here, client of CBORD but server of our own API. This lets us
support any decimal quantity (0.5, 0.25, 1.5, 2.25, ...), which is strictly more
flexible than CBORD.

Rules:
  * Numeric macro values are multiplied by the quantity.
  * A None value means the label said "NA" -> it stays None, never becomes 0.
  * '<' qualifiers (e.g. cholesterol "< 5mg") are preserved through scaling.
"""
from __future__ import annotations

from .parsers import MACRO_FIELDS

_PASSTHROUGH = {"name", "servingSizeText", "servingSizeGrams", "qualifiers",
                "dataWarning"}


def scale_macros(base: dict, quantity: float, ndigits: int = 2) -> dict:
    """Return a copy of a macro dict with numeric fields multiplied by quantity.

    `base` is the shape produced by parsers.to_macros(). NA (None) fields are
    left None. The result echoes the quantity used.
    """
    if quantity < 0:
        raise ValueError("quantity must be >= 0")

    out: dict = {}
    for key, value in base.items():
        if key in _PASSTHROUGH:
            out[key] = value
        elif key in MACRO_FIELDS:
            out[key] = None if value is None else round(value * quantity, ndigits)
        else:
            out[key] = value
    out["quantity"] = quantity
    return out


def sum_macros(components: list[dict], ndigits: int = 2) -> dict:
    """Sum already-scaled component macros into one total.

    This is how a "custom bowl" total is produced: Duke's NetNutrition has no
    build-your-own endpoint and no modifier-group concept — every component
    (base, protein, sauce, topping) is an independent item with its own
    detailOid. There is no CBORD-computed total to relay, so summing the
    individually-scaled components is the only available mechanism.

    NA handling: a component whose field is None (label said "NA") contributes
    nothing to that field's sum, but the field is recorded in `incomplete` so
    the total is never silently understated as if the value were a real 0. A
    field that is None in EVERY component stays None rather than becoming 0.
    """
    totals: dict[str, float] = {f: 0.0 for f in MACRO_FIELDS}
    known: dict[str, int] = {f: 0 for f in MACRO_FIELDS}
    incomplete: list[str] = []

    for comp in components:
        for field in MACRO_FIELDS:
            value = comp.get(field)
            if value is None:
                if field not in incomplete:
                    incomplete.append(field)
            else:
                totals[field] += value
                known[field] += 1

    out: dict = {}
    for field in MACRO_FIELDS:
        # No component supplied a real value -> the total is unknown, not zero.
        out[field] = round(totals[field], ndigits) if known[field] else None

    out["componentCount"] = len(components)
    # Fields where at least one component was NA, so the total is a floor, not exact.
    out["incomplete"] = [f for f in incomplete if out[f] is not None]
    # Surface any component whose upstream label is self-contradictory, so a
    # total built on suspect data is never presented as though it were solid.
    suspect = [{"itemName": c.get("itemName") or c.get("name"),
                **c["dataWarning"]}
               for c in components if c.get("dataWarning")]
    if suspect:
        out["dataWarnings"] = suspect
    return out

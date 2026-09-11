from __future__ import annotations

from .parsers import MACRO_FIELDS

_PASSTHROUGH = {"name", "servingSizeText", "servingSizeGrams", "qualifiers",
                "dataWarning"}


def scale_macros(base: dict, quantity: float, ndigits: int = 2) -> dict:
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
        out[field] = round(totals[field], ndigits) if known[field] else None

    out["componentCount"] = len(components)
    out["incomplete"] = [f for f in incomplete if out[f] is not None]
    suspect = [{"itemName": c.get("itemName") or c.get("name"),
                **c["dataWarning"]}
               for c in components if c.get("dataWarning")]
    if suspect:
        out["dataWarnings"] = suspect
    return out

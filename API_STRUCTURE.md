# Duke Nutrition API Structure

## Overview

This project is a FastAPI backend for Duke dining nutrition data and meal
logging. It fetches dining units, menus, and nutrition labels from Duke's
CBORD NetNutrition service, then exposes normalized JSON endpoints for a
separate frontend such as a Replit app.

The Replit frontend is deployed at
`https://duke-nutrition-tracker.replit.app`. The FastAPI backend needs its own
public URL unless both services are configured in the same Replit deployment.
FastAPI's interactive documentation is available at `<API_BASE_URL>/docs`.

## Main Resources

| Resource | Purpose |
| --- | --- |
| `units` | Current dining locations from Duke NetNutrition |
| `menus` | Meal periods or direct menu items for a location |
| `dishes` | Grouped, buildable dishes for a location |
| `items` | Menu item details and nutrition labels |
| `meals` | Scale multiple components and calculate totals |
| `log` | Store and retrieve logged meals |

## Endpoints

### Health

`GET /health`

Returns:

```json
{"status": "ok"}
```

### Dining Units

`GET /units`

Returns the live list of locations. The list is never hardcoded.

```json
{
  "units": [
    {"id": "6", "name": "Bella Union"}
  ]
}
```

Use `GET /units?refresh=true` to bypass the units cache.

`GET /units/lookup?name=Bella%20Union`

Returns an exact or partial match. An unknown location returns HTTP 200 with
`found: false` and `fallback: "manual_entry"`.

### Menus and Dishes

`GET /units/{unitId}/menus`

A location can return either meal periods or direct items:

```json
{
  "unit": {"id": "6", "name": "Bella Union"},
  "periods": [
    {"menuOid": "123", "name": "Lunch", "date": "2026-09-03"}
  ],
  "directItems": []
}
```

For single-period locations, `periods` may be empty and `directItems` contains
the menu items.

`GET /units/{unitId}/dishes`

Returns grouped dishes for locations whose menu is organized by categories.

`GET /menus/{menuOid}/items`

Returns the complete menu for a meal period, including categories and items.

### Nutrition

`GET /items/{detailOid}/nutrition?menuOid={menuOid}`

Returns normalized nutrition for one item. Use `unitId` instead of `menuOid`
when the item belongs to a direct-item location.

The primary nutrition fields are:

```json
{
  "calories": 250,
  "proteinG": 12,
  "totalFatG": 8,
  "totalCarbG": 30
}
```

`POST /items/{detailOid}/scale`

Request body:

```json
{"quantity": 0.5, "menuOid": "123"}
```

Returns the item's macros multiplied by `quantity`.

### Meal Calculation

`POST /meals/compute`

Request body:

```json
{
  "components": [
    {"detailOid": "456", "menuOid": "123", "quantity": 1},
    {"manualName": "Apple", "calories": 95, "proteinG": 0.5,
     "totalFatG": 0.3, "totalCarbG": 25, "quantity": 1}
  ]
}
```

Each component is scaled independently. The response contains the resolved
components and a `totalNutrition` object.

### Meal Log

`POST /log`

Accepts the same `components` structure as `/meals/compute`, plus an optional
`label` and ISO-8601 `timestamp`. The server stores the nutrition values at log
time.

`GET /log?date=YYYY-MM-DD`

Returns saved entries for a date, or all entries when `date` is omitted. It
also includes `dayTotal`.

`DELETE /log/{entryId}`

Deletes one saved meal entry.

### Cache

`POST /cache/clear`

Clears cached data. Pass an optional `prefix` query parameter, such as
`menus:` or `menu_items:`. Use this when Duke changes a menu and an immediate
refetch is needed.

## Integration Notes

- CORS is enabled by default so a browser frontend hosted on Replit can call
the API directly.
- Always load locations from `GET /units`; do not hardcode restaurant names or
IDs because Duke can add, remove, or reassign them.
- Treat `periods` and `directItems` as alternative menu shapes.
- Use `detailOid` together with its `menuOid` or `unitId` when requesting
nutrition.
- Four macro fields matter to the frontend: calories, protein, fat, and carbs.
- The API has no authentication or API keys.

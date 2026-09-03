# Replit prompt

Copy everything below the line into Replit.

---

Build a mobile-first PWA (installable to an iPhone home screen, no app store) for
logging what I eat at Duke dining halls and tracking macros. The backend already
exists and is live on Replit — do NOT build a backend, scrape anything, or
hardcode menu data. Set the API base URL to the deployed Replit backend and
call this API for everything:

The frontend is deployed at `https://duke-nutrition-tracker.replit.app`.
Configure `API_BASE_URL` with the separate public URL for this FastAPI backend;
do not use the frontend URL as the API URL unless both services run together.
Interactive docs: `${API_BASE_URL}/docs`

Only four macros matter: **Calories, Protein, Fat, Carbs**. CORS is already open,
so browser `fetch` works directly. No API key, no auth.

## Core flow

1. **Pick a dining hall** — `GET /units` → `{units: [{id, name}]}`.
   Always fetch this live. Never hardcode the list; Duke adds and renames places.

2. **Pick a meal period** — `GET /units/{unitId}/menus`.
   **Two possible shapes, you must handle both:**
   - `periods` is non-empty → multi-period hall (most of them). Show the
     periods; each has a `menuOid` and a `date`. Note some halls list several
     DAYS of periods, so group by `date` and default to today. Then load items
     with `GET /menus/{menuOid}/dishes`.
   - `periods` is empty and `directItems` is set → single-period place (e.g.
     Ginger + Soy, Gyotaku). There is no period to pick; the menu is already
     there. Use `GET /units/{unitId}/dishes` for the organized version.
   Decide by inspecting the response, not by remembering which hall is which —
   Duke changes this.

   If you only handle one shape, several dining halls will silently render empty.

3. **Show the menu, organized** — the `/dishes` endpoints return:
   ```json
   {
     "dishes": [
       { "dishName": "Poke Bowl",
         "sections": [
           { "role": "Base", "selectionHint": "pick_one", "header": "...",
             "categoryId": "1446",
             "items": [ {"detailOid": "...", "name": "Brown Rice",
                         "servingSizeText": "...", "allergens": ["Soy"]} ] },
           { "role": "Salmon", "selectionHint": "pick_any", "items": [...] }
         ] }
     ],
     "standalone": [ { "header": "Pad Thai", "items": [...] } ]
   }
   ```
   - `dishes` are build-your-own / multi-part dishes → render as a guided builder.
   - `standalone` are ordinary categories → render as a simple list.
   - `selectionHint` is `pick_one` (radio) or `pick_any` (checkboxes). Treat it as
     a **UI hint only** — the server does not enforce it, so never block a
     selection because of it.
   - Show `allergens` as small tags on each item.

4. **Get macros / totals** — never compute nutrition yourself. Send the selected
   components to `POST /meals/compute`:
   ```json
   { "components": [
       {"detailOid": "<from menu>", "quantity": 1,   "unitId": "<from /units>"},
       {"detailOid": "<from menu>", "quantity": 0.5, "unitId": "<from /units>"}
   ] }
   ```
   Returns each component's scaled macros plus `totalNutrition`. Call this
   (debounced ~300ms) whenever a selection or quantity changes, and show a
   **live-updating total pinned to the bottom of the screen**.

   Pass context with every component: `unitId` for single-period places, or
   `menuOid` for multi-period ones — take both from the responses above.
   **Never hardcode a unit id.** Duke's unit ids get reassigned when the roster
   changes (a semester rollover moved Ginger + Soy from 6 to 10 and Gyotaku from
   7 to 11), so always use the id from a fresh `GET /units`.

5. **Log it** — `POST /log` with the same `components` array plus an optional
   `label` (e.g. "Poke bowl, lunch"). Read the day back with
   `GET /log?date=YYYY-MM-DD`, which also returns a `dayTotal`. Show a running
   daily log I can scroll through, with per-entry breakdowns and a delete button
   (`DELETE /log/{entryId}`).

## Customization — the important part

Ginger + Soy serves preset bowls, but the API exposes every component
separately, so I want to **start from the preset and then modify it**. Build this
generally so it works for any dish, but Ginger + Soy is the case to get right.

When I open a preset bowl (e.g. **Poke Bowl → Salmon**):

- **Default to "as served"**: pre-select every item in that section, plus one base.
  The total should immediately reflect the standard bowl before I touch anything.
- **Removals**: unchecking a pre-selected item removes it from the total. Show
  removed items struck through / greyed rather than hiding them, so I can see what
  I took out and put it back easily.
- **Additions**: let me add any item from *other* sections of the same dish
  (e.g. add Spicy Mayo from the Spicy Tuna section onto a Salmon bowl), and from
  the same hall's other dishes/standalone categories. An "Add ingredient" button
  opening a searchable list of that hall's items is fine.
- **Half / partial portions**: every selected item needs its own quantity control,
  independent of the others. Offer quick buttons for **½, 1, 1½, 2** plus a way to
  enter any decimal (0.25, 0.75, 1.25…). This must be per-item — "half the rice,
  full protein, 1.5× sauce" is the main thing I want. Just send that number as the
  component's `quantity`; the backend does the math.
- Show a live per-item macro line next to each selection so I can see what a
  change costs, alongside the running total.

Concretely, a customized Salmon Poke Bowl posts as (ids illustrative — always use
ids from a fresh menu fetch, see the rules below):
```json
{ "components": [
  {"detailOid":"<Seasoned Rice>", "quantity":0.5, "unitId":"<id>"},  // ½ portion
  {"detailOid":"<Seaweed Salad>", "quantity":1,   "unitId":"<id>"},
  {"detailOid":"<Eel Sauce>",     "quantity":1.5, "unitId":"<id>"},  // extra sauce
  {"detailOid":"<Spicy Mayo>",    "quantity":1,   "unitId":"<id>"}   // added from
                                                                  // another bowl
] }
```
Pickled Carrot / Bubu Arare / Lemonaise Dill Sauce simply aren't in the array —
that is what "removed" means. A real response for the first three above totals
212.5 cal / 5.5g protein / 3g fat / 40.5g carbs.

Sections can be long — Ginger + Soy's Poke Bowl base offers Brown Rice, Greens,
Seasoned Rice and White Rice, and each bowl's topping section carries ~10 items
including the protein (Marinated Salmon, Spicy Tuna, Marinated Tuna, Beef
Bulgogi…). Make these scrollable/searchable rather than assuming a handful.

## Manual entry (required, not optional)

Not every campus spot is in NetNutrition, and off-campus food never is. A log
component can instead carry a manual item — no `detailOid`:
```json
{"manualName":"Chicken burrito","quantity":1,
 "calories":800,"proteinG":45,"totalFatG":30,"totalCarbG":85}
```
Use `GET /units/lookup?name=...` to check a place: it returns HTTP **200** with
`found:false` and `fallback:"manual_entry"` when NetNutrition doesn't cover it.
That is a normal path — route straight to the manual form, never show an error.

Manual and API-sourced components can be mixed in one logged meal.

## Rules that will bite you if ignored

- **Never persist a `detailOid`.** CBORD reissues every item id when the menu
  rolls over (which happens in the evening, not at midnight) — the same
  "Seasoned Rice" has a different id tomorrow. Always use ids from a fresh menu
  fetch, and re-fetch the menu when the user starts building rather than reusing
  ids from an earlier session. For favorites/recents, store the item **name** and
  re-resolve it against today's menu.
  A **`410` with `error:"stale_detail_oid"`** means the ids you're holding have
  rotated. The server has already dropped its stale cache by the time you see it,
  so the correct handling is: re-fetch the menu, map your selections back by
  item **name**, and retry — silently if you can. Do not show a crash.
- **Pass `unit.id`, never the array index.** The list is alphabetical by name
  while the ids are CBORD's own and are not in list order — using the position
  sends you to the wrong hall (a real bug I already hit).
- **Never recompute logged totals.** `GET /log` returns the totals as they were
  saved; display them verbatim. Menus change and past entries must not drift.
- **Handle `dataWarning`.** Some NetNutrition entries carry the nutrition for a
  whole bulk package while calling it a small portion — Red Mango's "Yogurt
  Chips" reads "1 oz Portion (4536g)" with **22,680 calories** (a 10-lb bag).
  When an item or a total includes `dataWarning` / `dataWarnings`, show a clear
  warning instead of logging the number as-is. The warning carries
  `suggestedQuantity`, the multiplier that converts the label down to the
  portion it claims to describe (0.00625 → 142 cal for 1 oz). Offer that as a
  one-tap fix, and let me override it by hand. Never apply it silently — the
  server deliberately does not, because it is a guess about which of CBORD's two
  contradictory fields is wrong.
- A macro can be `null` — that means NetNutrition listed it as "NA". Render it as
  "—", never as 0. Totals include an `incomplete` array naming any macro where
  some component was NA; show a small footnote when it's non-empty.
- Menus are cached ~6h server-side. Add `?refresh=true` behind a pull-to-refresh
  gesture, not on every load.

## UX

Mobile-first, thumb-reachable, works one-handed while standing in a dining hall.
Installable PWA: manifest + service worker + icons. Persist in-progress bowl
state locally so a refresh doesn't lose it. Show a clear loading state — the
first call after idle can take a couple of seconds while the backend wakes.

Suggested screens: Today's log (home, with day totals) → pick hall → pick period
(only when the API returns `periods`) → dish/category list → dish builder with
live total → confirm & log. Plus a manual-entry screen reachable from anywhere.

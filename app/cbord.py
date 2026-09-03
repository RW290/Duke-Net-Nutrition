"""CBORD / NetNutrition session wrapper for Duke dining.

Replicates the browser's request flow against the real endpoints confirmed by
live capture (see README "Confirmed API"):

    GET  /nn-prod/Duke                              establish ASP.NET session
    POST /nn-prod/Duke/Unit/SelectUnitFromUnitsList body: unitOid=<n>
    POST /nn-prod/Duke/Menu/SelectMenu             body: menuOid=<n>
    POST /nn-prod/Duke/NutritionDetail/ShowItemNutritionLabel  body: detailOid=<n>

Two facts drive the design:

1. The session is STATEFUL. The server tracks the currently-loaded unit/menu.
   ShowItemNutritionLabel(detailOid) only succeeds if that item's menu is the
   one currently loaded in the session; otherwise it returns an error panel.
   So fetching an item's nutrition means: select its menu, THEN ask for the
   label. Callers pass the menuOid alongside the detailOid for this reason.

2. Because there is one shared server-side selection, concurrent requests could
   clobber each other's "currently loaded menu." This is a personal, low-traffic
   app, so we serialize the whole select-then-read critical section behind a
   single lock rather than maintaining a pool of sessions. Simple and correct;
   the tradeoff is no parallelism against CBORD, which does not matter at one user.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import requests

logger = logging.getLogger("cbord")

BASE = "https://netnutrition.cbord.com/nn-prod/Duke"
_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


class SessionExpired(Exception):
    """Raised internally when a response looks like the session lapsed."""


class CbordError(Exception):
    """Raised when CBORD returns an error panel or an unexpected response."""


class CbordClient:
    """Thread-safe wrapper around a single NetNutrition session.

    Public methods each acquire the instance lock so the stateful
    select-then-read sequences never interleave.
    """

    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout
        self._lock = threading.RLock()
        self._session: Optional[requests.Session] = None

    # -- session lifecycle ------------------------------------------------------

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(_HEADERS)
        # The landing GET issues the Set-Cookie (ASP.NET_SessionId) that every
        # later POST relies on. requests' cookie jar carries it automatically.
        r = s.get(BASE, timeout=self._timeout)
        r.raise_for_status()
        self._landing_html = r.text
        logger.info("established new CBORD session")
        return s

    def _ensure_session(self) -> requests.Session:
        if self._session is None:
            self._session = self._new_session()
        return self._session

    def reset(self) -> None:
        """Drop the current session so the next call re-establishes it."""
        with self._lock:
            self._session = None

    # -- low-level POST with transparent re-establish ---------------------------

    def _post(self, path: str, data: dict, *, expect_json: bool, _retry: bool = True):
        """POST a form body, retrying once on a detected session lapse.

        expect_json=True  -> returns the parsed panel envelope dict.
        expect_json=False -> returns response text (raw HTML fragment).
        """
        session = self._ensure_session()
        url = f"{BASE}/{path}"
        try:
            r = session.post(url, data=data, timeout=self._timeout)
            r.raise_for_status()
            if expect_json:
                payload = r.json()
                # A lapsed session typically stops returning success envelopes.
                if not isinstance(payload, dict) or not payload.get("success", False):
                    raise SessionExpired(f"non-success envelope from {path}")
                return payload
            return r.text
        except (SessionExpired, requests.exceptions.JSONDecodeError) as exc:
            if _retry:
                logger.warning("re-establishing session after: %s", exc)
                self._session = None
                return self._post(path, data, expect_json=expect_json, _retry=False)
            raise CbordError(f"{path} failed after re-establishing session: {exc}") from exc

    @staticmethod
    def _panel(envelope: dict, panel_id: str) -> str:
        for panel in envelope.get("panels", []):
            if panel.get("id") == panel_id:
                return panel.get("html", "")
        return ""

    # -- public API -------------------------------------------------------------

    def get_landing_html(self) -> str:
        """Return the landing-page HTML (units are server-rendered there)."""
        with self._lock:
            self._ensure_session()
            return self._landing_html

    def select_unit(self, unit_oid: str) -> dict:
        """Select a dining unit; return both relevant panels' HTML.

        Multi-period units put meal-period (menuListSelectMenu) links in
        menuPanel with an empty itemPanel; single-period units return the menu
        items directly in itemPanel. Callers inspect both.
        """
        with self._lock:
            env = self._post("Unit/SelectUnitFromUnitsList",
                             {"unitOid": str(unit_oid)}, expect_json=True)
            return {
                "itemPanelHtml": self._panel(env, "itemPanel"),
                "menuPanelHtml": self._panel(env, "menuPanel"),
            }

    def select_menu(self, menu_oid: str, unit_oid: Optional[str] = None) -> str:
        """Select a specific date+meal menu; return its itemPanel HTML.

        SelectMenu requires the menu's unit to be selected in the session first
        — on a fresh session it returns a non-success envelope. We optimistically
        try the menu directly (it succeeds whenever the right unit happens to be
        loaded, which is the common case) and, on failure, select the unit and
        retry. Pass unit_oid whenever it's known; without it there's no recovery.
        """
        with self._lock:
            try:
                env = self._post("Menu/SelectMenu",
                                 {"menuOid": str(menu_oid)}, expect_json=True)
                return self._panel(env, "itemPanel")
            except CbordError:
                if unit_oid is None:
                    raise
                logger.info("SelectMenu(%s) failed; selecting unit %s and retrying",
                            menu_oid, unit_oid)
            self.select_unit(unit_oid)
            env = self._post("Menu/SelectMenu",
                             {"menuOid": str(menu_oid)}, expect_json=True)
            return self._panel(env, "itemPanel")

    def nutrition_label_html(self, detail_oid: str,
                             menu_oid: Optional[str] = None,
                             unit_oid: Optional[str] = None) -> str:
        """Return the raw nutrition-label HTML for an item.

        The call is session-stateful: the item's menu must be loaded first. Pass
        whichever context brings the item into the session:
          * menu_oid  -> multi-period units (Menu/SelectMenu)
          * unit_oid  -> single-period units, whose items load on unit selection
        If both are given, menu_oid wins. If the response is an error panel
        (item not in the loaded menu), raise CbordError.
        """
        with self._lock:
            if menu_oid is not None:
                self.select_menu(menu_oid, unit_oid=unit_oid)
            elif unit_oid is not None:
                self.select_unit(unit_oid)
            html = self._post("NutritionDetail/ShowItemNutritionLabel",
                              {"detailOid": str(detail_oid)}, expect_json=False)
            if "errorPanel" in html or "cbo_nn_PanelErrorDiv" in html:
                raise CbordError(
                    f"nutrition label for detailOid={detail_oid} returned an error "
                    f"panel (is menuOid={menu_oid} correct / still current?)")
            return html

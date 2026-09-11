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
    pass


class CbordError(Exception):
    pass


class CbordClient:

    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout
        self._lock = threading.RLock()
        self._session: Optional[requests.Session] = None


    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(_HEADERS)
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
        with self._lock:
            self._session = None


    def _post(self, path: str, data: dict, *, expect_json: bool, _retry: bool = True):
        session = self._ensure_session()
        url = f"{BASE}/{path}"
        try:
            r = session.post(url, data=data, timeout=self._timeout)
            r.raise_for_status()
            if expect_json:
                payload = r.json()
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


    def get_landing_html(self) -> str:
        with self._lock:
            self._ensure_session()
            return self._landing_html

    def select_unit(self, unit_oid: str) -> dict:
        with self._lock:
            env = self._post("Unit/SelectUnitFromUnitsList",
                             {"unitOid": str(unit_oid)}, expect_json=True)
            return {
                "itemPanelHtml": self._panel(env, "itemPanel"),
                "menuPanelHtml": self._panel(env, "menuPanel"),
            }

    def select_menu(self, menu_oid: str, unit_oid: Optional[str] = None) -> str:
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

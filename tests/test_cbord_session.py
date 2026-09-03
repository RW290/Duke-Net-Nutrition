"""CBORD session-state tests (no network: a fake transport stands in)."""
from app.cbord import CbordClient, CbordError


class _FakeClient(CbordClient):
    """CbordClient with the HTTP layer replaced, mimicking CBORD's real rule:
    SelectMenu only succeeds when the owning unit is selected in the session."""

    OWNER = {"999": "3"}          # menuOid -> unitOid that must be selected

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []
        self._selected_unit = None
        self._session = object()   # bypass real session creation
        self._landing_html = "<html></html>"

    def _post(self, path, data, *, expect_json, _retry=True):
        self.calls.append((path, dict(data)))
        if path == "Unit/SelectUnitFromUnitsList":
            self._selected_unit = data["unitOid"]
            return {"success": True, "panels": [{"id": "itemPanel", "html": "<unit/>"},
                                                {"id": "menuPanel", "html": "<menus/>"}]}
        if path == "Menu/SelectMenu":
            if self._selected_unit != self.OWNER.get(data["menuOid"]):
                raise CbordError("non-success envelope from Menu/SelectMenu")
            return {"success": True, "panels": [{"id": "itemPanel", "html": "<items/>"}]}
        raise AssertionError(f"unexpected path {path}")


def test_select_menu_without_unit_fails_when_unit_unknown():
    c = _FakeClient()
    try:
        c.select_menu("999")
        assert False, "expected CbordError"
    except CbordError:
        pass


def test_select_menu_recovers_by_selecting_the_owning_unit():
    c = _FakeClient()
    html = c.select_menu("999", unit_oid="3")
    assert html == "<items/>"
    paths = [p for p, _ in c.calls]
    # Tries the menu, fails, selects the unit, retries the menu.
    assert paths == ["Menu/SelectMenu", "Unit/SelectUnitFromUnitsList", "Menu/SelectMenu"]


def test_select_menu_skips_unit_selection_when_already_correct():
    c = _FakeClient()
    c.select_unit("3")
    c.calls.clear()
    assert c.select_menu("999", unit_oid="3") == "<items/>"
    # Already on the right unit: one call, no redundant re-selection.
    assert [p for p, _ in c.calls] == ["Menu/SelectMenu"]

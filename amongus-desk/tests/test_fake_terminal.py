from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from PIL import Image

import amongus_desk.execution.unitrader.fake as fake_module
from amongus_desk.clock import ManualClock
from amongus_desk.execution.unitrader.backend import GuiBackend, OperatorAbortError
from amongus_desk.execution.unitrader.fake import (
    FAKE_LAYOUT_FILENAME,
    FAKE_TEMPLATE_DIR,
    HANDLE_OTHER,
    HANDLE_POPUP,
    HANDLE_TERMINAL,
    FakeGuiBackend,
    FakeTerminal,
    Rect,
    calibrate_fake,
)
from amongus_desk.execution.unitrader.layout import LayoutFile
from amongus_desk.execution.unitrader.vision import changed_fraction, is_blank, similarity
from factories import T0


def rig(*symbols: str, **kwargs: object) -> tuple[FakeTerminal, FakeGuiBackend]:
    term = FakeTerminal(symbols or ("EURUSD",), **kwargs)  # type: ignore[arg-type]
    return term, FakeGuiBackend(term)


def screen(term: FakeTerminal, x: int, y: int) -> tuple[int, int]:
    left, top = term.client_origin
    return left + x, top + y


def click_rect(be: FakeGuiBackend, term: FakeTerminal, r: Rect) -> None:
    be.click(*screen(term, *r.center))


def type_lot(be: FakeGuiBackend, term: FakeTerminal, symbol: str, text: str) -> None:
    click_rect(be, term, term.geometry().panels[symbol].lot_field)
    be.hotkey("ctrl", "a")
    be.type_text(text)


def capture_rect(be: FakeGuiBackend, term: FakeTerminal, r: Rect) -> Image.Image:
    x, y = screen(term, r.x, r.y)
    return be.capture(x, y, r.w, r.h)


# ------------------------------------------------------------------------------------------ identity
def test_module_says_it_is_a_simulation() -> None:
    doc = inspect.getdoc(fake_module) or ""
    assert "SIMULATION" in doc
    assert "says nothing about the real UniTrader terminal" in doc
    assert "UniTrader" not in fake_module.FAKE_TITLE


def test_backend_implements_the_gui_backend_protocol() -> None:
    _, be = rig()
    backend: GuiBackend = be  # static check via mypy
    for name in dir(GuiBackend):
        if not name.startswith("_"):
            assert callable(getattr(backend, name)), name


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        FakeTerminal(())
    with pytest.raises(ValueError):
        FakeTerminal(("EURUSD", "EURUSD"))
    with pytest.raises(ValueError, match="too small"):
        FakeTerminal(("A1", "B1", "C1", "D1"), client_size=(600, 400))


@pytest.mark.parametrize("size", [(500, 600), (700, 600)])
def test_narrow_client_area_is_rejected_instead_of_overlapping_widgets(size: tuple[int, int]) -> None:
    # 500 px: the positions indicator would stick out of the window; 700 px: the close-all button would sit
    # on top of the positions indicator.
    with pytest.raises(ValueError, match="too small"):
        FakeTerminal(client_size=size)


def test_rect_overlaps() -> None:
    a = Rect(0, 0, 10, 10)
    assert a.overlaps(Rect(9, 9, 5, 5)) and Rect(9, 9, 5, 5).overlaps(a)
    assert not a.overlaps(Rect(10, 0, 5, 5))  # touching edges share no pixel
    assert not a.overlaps(Rect(0, 10, 5, 5))


def test_badge_alias() -> None:
    term = FakeTerminal(badge="LIVE")
    assert term.badge == term.badge_text == "LIVE"
    term.badge = "DEMO"
    assert term.badge_text == "DEMO"


# ------------------------------------------------------------------------------------------ windows
def test_window_listing_and_knobs() -> None:
    term, be = rig()
    wins = be.list_windows()
    assert [w.handle for w in wins if w.title == term.title] == [HANDLE_TERMINAL]
    info = be.get_window(HANDLE_TERMINAL)
    assert info is not None
    assert (info.client_width, info.client_height, info.dpi) == (*term.client_size, term.dpi)
    term.duplicate_windows = 2
    assert len([w for w in be.list_windows() if w.title == term.title]) == 3
    term.window_present = False
    term.duplicate_windows = 0
    assert all(w.title != term.title for w in be.list_windows())
    assert be.get_window(HANDLE_TERMINAL) is None


def test_minimized_window_is_not_drawn_until_restored() -> None:
    term, be = rig()
    term.minimized = True
    info = be.get_window(HANDLE_TERMINAL)
    assert info is not None and info.minimized
    left, top = term.client_origin
    assert be.root_window_at(left + 5, top + 5) is None
    be.restore_window(HANDLE_TERMINAL)
    assert not term.minimized
    assert be.root_window_at(left + 5, top + 5) == HANDLE_TERMINAL


def test_activation_and_focus_knobs() -> None:
    term, be = rig()
    term.steal_focus()
    assert be.foreground_window() == HANDLE_OTHER
    assert be.activate_window(HANDLE_TERMINAL)
    assert be.foreground_window() == HANDLE_TERMINAL
    term.steal_focus()
    term.refuse_focus = True
    assert not be.activate_window(HANDLE_TERMINAL)
    assert be.foreground_window() == HANDLE_OTHER


def test_steal_focus_after_actions() -> None:
    term, be = rig()
    term.steal_focus_after_actions = 2
    be.move_mouse(1, 1)
    assert be.foreground_window() == HANDLE_TERMINAL
    be.move_mouse(2, 2)
    assert be.foreground_window() == HANDLE_OTHER


def test_popup_covers_part_of_the_client_area() -> None:
    term, be = rig()
    buy = term.geometry().panels["EURUSD"].buy_button
    term.popup = Rect(buy.x - 5, buy.y - 5, buy.w + 10, buy.h + 10)
    assert be.root_window_at(*screen(term, *buy.center)) == HANDLE_POPUP
    assert be.root_window_at(*screen(term, 2, 2)) == HANDLE_TERMINAL
    click_rect(be, term, buy)
    assert term.order_clicks == []  # the popup swallowed the click
    assert be.foreground_window() == HANDLE_POPUP


def test_root_window_outside_the_terminal_is_none() -> None:
    _, be = rig()
    assert be.root_window_at(0, 0) is None


# ------------------------------------------------------------------------------------------ screen
def test_capture_returns_exact_size_and_matches_rendering() -> None:
    term, be = rig()
    w, h = term.client_size
    shot = be.capture(*term.client_origin, w, h)
    assert shot.size == (w, h)
    assert similarity(shot, term.render_for_calibration()) == pytest.approx(1.0)


def test_capture_off_screen_is_padded() -> None:
    _, be = rig()
    shot = be.capture(-50, -50, 40, 40)
    assert shot.size == (40, 40)


def test_locked_desktop_is_black_and_drops_input() -> None:
    term, be = rig()
    term.locked = True
    assert not be.input_desktop_available()
    assert is_blank(be.capture(*term.client_origin, 300, 200))
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    assert term.order_clicks == []
    assert not be.activate_window(HANDLE_TERMINAL)


def test_capture_rejects_bad_sizes() -> None:
    _, be = rig()
    with pytest.raises(Exception, match="invalid capture size"):
        be.capture(0, 0, 0, 10)


# ------------------------------------------------------------------------------------------ fields + clipboard
def test_select_all_type_and_copy() -> None:
    term, be = rig()
    type_lot(be, term, "EURUSD", "0.25")
    assert term.field_text("EURUSD") == "0.25"
    assert term.focused_field == ("EURUSD", "lot")
    be.set_clipboard_text("sentinel")
    click_rect(be, term, term.geometry().panels["EURUSD"].lot_field)
    be.hotkey("ctrl", "c")  # nothing selected after a click
    assert be.get_clipboard_text() == "sentinel"
    be.hotkey("ctrl", "a")
    be.hotkey("ctrl", "c")
    assert be.get_clipboard_text() == "0.25"
    be.press("tab")
    assert term.focused_field is None


def test_typing_without_selection_appends() -> None:
    term, be = rig()
    click_rect(be, term, term.geometry().panels["EURUSD"].lot_field)
    be.type_text("5")
    assert term.field_text("EURUSD") == "0.015"


def test_backspace_and_delete() -> None:
    term, be = rig()
    click_rect(be, term, term.geometry().panels["EURUSD"].lot_field)
    be.press("backspace")
    assert term.field_text("EURUSD") == "0.0"
    be.hotkey("ctrl", "a")
    be.press("delete")
    assert term.field_text("EURUSD") == ""


def test_lot_field_that_ignores_select_all() -> None:
    term, be = rig()
    term.lot_field_ignores_select_all = True
    type_lot(be, term, "EURUSD", "0.10")
    assert term.field_text("EURUSD") == "0.010.10"
    be.set_clipboard_text("sentinel")
    be.hotkey("ctrl", "a")
    be.hotkey("ctrl", "c")
    assert be.get_clipboard_text() == "sentinel"


def test_broken_copy_leaves_the_clipboard_alone() -> None:
    term, be = rig()
    term.clipboard_copy_broken = True
    type_lot(be, term, "EURUSD", "0.10")
    be.set_clipboard_text("sentinel")
    be.hotkey("ctrl", "a")
    be.hotkey("ctrl", "c")
    assert be.get_clipboard_text() == "sentinel"


def test_lot_field_transform() -> None:
    term, be = rig()
    term.lot_field_transform = lambda text: text[:3]
    type_lot(be, term, "EURUSD", "0.25")
    assert term.field_text("EURUSD") == "0.2"


def test_commit_transform_runs_when_the_field_commits() -> None:
    term, be = rig()
    seen: list[tuple[str, str]] = []

    def clamp(kind: str, text: str) -> str:
        seen.append((kind, text))
        return "0.05" if kind == "lot" else text

    term.commit_transform = clamp
    type_lot(be, term, "EURUSD", "0.25")
    click_rect(be, term, term.geometry().panels["EURUSD"].lot_field)  # clicking the focused field again: no commit
    be.hotkey("ctrl", "a")
    be.hotkey("ctrl", "c")
    assert be.get_clipboard_text() == "0.25" and seen == []  # typed, not committed yet
    be.press("tab")
    assert term.field_text("EURUSD") == "0.05" and seen == [("lot", "0.25")]
    assert term.focused_field is None


def test_commit_transform_runs_when_the_focus_leaves_the_field() -> None:
    term, be = rig(protective_fields=True)
    term.commit_transform = lambda kind, text: f"{text}0" if kind == "lot" else text.strip()
    panel = term.geometry().panels["EURUSD"]
    type_lot(be, term, "EURUSD", "0.1")
    click_rect(be, term, panel.sl_field)  # the lot field loses the focus to another field
    assert term.field_text("EURUSD") == "0.10"
    be.type_text(" 1.09800 ")
    click_rect(be, term, panel.buy_button)  # ...and the SL field to the button, before the order reads it
    assert [(o.lot_text, o.sl_text) for o in term.orders] == [("0.10", "1.09800")]


def test_empty_field_copies_nothing_when_asked() -> None:
    term, be = rig()
    click_rect(be, term, term.geometry().panels["EURUSD"].lot_field)
    be.hotkey("ctrl", "a")
    be.press("backspace")
    assert term.field_text("EURUSD") == ""
    be.set_clipboard_text("sentinel")
    be.hotkey("ctrl", "a")
    be.hotkey("ctrl", "c")
    assert be.get_clipboard_text() == ""  # default: the empty text is copied
    term.empty_copy_keeps_clipboard = True
    be.set_clipboard_text("sentinel")
    be.hotkey("ctrl", "a")
    be.hotkey("ctrl", "c")
    assert be.get_clipboard_text() == "sentinel"


def test_keyboard_input_needs_the_foreground() -> None:
    term, be = rig()
    click_rect(be, term, term.geometry().panels["EURUSD"].lot_field)
    term.steal_focus()
    be.hotkey("ctrl", "a")
    be.type_text("9")
    assert term.field_text("EURUSD") == "0.01"


# ------------------------------------------------------------------------------------------ orders
def test_buy_click_fills_with_the_field_lots() -> None:
    term, be = rig()
    type_lot(be, term, "EURUSD", "0.10")
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    assert term.order_clicks == [("EURUSD", "BUY")]
    assert [(o.symbol, o.side, o.lot_text, o.lots) for o in term.orders] == [("EURUSD", "BUY", "0.10", Decimal("0.10"))]
    assert [(p.symbol, p.side, p.lots) for p in term.positions] == [("EURUSD", "BUY", Decimal("0.10"))]


def test_positions_indicator_changes_on_fill() -> None:
    term, be = rig()
    ind = term.geometry().indicator
    before = capture_rect(be, term, ind)
    click_rect(be, term, term.geometry().panels["EURUSD"].sell_button)
    after = capture_rect(be, term, ind)
    assert changed_fraction(before, after) > 0.05


def test_fill_delay_uses_virtual_time_and_advances_the_clock() -> None:
    term = FakeTerminal()
    clock = ManualClock(T0)
    be = FakeGuiBackend(term, clock=clock)
    term.fill_delay_seconds = 1.0
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    assert term.positions == []
    be.sleep(0.5)
    assert term.positions == []
    be.sleep(0.5)
    assert len(term.positions) == 1
    assert clock.monotonic() == pytest.approx(1.0)


def test_no_fill_knob() -> None:
    term, be = rig()
    term.fill_enabled = False
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    assert term.order_clicks == [("EURUSD", "BUY")]
    assert term.positions == [] and term.orders == []


def test_netting_and_hedging() -> None:
    term, be = rig()
    buy = term.geometry().panels["EURUSD"].buy_button
    sell = term.geometry().panels["EURUSD"].sell_button
    click_rect(be, term, buy)
    click_rect(be, term, sell)  # netting: same size opposite order closes
    assert term.positions == []
    term2, be2 = rig(netting=False)
    click_rect(be2, term2, buy)
    click_rect(be2, term2, sell)
    assert len(term2.positions) == 2


def current_dialog(term: FakeTerminal) -> str | None:
    return term.dialog


def test_show_dialog_opens_a_leftover_dialog() -> None:
    term, be = rig(order_confirm_dialog=True)
    with pytest.raises(ValueError, match="needs order"):
        term.show_dialog("order_confirm")
    term.show_dialog("order_confirm", ("EURUSD", "SELL"))
    assert current_dialog(term) == "order_confirm"
    click_rect(be, term, term.geometry().dialog_ok)  # confirming it executes THAT order
    assert [(p.side, p.lots) for p in term.positions] == [("SELL", Decimal("0.01"))]
    term.show_dialog("error")
    assert current_dialog(term) == "error"
    be.press("esc")
    assert current_dialog(term) is None


def test_confirmation_dialog_ok_cancel_and_esc() -> None:
    term, be = rig(order_confirm_dialog=True)
    geo = term.geometry()
    buy = geo.panels["EURUSD"].buy_button
    click_rect(be, term, buy)
    assert current_dialog(term) == "order_confirm" and term.positions == []
    click_rect(be, term, geo.panels["EURUSD"].sell_button)  # modal: ignored
    assert term.order_clicks == [("EURUSD", "BUY")]
    click_rect(be, term, geo.dialog_ok)
    assert current_dialog(term) is None and len(term.positions) == 1 and term.dialog_confirms == 1
    click_rect(be, term, buy)
    click_rect(be, term, geo.dialog_cancel)
    assert current_dialog(term) is None and term.positions[0].lots == Decimal("0.01")
    click_rect(be, term, buy)
    be.press("esc")
    assert current_dialog(term) is None and term.positions[0].lots == Decimal("0.01")


def test_error_dialog() -> None:
    term, be = rig()
    term.order_error = True
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    assert term.dialog == "error" and term.positions == []
    be.press("esc")
    assert term.dialog is None


def test_invalid_lot_text_gives_an_error_dialog() -> None:
    term, be = rig()
    type_lot(be, term, "EURUSD", "abc")
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    assert term.dialog == "error" and term.orders == []


def test_one_click_disabled_does_nothing() -> None:
    term, be = rig(one_click_enabled=False)
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    assert term.order_clicks == [("EURUSD", "BUY")] and term.positions == []


def test_close_all_with_and_without_dialog() -> None:
    term, be = rig()
    term.open_position("EURUSD", "BUY", Decimal("0.10"))
    click_rect(be, term, term.geometry().close_all)
    assert term.positions == [] and term.close_all_clicks == 1
    term2, be2 = rig(close_all_confirm_dialog=True)
    term2.open_position("EURUSD", "SELL", Decimal("0.10"))
    click_rect(be2, term2, term2.geometry().close_all)
    assert term2.dialog == "close_all_confirm" and len(term2.positions) == 1
    click_rect(be2, term2, term2.geometry().dialog_ok)
    assert term2.positions == []


def test_swapped_panels_move_the_labels() -> None:
    term, be = rig("EURUSD", "GBPUSD")
    label = term.geometry().panels["EURUSD"].label
    reference = term.render_for_calibration().crop(label.box)
    assert similarity(capture_rect(be, term, label), reference) == pytest.approx(1.0)
    term.swap_panels = True
    assert similarity(capture_rect(be, term, label), reference) < 0.9
    # the BUY at EURUSD's calibrated spot now belongs to GBPUSD
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    assert term.order_clicks == [("GBPUSD", "BUY")]


def test_indicator_flicker_changes_between_captures() -> None:
    term, be = rig()
    term.indicator_flicker = True
    ind = term.geometry().indicator
    a = capture_rect(be, term, ind)
    b = capture_rect(be, term, ind)
    assert changed_fraction(a, b) > 0


# ------------------------------------------------------------------------------------------ fail-safe + log
def test_failsafe_after_actions() -> None:
    term, be = rig()
    term.failsafe_after_actions = 1
    be.move_mouse(1, 1)
    with pytest.raises(OperatorAbortError):
        be.move_mouse(2, 2)
    with pytest.raises(OperatorAbortError):
        be.click(3, 3)


def test_failsafe_after_order_click() -> None:
    term, be = rig()
    term.failsafe_after_order_click = True
    click_rect(be, term, term.geometry().panels["EURUSD"].buy_button)
    with pytest.raises(OperatorAbortError):
        be.move_mouse(0, 0)


def test_on_action_hook_and_action_log() -> None:
    term, be = rig()
    seen: list[str] = []
    term.on_action = lambda t, name: seen.append(name)
    be.move_mouse(5, 5)
    be.press("tab")
    be.capture(0, 0, 10, 10)
    assert seen == ["move_mouse", "press"]
    assert [a.name for a in be.input_actions()] == ["move_mouse", "press"]
    assert be.cursor_position() == (5, 5)
    assert any(a.name == "capture" for a in be.actions)


def test_sleep_is_virtual() -> None:
    term = FakeTerminal()
    clock = ManualClock(T0)
    be = FakeGuiBackend(term, clock=clock)
    be.sleep(100.0)
    assert term.time == pytest.approx(100.0)
    assert clock.monotonic() == pytest.approx(100.0)


# ------------------------------------------------------------------------------------------ calibrate_fake
def test_calibrate_fake_writes_a_loadable_correct_layout(tmp_path: Path) -> None:
    term, be = rig("EURUSD", "GBPUSD", protective_fields=True, order_confirm_dialog=True, close_all_confirm_dialog=True)
    lf = calibrate_fake(term, tmp_path, calibrated_at=T0)
    lay = lf.layout
    assert (tmp_path / FAKE_LAYOUT_FILENAME).exists()
    assert (tmp_path / FAKE_TEMPLATE_DIR / "account_badge.png").exists()
    loaded = LayoutFile.load(tmp_path / FAKE_LAYOUT_FILENAME)
    assert loaded.layout == lay
    assert lay.account_kind == "demo"
    assert lay.calibrated_at == T0
    assert "SIMULATED" in (lay.notes or "")
    assert set(lay.panels) == {"EURUSD", "GBPUSD"}
    assert lay.panels["EURUSD"].sl_input is not None and lay.order_dialog and lay.close_all_dialog
    assert lay.close_all_button is not None and len(lay.error_templates) == 1
    raw = yaml.safe_load((tmp_path / FAKE_LAYOUT_FILENAME).read_text())
    assert raw["window_title_regex"]
    # every template matches what is on screen right now
    left, top = term.client_origin
    for name, ref in loaded.all_templates().items():
        r = ref.region
        shot = be.capture(left + r.x, top + r.y, r.w, r.h)
        assert similarity(shot, loaded.load_template(ref)) == pytest.approx(1.0), name


def test_calibrate_fake_points_hit_the_right_widgets(tmp_path: Path) -> None:
    term, be = rig("EURUSD", "GBPUSD")
    lay = calibrate_fake(term, tmp_path, calibrated_at=T0).layout
    left, top = term.client_origin
    be.click(left + lay.panels["GBPUSD"].sell_button.x, top + lay.panels["GBPUSD"].sell_button.y)
    assert term.order_clicks == [("GBPUSD", "SELL")]
    park = lay.park_point
    assert be.root_window_at(left + park.x, top + park.y) == HANDLE_TERMINAL
    be.click(left + park.x, top + park.y)  # the park point is not a widget
    assert term.order_clicks == [("GBPUSD", "SELL")] and term.close_all_clicks == 0


def test_calibrate_fake_optional_parts(tmp_path: Path) -> None:
    term, _ = rig(has_close_all=False)
    lay = calibrate_fake(term, tmp_path, calibrated_at=T0, one_click_indicator=False, error_template=False).layout
    assert lay.close_all_button is None
    assert lay.one_click_indicator is None
    assert lay.order_dialog is None and lay.close_all_dialog is None
    assert lay.error_templates == []


def test_calibrate_fake_live_badge(tmp_path: Path) -> None:
    term, _ = rig(badge="LIVE")
    assert calibrate_fake(term, tmp_path, calibrated_at=T0).layout.account_kind == "live"


def test_calibrate_fake_rejects_unknown_badges(tmp_path: Path) -> None:
    term, _ = rig(badge="???")
    with pytest.raises(ValueError, match="DEMO or LIVE"):
        calibrate_fake(term, tmp_path)


def test_demo_and_live_badges_are_told_apart(tmp_path: Path) -> None:
    term, be = rig()
    lf = calibrate_fake(term, tmp_path, calibrated_at=T0)
    ref = lf.layout.account_badge
    template = lf.load_template(ref)
    term.badge = "LIVE"
    left, top = term.client_origin
    shot = be.capture(left + ref.region.x, top + ref.region.y, ref.region.w, ref.region.h)
    assert similarity(shot, template) < 0.5

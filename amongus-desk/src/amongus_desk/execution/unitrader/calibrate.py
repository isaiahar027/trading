"""Interactive calibration wizard: records where things are in YOUR UniTrader window, from YOUR screen.

Nothing in this module describes the real UniTrader UI, and nothing is guessed. The operator logs in
to the terminal **themselves**, opens the one-click panels, picks the terminal window from a list and
then hovers the mouse over each element when asked. The wizard records mouse positions (converted to
client-area coordinates) and screenshots of the areas the operator marks, and asks the operator which
account (demo or live) is showing.

What the wizard does to the desktop: it reads the mouse position, takes screenshots, and moves the
mouse to the operator-chosen rest spot (``park_point``) before every screenshot, so hover highlights
and tooltips never end up in a reference image (at trading time the venue parks the mouse there too).
It never clicks, never types, never activates, moves or resizes the terminal window, and never asks
for, reads or stores credentials.

Output: the layout YAML at ``out_path`` and its reference images in ``templates/`` next to it. Images
get unique names and are written first; the layout file is replaced atomically last, so the layout on
disk always refers to a complete set of images. Images only the previous layout used are then removed.
Finally every template is re-captured and its similarity score reported.
"""

from __future__ import annotations

import logging
import math
import os
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from PIL import Image
from pydantic import ValidationError

from ...clock import Clock, SystemClock
from ...config import DeskConfig
from ...domain import Mode
from .backend import BackendError, GuiBackend, OperatorAbortError, WindowInfo
from .layout import DialogLayout, Layout, LayoutFile, PanelLayout, Point, Region, TemplateRef
from .vision import changed_fraction, is_blank, similarity

log = logging.getLogger(__name__)

_T = TypeVar("_T")

Prompt = Callable[[str], str]
Say = Callable[[str], None]

TEMPLATES_DIR = "templates"
MIN_TEMPLATE_SIDE = 4  # pixels; a smaller reference image can not identify anything reliably
_MIN_POINT_DISTANCE = 3  # pixels; two targets closer than this are treated as the same spot
_PARK_MARGIN = 5  # pixels; the mouse rest spot must stay this far away from every reference area
# Same probe grid as the venue's ``_region_unobstructed``: both ends plus points at most this far apart.
_GRID_STEP_PX = 20
_GRID_MAX_POINTS = 32
_PARK_LABEL = "the mouse rest spot"
_MAX_INVALID_ANSWERS = 5
_MAX_ERROR_TEMPLATES = 10
_QUIT_WORDS = ("q", "quit")

_NO_WINDOW = (
    "The UniTrader window was not found. Open UniTrader and log in to your account yourself "
    "(this tool never handles your password), open the one-click panels, then run calibrate again."
)


class CalibrationError(Exception):
    """Calibration could not be completed. The message tells the operator what to do next."""


class CalibrationAborted(CalibrationError):
    """The operator quit, declined, or triggered the fail-safe."""


class _Retry(Exception):
    """The operator's input for one element was unusable; explain why and let them try again."""


@dataclass(frozen=True, slots=True)
class TemplateCheck:
    """Similarity between a saved reference image and a fresh capture of its region."""

    name: str
    score: float
    threshold: float
    # True for always-visible areas (badge, labels, one-click indicator), which must match now. False for
    # dialogs / error messages: the operator closed them, so they must NOT match now. A dialog template
    # that matches the normal screen would make the venue click its confirm point after every order, and
    # an error template that does would turn orders into false REJECTED reports.
    required: bool

    @property
    def matched(self) -> bool:
        """The fresh capture matched the saved image at or above the threshold."""
        return self.score >= self.threshold

    @property
    def passed(self) -> bool:
        """Always-visible templates must match the fresh capture; dialog/error templates must not."""
        return self.matched if self.required else not self.matched


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """What :func:`run_calibration` saved and how the re-verification went."""

    layout_file: LayoutFile
    path: Path
    checks: tuple[TemplateCheck, ...]
    warnings: tuple[str, ...] = ()

    @property
    def failed(self) -> list[TemplateCheck]:
        """Checks that did not pass (see :attr:`TemplateCheck.passed`)."""
        return [c for c in self.checks if not c.passed]

    @property
    def ok(self) -> bool:
        """True when every always-visible template matched and no dialog/error template matched."""
        return not self.failed

    def __bool__(self) -> bool:
        """Same as :attr:`ok`, so ``if run_calibration(...):`` means "saved and verified"."""
        return self.ok


def run_calibration(  # noqa: PLR0917 - positional signature fixed by docs/ARCHITECTURE.md
    cfg: DeskConfig,
    backend: GuiBackend,
    out_path: Path,
    symbols: Sequence[str] | None,
    prompt: Prompt = input,
    say: Say = print,
    countdown: int = 3,
    *,
    clock: Clock | None = None,
    settle_seconds: float = 0.5,
    max_attempts: int = 3,
) -> CalibrationResult:
    """Run the interactive calibration wizard and write the layout + templates to ``out_path``.

    ``symbols`` selects the panels to calibrate (``None`` or empty: every enabled instrument). The
    new layout replaces the previous one completely. ``prompt``/``say`` default to ``input``/``print``
    and are injectable for tests; ``countdown`` is the number of seconds (slept through ``backend``)
    before each mouse position is recorded.

    The returned :class:`CalibrationResult` is falsy when the layout was saved but re-verification
    failed: an always-visible template did not match its fresh capture, or a dialog/error template
    still matched the screen after the operator closed it.

    Raises :class:`CalibrationAborted` when the operator quits or declines and
    :class:`CalibrationError` when calibration can not be completed. In both cases nothing is
    written unless the operator already confirmed saving.
    """
    wizard = _Wizard(
        cfg,
        backend,
        Path(out_path),
        _resolve_symbols(cfg, symbols),
        prompt=prompt,
        say=say,
        countdown=countdown,
        clock=clock or SystemClock(),
        settle_seconds=settle_seconds,
        max_attempts=max_attempts,
    )
    return wizard.run()


def _resolve_symbols(cfg: DeskConfig, symbols: Sequence[str] | None) -> list[str]:
    if not symbols:
        return [inst.symbol for inst in cfg.enabled_instruments]
    known = {inst.symbol.upper(): inst.symbol for inst in cfg.instruments}
    chosen: list[str] = []
    unknown: list[str] = []
    for raw in symbols:
        symbol = known.get(raw.strip().upper())
        if symbol is None:
            unknown.append(raw)
        elif symbol not in chosen:
            chosen.append(symbol)
    if unknown:
        raise CalibrationError(
            f"unknown symbol(s): {', '.join(unknown)}; configured instruments: {', '.join(sorted(known.values()))}"
        )
    return chosen


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._") or "template"


def _title_matches(pattern: str, title: str) -> bool:
    # Same rule as the venue: the pattern must match the WHOLE title (re.fullmatch), and only visible
    # windows count.
    return re.fullmatch(pattern, title) is not None


def _process_matches(process: str | None, window: WindowInfo) -> bool:
    if process is None:
        return True
    return window.process_name is not None and window.process_name.lower() == process.lower()


def template_refs(layout: Layout) -> dict[str, tuple[TemplateRef, bool]]:
    """Every template in a layout, keyed by name, with whether it must be visible at all times."""
    refs: dict[str, tuple[TemplateRef, bool]] = {"account_badge": (layout.account_badge, True)}
    if layout.one_click_indicator is not None:
        refs["one_click_indicator"] = (layout.one_click_indicator, True)
    for symbol, panel in layout.panels.items():
        refs[f"panel:{symbol}"] = (panel.label, True)
    if layout.close_all_dialog is not None:
        refs["close_all_dialog"] = (layout.close_all_dialog.template, False)
    if layout.order_dialog is not None:
        refs["order_dialog"] = (layout.order_dialog.template, False)
    for i, ref in enumerate(layout.error_templates, start=1):
        refs[f"error_{i}"] = (ref, False)
    return refs


def _referenced_template_files(layout_path: Path) -> set[Path]:
    """Template files in ``templates/`` that the existing layout at ``layout_path`` refers to."""
    if not layout_path.exists():
        return set()
    try:
        previous = LayoutFile.load(layout_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log.warning("previous layout %s could not be read (%s); leaving its template files alone", layout_path, exc)
        return set()
    templates_dir = (previous.base_dir / TEMPLATES_DIR).resolve()
    files: set[Path] = set()
    for ref, _required in template_refs(previous.layout).values():
        try:
            path = previous.template_path(ref)
        except ValueError:
            continue
        if path.parent == templates_dir and path.suffix.lower() == ".png":
            files.add(path)
    return files


def _write_png_atomically(path: Path, image: Image.Image) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("wb") as fh:
            image.save(fh, format="PNG")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)


def _near(a: Point, b: Point) -> bool:
    return max(abs(a.x - b.x), abs(a.y - b.y)) < _MIN_POINT_DISTANCE


def _overlaps(a: Region, b: Region) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


def _grid(start: int, length: int) -> list[int]:
    """Probe coordinates along one axis: both ends plus points at most ``_GRID_STEP_PX`` apart."""
    if length <= 1:
        return [start]
    count = min(_GRID_MAX_POINTS, max(2, math.ceil((length - 1) / _GRID_STEP_PX) + 1))
    return sorted({start + round(i * (length - 1) / (count - 1)) for i in range(count)})


class _Wizard:
    """State of one calibration run. Single-threaded; one instance per run."""

    def __init__(
        self,
        cfg: DeskConfig,
        backend: GuiBackend,
        out_path: Path,
        symbols: list[str],
        *,
        prompt: Prompt,
        say: Say,
        countdown: int,
        clock: Clock,
        settle_seconds: float,
        max_attempts: int,
    ) -> None:
        if countdown < 0:
            raise ValueError("countdown must not be negative")
        if settle_seconds < 0:
            raise ValueError("settle_seconds must not be negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._cfg = cfg
        self._ut = cfg.execution.unitrader
        self._threshold = self._ut.template_threshold
        self._backend = backend
        self._out = out_path
        self._symbols = symbols
        self._prompt = prompt
        self._say = say
        self._countdown_from = countdown
        self._clock = clock
        self._settle = settle_seconds
        self._max_attempts = max_attempts

        self._stamp = ""
        self._handle = 0
        self._geometry: tuple[int, int, int] = (0, 0, 0)  # client width, client height, dpi
        self._park: Point | None = None
        self._positions: Region | None = None
        self._targets: list[tuple[str, Point]] = []  # recorded click targets (+ park point)
        self._areas: list[tuple[str, Region, Image.Image]] = []  # recorded always-visible templates
        self._images: dict[str, Image.Image] = {}  # layout-relative image path -> image
        self._warnings: list[str] = []
        self._saved = False

    # --- entry point ------------------------------------------------------------------------------

    def run(self) -> CalibrationResult:
        try:
            return self._run()
        except OperatorAbortError as exc:
            raise CalibrationAborted(self._failure_text(f"fail-safe triggered ({exc})")) from exc
        except BackendError as exc:
            raise CalibrationError(self._failure_text(f"desktop automation failed: {exc}")) from exc

    def _failure_text(self, reason: str) -> str:
        if self._saved:
            return f"{reason}. The layout was already saved to {self._out} but not fully re-verified."
        return f"{reason}. Calibration aborted; nothing was saved."

    def _run(self) -> CalibrationResult:
        calibrated_at = self._clock.now()
        self._stamp = f"{calibrated_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
        self._intro()
        if not self._backend.input_desktop_available():
            raise CalibrationError(
                "The Windows session is locked or disconnected, so the screen can't be read. Unlock it, "
                "log in to UniTrader yourself, and run calibrate again."
            )
        window, pattern, process = self._choose_window()
        self._handle = window.handle
        self._geometry = (window.client_width, window.client_height, window.dpi)
        self._say(
            f"Recorded the client area: {window.client_width}x{window.client_height} pixels at {window.dpi} DPI "
            f"({round(window.dpi * 100 / 96)}% scaling). From now on do not resize the terminal or change "
            "display scaling: the desk refuses to act if either differs from this calibration."
        )

        self._say("")
        self._say("Step 1: a rest spot for the mouse.")
        park = self._point(
            _PARK_LABEL,
            "a neutral spot inside the terminal where the mouse can rest: not a button, field, menu or "
            "anything else that reacts when the mouse is over it",
        )
        self._park = park

        self._say("")
        self._say("Step 2: the account badge.")
        badge = self._template(
            "account badge",
            "account_badge",
            "the account badge: the text or symbol in the terminal that shows which account is logged in",
        )
        account_kind = self._ask_account_kind()

        one_click: TemplateRef | None = None
        if self._yes_no(
            "Does your terminal show an indicator that one-click trading is switched on, which the desk "
            "should check before every order?"
        ):
            one_click = self._template(
                "one-click indicator",
                "one_click_indicator",
                "the one-click trading indicator, in the state it has while one-click trading is ON",
            )

        self._say("")
        self._say("Step 3: the positions indicator.")
        positions = self._positions_indicator()
        self._positions = positions

        panels: dict[str, PanelLayout] = {}
        for number, symbol in enumerate(self._symbols, start=1):
            self._say("")
            self._say(f"Step 4.{number}: the {symbol} one-click panel.")
            panels[symbol] = self._panel(symbol)

        self._say("")
        self._say("Step 5: optional elements.")
        close_all_button, close_all_dialog = self._close_all()
        order_dialog = self._order_dialog()
        errors = self._error_templates()

        notes = (
            f"Recorded by `desk calibrate` for {', '.join(self._symbols)}. Window title at calibration: "
            f"{window.title!r}; program: {window.process_name or 'unknown'}; client area origin on screen: "
            f"({window.client_left}, {window.client_top})."
        )
        layout = self._build_layout(
            window_title_regex=pattern,
            process_name=process,
            account_kind=account_kind,
            account_badge=badge,
            one_click_indicator=one_click,
            positions_indicator=positions,
            panels=panels,
            close_all_button=close_all_button,
            close_all_dialog=close_all_dialog,
            order_dialog=order_dialog,
            error_templates=errors,
            park_point=park,
            calibrated_at=calibrated_at,
            notes=notes,
        )
        self._summary(layout)
        if not self._yes_no(f"Save this layout to {self._out}?"):
            raise CalibrationAborted("Not saved (you declined). The previous layout, if any, is unchanged.")
        layout_file = self._write(layout)
        self._saved = True
        checks = self._verify(layout_file)
        result = CalibrationResult(
            layout_file=layout_file, path=self._out, checks=tuple(checks), warnings=tuple(self._warnings)
        )
        self._conclude(result)
        return result

    # --- operator dialogue --------------------------------------------------------------------------

    def _ask(self, question: str) -> str:
        try:
            answer = self._prompt(question + " ")
        except EOFError as exc:
            raise CalibrationAborted("Input ended. Calibration aborted; nothing was saved.") from exc
        answer = answer.strip()
        if answer.lower() in _QUIT_WORDS:
            raise CalibrationAborted("Quit by the operator. Calibration aborted; nothing was saved.")
        return answer

    def _yes_no(self, question: str) -> bool:
        for _ in range(_MAX_INVALID_ANSWERS):
            answer = self._ask(f"{question} [y/n]").lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self._say("  Please answer y or n (or q to quit).")
        raise CalibrationAborted("No valid answer. Calibration aborted; nothing was saved.")

    def _choice(self, question: str, options: Sequence[str]) -> str:
        for _ in range(_MAX_INVALID_ANSWERS):
            answer = self._ask(question).lower()
            if answer in options:
                return answer
            self._say(f"  Please type one of: {', '.join(options)} (or q to quit).")
        raise CalibrationAborted("No valid answer. Calibration aborted; nothing was saved.")

    def _number(self, question: str, low: int, high: int) -> int:
        for _ in range(_MAX_INVALID_ANSWERS):
            answer = self._ask(question)
            # ASCII digits only: str.isdigit() also accepts e.g. "²", which int() rejects.
            if re.fullmatch(r"[0-9]+", answer) and low <= int(answer) <= high:
                return int(answer)
            self._say(f"  Please type a number from {low} to {high} (or q to quit).")
        raise CalibrationAborted("No valid answer. Calibration aborted; nothing was saved.")

    def _warn(self, text: str) -> None:
        self._warnings.append(text)
        self._say(f"  WARNING: {text}")
        log.warning("calibration: %s", text)

    def _intro(self) -> None:
        self._say("=== amongus-desk calibration ===")
        self._say("This wizard records where the controls are in YOUR UniTrader window, from your own screen.")
        self._say("It never clicks or types into the terminal, and it never asks for, reads or stores your password.")
        self._say("")
        self._say("Before you continue:")
        self._say("  1. Open UniTrader and log in to your account YOURSELF (use your DEMO account for testing).")
        self._say(f"  2. Open the one-click trading panels for: {', '.join(self._symbols)}.")
        self._say("  3. Arrange the terminal the way it will stay. Do not resize it or change display scaling later.")
        self._say("  4. Move this console window so that it does not cover the terminal.")
        self._say("")
        self._say("You only hover the mouse. Each time, the wizard counts down and then records the mouse position.")
        self._say("Before each screenshot it moves the mouse to the rest spot you pick first, so hover effects")
        self._say("are not captured. Type q at any question to quit without saving; moving the mouse into a")
        self._say("screen corner also aborts.")
        if not self._yes_no("Have you logged in to UniTrader yourself and opened those panels?"):
            raise CalibrationAborted(
                "Log in to UniTrader yourself and open the one-click panels, then run calibrate again. "
                "Nothing was saved."
            )

    # --- window selection ---------------------------------------------------------------------------

    def _choose_window(self) -> tuple[WindowInfo, str, str | None]:
        windows = self._backend.list_windows()
        if not windows:
            raise CalibrationError(_NO_WINDOW)
        self._say("")
        self._say("Visible windows:")
        for i, win in enumerate(windows, start=1):
            self._say(f"  {i:>2}. {win.title!r}  [{self._describe(win)}]")
        index = self._number("Type the number of the UniTrader window (0 if it is not listed):", 0, len(windows))
        if index == 0:
            raise CalibrationError(_NO_WINDOW)
        chosen = windows[index - 1]
        if chosen.minimized:
            raise CalibrationError(
                "That window is minimized. Restore it (without resizing it) and run calibrate again."
            )
        if chosen.client_width <= 0 or chosen.client_height <= 0:
            raise CalibrationError("That window has no visible client area; pick the terminal's main window.")

        process: str | None = None
        if chosen.process_name:
            if self._yes_no(f"Also require the window to belong to the program {chosen.process_name!r}? (recommended)"):
                process = chosen.process_name
        else:
            self._say("  The program name of this window can't be read, so it will be matched by its title only.")

        for attempt in range(1, self._max_attempts + 1):
            pattern = self._title_pattern(chosen)
            matches = [
                w
                for w in self._backend.list_windows()
                if w.visible and _title_matches(pattern, w.title) and _process_matches(process, w)
            ]
            where = f" in program {process!r}" if process else ""
            self._say(f"Windows matching {pattern!r}{where}:")
            for win in matches:
                self._say(f"  - {win.title!r}  [{self._describe(win)}]")
            if not matches:
                self._say("  (none)")
            if len(matches) == 1 and matches[0].handle == chosen.handle:
                return matches[0], pattern, process
            if not any(w.handle == chosen.handle for w in matches):
                problem = "the chosen window is not among the matches (did it close?)"
            else:
                problem = (
                    "the pattern matches more than one window; the desk refuses to trade when it can't tell "
                    "which one is the terminal. Close duplicate terminal windows or type a more specific pattern"
                )
            self._say(f"  {problem}.")
            if attempt == self._max_attempts:
                raise CalibrationError(f"No title pattern matched exactly the terminal window: {problem}.")
        raise AssertionError("unreachable")

    def _title_pattern(self, chosen: WindowInfo) -> str:
        default = re.escape(chosen.title)
        for _ in range(_MAX_INVALID_ANSWERS):
            answer = self._ask(
                "Title pattern (a regular expression that must match the WHOLE title) the desk will use to find "
                f"this window. Press Enter to use the exact title {default!r}, or type a pattern (useful if the "
                "title changes, e.g. 'Name - .*'):"
            )
            pattern = answer or default
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                self._say(f"  That is not a valid regular expression: {exc}.")
                continue
            if compiled.fullmatch(chosen.title) is None:
                self._say(f"  That pattern does not match the whole title of the chosen window, {chosen.title!r}.")
                continue
            return pattern
        raise CalibrationAborted("No usable title pattern. Calibration aborted; nothing was saved.")

    @staticmethod
    def _describe(win: WindowInfo) -> str:
        parts = [
            win.process_name or "program unknown",
            f"pid {win.pid}",
            f"client {win.client_width}x{win.client_height}",
        ]
        if win.minimized:
            parts.append("minimized")
        return ", ".join(parts)

    # --- geometry helpers ---------------------------------------------------------------------------

    def _window(self) -> WindowInfo:
        """Fresh info about the terminal window; fails if it vanished, minimized or changed size/DPI."""
        info = self._backend.get_window(self._handle)
        if info is None:
            raise CalibrationError(
                self._failure_text(
                    "The terminal window disappeared. Open UniTrader, log in yourself, and run calibrate again"
                )
            )
        if info.minimized:
            raise CalibrationError(
                self._failure_text(
                    "The terminal window was minimized during calibration. Restore it and run calibrate again"
                )
            )
        width, height, dpi = self._geometry
        if (info.client_width, info.client_height, info.dpi) != self._geometry:
            raise CalibrationError(
                self._failure_text(
                    "The terminal window was resized or its display scaling changed during calibration "
                    f"({width}x{height} at {dpi} DPI -> {info.client_width}x{info.client_height} at {info.dpi} "
                    "DPI). Keep it fixed and run calibrate again"
                )
            )
        return info

    def _countdown(self, what: str) -> None:
        seconds = self._countdown_from
        if seconds <= 0:
            self._say(f"Hover the mouse over {what}. Do not click. Capturing now.")
            return
        self._say(f"Hover the mouse over {what}. Do not click. Capturing in {seconds}...")
        for remaining in range(seconds - 1, 0, -1):
            self._backend.sleep(1.0)
            self._say(f"  {remaining}...")
        self._backend.sleep(1.0)

    def _hover(self, what: str, *, own_window: bool) -> Point:
        """Count down, then return the mouse position in client coordinates (raises :class:`_Retry`)."""
        self._countdown(what)
        info = self._window()
        sx, sy = self._backend.cursor_position()
        cx, cy = sx - info.client_left, sy - info.client_top
        if not (0 <= cx < info.client_width and 0 <= cy < info.client_height):
            raise _Retry(
                f"the mouse was at screen ({sx}, {sy}), outside the terminal's client area (screen "
                f"{info.client_left},{info.client_top}, size {info.client_width}x{info.client_height})"
            )
        if own_window and self._backend.root_window_at(sx, sy) != self._handle:
            raise _Retry(
                f"the spot at screen ({sx}, {sy}) does not belong to the terminal's main window: another window "
                "covers it (move this console away) or it is a separate floating window, which the desk can't use"
            )
        self._say(f"  recorded ({cx}, {cy})")
        return Point(x=cx, y=cy)

    def _with_retries(self, label: str, step: Callable[[], _T]) -> _T:
        for attempt in range(1, self._max_attempts + 1):
            try:
                return step()
            except _Retry as exc:
                self._say(f"  Problem: {exc}.")
                if attempt == self._max_attempts:
                    raise CalibrationError(
                        f"Could not record {label} after {self._max_attempts} attempts ({exc}). Nothing was saved."
                    ) from exc
                self._ask("  Press Enter to try again (or q to quit):")
        raise AssertionError("unreachable")

    def _point(self, label: str, what: str, *, own_window: bool = True, distinct: bool = True) -> Point:
        """Record a click target / spot. ``distinct=False`` is for dialog buttons, which sit over the terminal."""

        def step() -> Point:
            point = self._hover(what, own_window=own_window)
            if distinct:
                for other_label, other in self._targets:
                    if _near(point, other):
                        raise _Retry(f"that is the same spot as {other_label} ({other.x}, {other.y})")
            self._check_target(point, reference_areas=distinct)
            return point

        point = self._with_retries(label, step)
        if distinct:
            self._targets.append((label, point))
        return point

    @staticmethod
    def _inside(point: Point, region: Region, margin: int = 0) -> bool:
        return (
            region.x - margin <= point.x < region.x + region.w + margin
            and region.y - margin <= point.y < region.y + region.h + margin
        )

    def _check_target(self, point: Point, *, reference_areas: bool) -> None:
        """A spot the desk clicks, types into or rests the mouse on must not change a watched area.

        The positions indicator is compared against a baseline taken *before* the lot is typed and the
        button clicked, so a field, button or hover spot inside it would make the venue count its own
        typing/click as a fill. A target inside an always-visible reference area (badge, label, ...) would
        change that area and make every later check fail.
        """
        positions = self._positions
        if positions is not None and self._inside(point, positions, _PARK_MARGIN):
            raise _Retry(
                "that spot is inside (or right next to) the positions indicator area; clicking, typing or "
                "hovering there would change it and the desk would mistake that for a fill"
            )
        if reference_areas:
            for area_label, region, _image in self._areas:
                if self._inside(point, region):
                    raise _Retry(
                        f"that spot is inside the {area_label} area; clicking or typing there would change it and "
                        "the desk would then refuse to trade"
                    )

    def _region(self, what: str, *, own_window: bool, target_margin: int = 0) -> Region:
        top_left = self._hover(f"the TOP-LEFT corner of {what}", own_window=own_window)
        bottom_right = self._hover(f"the BOTTOM-RIGHT corner of {what}", own_window=own_window)
        width = bottom_right.x - top_left.x + 1
        height = bottom_right.y - top_left.y + 1
        if width < MIN_TEMPLATE_SIDE or height < MIN_TEMPLATE_SIDE:
            raise _Retry(
                f"the marked area is {max(width, 0)}x{max(height, 0)} pixels; the bottom-right corner must be at "
                f"least {MIN_TEMPLATE_SIDE} pixels to the right of and below the top-left corner"
            )
        region = Region(x=top_left.x, y=top_left.y, w=width, h=height)
        if not own_window:
            return region
        # Same probe as the venue: a grid over the area (corners included, points <= 20 px apart) must
        # belong to the terminal's own top-level window.
        info = self._window()
        xs = _grid(info.client_left + region.x, region.w)
        ys = _grid(info.client_top + region.y, region.h)
        if any(self._backend.root_window_at(x, y) != self._handle for y in ys for x in xs):
            raise _Retry("part of the marked area is covered by another window (move this console away)")
        if self._park is not None and self._inside(self._park, region, _PARK_MARGIN):
            raise _Retry(
                "the mouse rest spot is inside (or right next to) this area, so the mouse would sit on it; mark "
                "the area again, or quit and pick another rest spot"
            )
        for target_label, target in self._targets:
            if target_label != _PARK_LABEL and self._inside(target, region, target_margin):
                raise _Retry(
                    f"{target_label} ({target.x}, {target.y}) is inside this area, so clicking or typing there would "
                    "change it; mark an area that leaves it out"
                )
        return region

    def _park_mouse(self) -> None:
        if self._park is None:
            return
        info = self._window()
        self._backend.move_mouse(info.client_left + self._park.x, info.client_top + self._park.y)

    def _grab(self, region: Region) -> Image.Image:
        info = self._window()
        image = self._backend.capture(info.client_left + region.x, info.client_top + region.y, region.w, region.h)
        if image.size != (region.w, region.h):
            raise BackendError(
                f"the GUI backend returned a {image.size[0]}x{image.size[1]} screenshot "
                f"for a {region.w}x{region.h} area"
            )
        return image if image.mode == "RGB" else image.convert("RGB")

    def _two_captures(self, region: Region) -> tuple[Image.Image, Image.Image]:
        self._park_mouse()
        self._backend.sleep(self._settle)
        first = self._grab(region)
        self._backend.sleep(self._settle)
        return first, self._grab(region)

    # --- elements -----------------------------------------------------------------------------------

    def _template(
        self,
        label: str,
        slug: str,
        what: str,
        *,
        transient: bool = False,
        validate: Callable[[Region, Image.Image], None] | None = None,
    ) -> TemplateRef:
        def step() -> tuple[Region, Image.Image]:
            region = self._region(what, own_window=not transient)
            first, second = self._two_captures(region)
            if is_blank(first):
                raise _Retry(
                    "the marked area is (nearly) one flat colour, so it would match any empty area; include "
                    "the text or symbol itself"
                )
            score = similarity(first, second)
            if score < self._threshold:
                raise _Retry(
                    f"the marked area changed between two screenshots {self._settle:.1f} s apart (similarity "
                    f"{score:.3f} < {self._threshold:.2f}); mark an area without live prices, clocks or animations"
                )
            if validate is not None:
                validate(region, first)
            return region, first

        region, image = self._with_retries(label, step)
        # The sequence number keeps names unique even when two slugs collapse to the same text (e.g. the
        # symbols "XX" and "XX_"), so one template can never overwrite another.
        rel = f"{TEMPLATES_DIR}/{self._stamp}-{len(self._images) + 1:02d}-{_slug(slug)}.png"
        self._images[rel] = image
        if not transient:
            self._areas.append((label, region, image))
        self._say(f"  captured {label}: {region.w}x{region.h} pixels at ({region.x}, {region.y})")
        return TemplateRef(region=region, image=rel)

    def _ask_account_kind(self) -> Literal["demo", "live"]:
        for _ in range(_MAX_INVALID_ANSWERS):
            answer = self._choice(
                "Which account is the terminal logged in to right now? Look at the badge you just marked and "
                "type demo or live (the wizard does not guess):",
                ("demo", "live"),
            )
            if answer == "demo":
                kind: Literal["demo", "live"] = "demo"
                break
            if self._yes_no("You declared a LIVE (real-money) account. Is that right?"):
                kind = "live"
                break
        else:
            raise CalibrationAborted("Account kind not confirmed. Calibration aborted; nothing was saved.")
        self._say(f"  account kind: {kind.upper()} (declared by you)")
        mode = self._cfg.mode
        if mode is Mode.DEMO and kind == "live":
            self._warn("this layout is for a LIVE account; mode 'demo' will refuse to trade with it")
        elif mode is Mode.LIVE and kind == "demo":
            self._warn("this layout is for a DEMO account; mode 'live' will refuse to trade with it")
        return kind

    def _positions_indicator(self) -> Region:
        what = (
            "an area that changes when a position opens or closes and otherwise stays still (the desk watches "
            "it to confirm fills)"
        )

        def step() -> Region:
            region = self._region(what, own_window=True, target_margin=_PARK_MARGIN)
            first, second = self._two_captures(region)
            fraction = changed_fraction(first, second)
            # The venue refuses to trade when ANY pixel of this area changes between its two baseline
            # captures, and counts a change as a fill afterwards, so it has to be perfectly still now.
            if fraction > 0.0:
                raise _Retry(
                    f"{fraction:.1%} of that area changed by itself within {self._settle:.1f} s; the desk would "
                    "refuse every order (or mistake the change for a fill). Mark an area that only changes when "
                    "positions open or close"
                )
            return region

        region = self._with_retries("the positions indicator", step)
        self._say(f"  recorded the positions indicator: {region.w}x{region.h} pixels at ({region.x}, {region.y})")
        return region

    def _panel(self, symbol: str) -> PanelLayout:
        def distinct_label(region: Region, image: Image.Image) -> None:
            for other_label, other_region, other_image in self._areas:
                if not other_label.startswith("panel "):
                    continue
                if _overlaps(region, other_region):
                    raise _Retry(f"this area overlaps the {other_label} area")
                if other_image.size == image.size and similarity(other_image, image) >= self._threshold:
                    raise _Retry(
                        f"this label looks the same as the {other_label}, so the desk could not tell them apart"
                    )

        label = self._template(
            f"panel {symbol} label",
            f"panel-{symbol}",
            f"the {symbol} panel's symbol label (only its static text: leave out prices and anything that changes)",
            validate=distinct_label,
        )
        lot = self._point(f"the {symbol} lot field", f"the lot (volume) input field of the {symbol} panel")
        buy = self._point(f"the {symbol} BUY button", f"the BUY button of the {symbol} panel")
        sell = self._point(f"the {symbol} SELL button", f"the SELL button of the {symbol} panel")
        sl: Point | None = None
        tp: Point | None = None
        if self._ut.protective_orders == "panel_fields":
            self._say(
                "  Your config uses protective_orders: panel_fields, so the stop-loss and take-profit fields are "
                "required. If this panel has none, quit (q) and change the config."
            )
            wants_fields = True
        else:
            wants_fields = self._yes_no(
                f"Does the {symbol} panel have stop-loss and take-profit input fields you want to record "
                "(only used with protective_orders: panel_fields)?"
            )
        if wants_fields:
            sl = self._point(f"the {symbol} stop-loss field", f"the stop-loss (SL) input field of the {symbol} panel")
            tp = self._point(
                f"the {symbol} take-profit field", f"the take-profit (TP) input field of the {symbol} panel"
            )
        return PanelLayout(lot_input=lot, buy_button=buy, sell_button=sell, label=label, sl_input=sl, tp_input=tp)

    def _dialog(self, slug: str, what: str) -> DialogLayout:
        self._ask(
            f"Open {what} yourself now and leave it open (do NOT confirm it). Press Enter when it shows (q to quit):"
        )
        template = self._template(
            what, slug, f"{what}: a part of it that always looks the same, e.g. its title or message", transient=True
        )
        confirm = self._point(
            f"the button that confirms {what}",
            f"the button that CONFIRMS {what} (do NOT click it)",
            own_window=False,
            distinct=False,
        )
        self._ask(f"Now close {what} yourself WITHOUT confirming it. Press Enter when it is gone (q to quit):")
        self._confirm_closed(what, template)
        return DialogLayout(template=template, confirm_button=confirm)

    def _confirm_closed(self, what: str, ref: TemplateRef) -> None:
        """The area of a dialog/message the operator just closed must no longer match its reference image.

        Otherwise the venue would "see" the dialog on the normal screen: it would click the recorded
        confirm point after every order, or take every order for rejected by an error message.
        """
        image = self._images[ref.image]
        for attempt in range(1, self._max_attempts + 1):
            self._park_mouse()
            self._backend.sleep(self._settle)
            score = similarity(image, self._grab(ref.region))
            if score < self._threshold:
                self._say(f"  {what} is gone (similarity {score:.3f}): the desk can tell it from the normal screen")
                return
            problem = (
                f"the area marked for {what} still looks the same while it should be closed (similarity "
                f"{score:.3f} >= {self._threshold:.2f})"
            )
            self._say(f"  Problem: {problem}. If it is still open, close it yourself without confirming it.")
            if attempt == self._max_attempts:
                raise CalibrationError(
                    f"Could not record {what}: {problem}, so the desk would mistake the normal screen for it. "
                    f"Run calibrate again and mark a part of {what} itself. Nothing was saved."
                )
            self._ask("  Press Enter to check again (or q to quit):")

    def _close_all(self) -> tuple[Point | None, DialogLayout | None]:
        needed = self._ut.exit_method == "close_all"
        note = " Your config uses exit_method: close_all, so the desk needs it to close positions." if needed else ""
        if not self._yes_no(f"Is there a button in the terminal that closes ALL open positions?{note}"):
            if needed:
                self._warn(
                    "no close-all button recorded: with exit_method close_all the desk can not close positions; "
                    "exits and PANIC will ask you to close them by hand"
                )
            return None, None
        button = self._point("the close-all button", "the close-all button (do NOT click it)")
        dialog: DialogLayout | None = None
        if self._yes_no(
            "Does the close-all button ask for confirmation in a dialog that you want to record? You would have "
            "to open that dialog yourself (with no positions open) and leave it open; the wizard never clicks."
        ):
            dialog = self._dialog("close_all_dialog", "the close-all confirmation dialog")
        return button, dialog

    def _order_dialog(self) -> DialogLayout | None:
        if not self._yes_no(
            "Does the terminal show a confirmation dialog after a BUY/SELL click that the desk should confirm? "
            "To record it you would have to open one yourself (the wizard never clicks)."
        ):
            return None
        return self._dialog("order_dialog", "the order confirmation dialog")

    def _error_templates(self) -> list[TemplateRef]:
        refs: list[TemplateRef] = []
        question = (
            "Do you want to record an error message that the desk should recognise? It must be showing on "
            "screen right now (the wizard never clicks)."
        )
        while len(refs) < _MAX_ERROR_TEMPLATES and self._yes_no(question):
            number = len(refs) + 1
            ref = self._template(
                f"error message {number}",
                f"error_{number}",
                f"error message {number}: a part of it that always looks the same",
                transient=True,
            )
            self._ask("Close the error message yourself, then press Enter (q to quit):")
            self._confirm_closed(f"error message {number}", ref)
            refs.append(ref)
            question = "Record another error message?"
        return refs

    # --- layout, saving, verification ---------------------------------------------------------------

    def _build_layout(
        self,
        *,
        window_title_regex: str,
        process_name: str | None,
        account_kind: Literal["demo", "live"],
        account_badge: TemplateRef,
        one_click_indicator: TemplateRef | None,
        positions_indicator: Region,
        panels: dict[str, PanelLayout],
        close_all_button: Point | None,
        close_all_dialog: DialogLayout | None,
        order_dialog: DialogLayout | None,
        error_templates: list[TemplateRef],
        park_point: Point,
        calibrated_at: datetime,
        notes: str,
    ) -> Layout:
        width, height, dpi = self._geometry
        try:
            return Layout(
                window_title_regex=window_title_regex,
                process_name=process_name,
                client_width=width,
                client_height=height,
                dpi=dpi,
                account_kind=account_kind,
                account_badge=account_badge,
                one_click_indicator=one_click_indicator,
                positions_indicator=positions_indicator,
                panels=panels,
                close_all_button=close_all_button,
                close_all_dialog=close_all_dialog,
                order_dialog=order_dialog,
                error_templates=error_templates,
                park_point=park_point,
                calibrated_at=calibrated_at,
                notes=notes,
            )
        except ValidationError as exc:
            raise CalibrationError(f"The recorded layout is not valid: {exc}. Nothing was saved.") from exc

    def _summary(self, layout: Layout) -> None:
        self._say("")
        self._say("Summary:")
        self._say(f"  window title pattern: {layout.window_title_regex!r}")
        self._say(f"  program:              {layout.process_name or '(any)'}")
        self._say(f"  client area:          {layout.client_width}x{layout.client_height} at {layout.dpi} DPI")
        self._say(f"  account kind:         {layout.account_kind.upper()}")
        self._say(f"  panels:               {', '.join(layout.panels)}")
        self._say(f"  one-click indicator:  {'yes' if layout.one_click_indicator else 'no'}")
        self._say(f"  close-all button:     {'yes' if layout.close_all_button else 'no'}")
        self._say(f"  close-all dialog:     {'yes' if layout.close_all_dialog else 'no'}")
        self._say(f"  order dialog:         {'yes' if layout.order_dialog else 'no'}")
        self._say(f"  error messages:       {len(layout.error_templates)}")
        if self._out.exists():
            self._say(f"  {self._out} already exists and will be replaced completely.")

    def _write(self, layout: Layout) -> LayoutFile:
        base = self._out.parent
        previous = _referenced_template_files(self._out)
        written: list[Path] = []
        try:
            (base / TEMPLATES_DIR).mkdir(parents=True, exist_ok=True)
            for rel, image in self._images.items():
                target = base / rel
                _write_png_atomically(target, image)
                written.append(target)
            LayoutFile(layout, base).save(self._out)
        except BaseException as exc:  # incl. Ctrl+C: never leave images behind that no layout refers to
            for path in written:
                _unlink_quietly(path)
            if isinstance(exc, OSError):
                raise CalibrationError(
                    f"Could not write the layout ({exc}). The previous layout, if any, is unchanged."
                ) from exc
            raise
        current = {(base / rel).resolve() for rel in self._images}
        for stale in sorted(previous - current):
            _unlink_quietly(stale)
        self._say(f"Saved {self._out} and {len(written)} template image(s) in {base / TEMPLATES_DIR}.")
        log.info("calibration layout saved to %s with %d template(s)", self._out, len(written))
        try:
            return LayoutFile.load(self._out)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise CalibrationError(f"The layout was written but can not be read back: {exc}") from exc

    def _verify(self, layout_file: LayoutFile) -> list[TemplateCheck]:
        self._say("")
        self._say("Re-checking every saved template against a fresh screenshot...")
        self._park_mouse()
        self._backend.sleep(self._settle)
        checks: list[TemplateCheck] = []
        for name, (ref, required) in template_refs(layout_file.layout).items():
            try:
                reference = layout_file.load_template(ref)
            except (OSError, ValueError) as exc:
                raise CalibrationError(f"The saved template {ref.image} can not be loaded: {exc}") from exc
            threshold = ref.threshold if ref.threshold is not None else self._threshold
            check = TemplateCheck(
                name=name, score=similarity(reference, self._grab(ref.region)), threshold=threshold, required=required
            )
            checks.append(check)
            if required:
                status = "ok" if check.passed else "FAILED"
                self._say(f"  {name:<28} similarity {check.score:6.3f} (needs >= {threshold:.2f})  {status}")
            else:
                status = (
                    "ok (not on screen, as expected now that it is closed)"
                    if check.passed
                    else "FAILED: matches the screen although it should be closed"
                )
                self._say(f"  {name:<28} similarity {check.score:6.3f} (needs <  {threshold:.2f})  {status}")
        return checks

    def _conclude(self, result: CalibrationResult) -> None:
        self._say("")
        if result.ok:
            self._say("Calibration saved and verified. Next: run `desk preflight` while you are logged in.")
            log.info("calibration verified (%d templates)", len(result.checks))
        else:
            names = ", ".join(c.name for c in result.failed)
            self._say(
                f"WARNING: re-verification FAILED for {names}. The layout was saved but must not be used: check "
                "that nothing covers the terminal and that every dialog/error message is closed, then run "
                "calibrate again (and `desk preflight`) before trading."
            )
            log.warning("calibration saved but re-verification failed for %s", names)
        for text in result.warnings:
            self._say(f"Note: {text}.")

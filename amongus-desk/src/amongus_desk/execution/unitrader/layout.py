"""Calibrated UniTrader screen layout.

Nothing in this file describes the real UniTrader UI: every coordinate and reference image comes from
the operator's own screen via ``desk calibrate``. All points and regions are relative to the
terminal window's *client area* (top-left = 0,0), so the window may sit anywhere on screen as long as
its client size and DPI match what was calibrated.
"""

from __future__ import annotations

import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import yaml
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...domain import ensure_utc


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Point(_Model):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Region(_Model):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)

    def contains(self, point: Point) -> bool:
        return self.x <= point.x < self.x + self.w and self.y <= point.y < self.y + self.h


class TemplateRef(_Model):
    """A screen region plus the reference image captured from it during calibration."""

    region: Region
    image: str = Field(min_length=1)  # path relative to the layout file's directory
    threshold: float | None = Field(default=None, gt=0, le=1)  # overrides the global threshold


class PanelLayout(_Model):
    lot_input: Point
    buy_button: Point
    sell_button: Point
    label: TemplateRef  # must match before anything is typed or clicked on this panel
    sl_input: Point | None = None  # only used with protective_orders: panel_fields
    tp_input: Point | None = None


class DialogLayout(_Model):
    """A confirmation dialog that may appear after a click, and the button that confirms it."""

    template: TemplateRef
    confirm_button: Point


class Layout(_Model):
    schema_version: Literal[1] = 1
    window_title_regex: str = Field(min_length=1)  # matched with re.fullmatch against the whole window title
    process_name: str | None = None
    client_width: int = Field(gt=0)
    client_height: int = Field(gt=0)
    dpi: int = Field(gt=0)
    account_kind: Literal["demo", "live"]  # declared by the operator while the badge was captured
    account_badge: TemplateRef
    one_click_indicator: TemplateRef | None = None
    positions_indicator: Region  # region that changes when a position opens/closes, and only then
    panels: dict[str, PanelLayout] = Field(min_length=1)
    close_all_button: Point | None = None
    close_all_dialog: DialogLayout | None = None
    order_dialog: DialogLayout | None = None
    error_templates: list[TemplateRef] = Field(default_factory=list)
    park_point: Point
    calibrated_at: datetime
    notes: str | None = None

    @field_validator("calibrated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _inside_client_area(self) -> Self:
        bad: list[str] = []

        def point(name: str, p: Point | None) -> None:
            if p is not None and not (p.x < self.client_width and p.y < self.client_height):
                bad.append(f"{name} ({p.x},{p.y})")

        def region(name: str, r: Region | None) -> None:
            if r is not None and (r.x + r.w > self.client_width or r.y + r.h > self.client_height):
                bad.append(f"{name} ({r.x},{r.y},{r.w}x{r.h})")

        region("account_badge", self.account_badge.region)
        region("one_click_indicator", self.one_click_indicator.region if self.one_click_indicator else None)
        region("positions_indicator", self.positions_indicator)
        point("close_all_button", self.close_all_button)
        point("park_point", self.park_point)
        for dname, dialog in (("close_all_dialog", self.close_all_dialog), ("order_dialog", self.order_dialog)):
            if dialog is not None:
                region(f"{dname}.template", dialog.template.region)
                point(f"{dname}.confirm_button", dialog.confirm_button)
        for i, tmpl in enumerate(self.error_templates):
            region(f"error_templates[{i}]", tmpl.region)
        for sym, panel in self.panels.items():
            for field in ("lot_input", "buy_button", "sell_button", "sl_input", "tp_input"):
                point(f"panels.{sym}.{field}", getattr(panel, field))
            region(f"panels.{sym}.label", panel.label.region)
        if bad:
            raise ValueError("outside the calibrated client area: " + ", ".join(bad))
        return self


class LayoutFile:
    """A layout plus the directory its template images live in."""

    def __init__(self, layout: Layout, base_dir: Path) -> None:
        self.layout = layout
        self.base_dir = base_dir

    @classmethod
    def load(cls, path: Path) -> LayoutFile:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(Layout.model_validate(raw), path.resolve().parent)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.layout.model_dump(mode="json", exclude_none=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(yaml.safe_dump(data, sort_keys=False))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        if sys.platform != "win32":  # persist the rename itself (best effort; not supported on Windows)
            with contextlib.suppress(OSError):
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

    def template_path(self, ref: TemplateRef) -> Path:
        candidate = (self.base_dir / ref.image).resolve()
        if self.base_dir.resolve() not in candidate.parents:
            raise ValueError(f"template path escapes the layout directory: {ref.image}")
        return candidate

    def load_template(self, ref: TemplateRef) -> Image.Image:
        with Image.open(self.template_path(ref)) as img:
            img.load()
            rgb = img.convert("RGB")
        if rgb.size != (ref.region.w, ref.region.h):
            raise ValueError(
                f"template {ref.image} is {rgb.size[0]}x{rgb.size[1]} but its region is {ref.region.w}x{ref.region.h}"
            )
        return rgb

    def all_templates(self) -> dict[str, TemplateRef]:
        """Every template keyed by a human-readable name (for preflight reports)."""
        lay = self.layout
        out: dict[str, TemplateRef] = {"account_badge": lay.account_badge}
        if lay.one_click_indicator:
            out["one_click_indicator"] = lay.one_click_indicator
        for sym, panel in lay.panels.items():
            out[f"panel:{sym}"] = panel.label
        return out

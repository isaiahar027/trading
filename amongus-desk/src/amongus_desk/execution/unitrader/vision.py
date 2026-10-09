"""Small, dependency-light image comparisons used to verify the terminal's screen before acting.

All math is done on grayscale ``float64`` arrays (ITU-R 601 luma of the RGB pixels) with numpy. The
functions compare a live capture against a reference image that the operator captured during
``desk calibrate``; nothing here knows anything about what the real UniTrader terminal looks like.

Comparisons are deliberately conservative: an image pair that can not be compared meaningfully (one
flat image against a textured one) scores ``0.0``, which every caller treats as "does not match".
"""

from __future__ import annotations

import logging

import numpy as np
import numpy.typing as npt
from PIL import Image

log = logging.getLogger(__name__)

FloatArray = npt.NDArray[np.float64]

#: Standard deviation (grey levels, 0..255) at or below which an image counts as flat/uniform, and the
#: largest mean difference at which two flat images still count as the same picture.
FLAT_TOLERANCE = 2.0

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float64)


def _rgb(img: Image.Image) -> FloatArray:
    if img.width <= 0 or img.height <= 0:
        raise ValueError("cannot compare an empty image")
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    return np.asarray(rgb, dtype=np.float64)


def _gray(img: Image.Image) -> FloatArray:
    return _rgb(img) @ _LUMA


def _same_size(a: Image.Image, b: Image.Image) -> None:
    if a.size != b.size:
        raise ValueError(f"image sizes differ: {a.size[0]}x{a.size[1]} vs {b.size[0]}x{b.size[1]}")


def similarity(a: Image.Image, b: Image.Image) -> float:
    """Zero-mean normalized cross-correlation of two equally sized images, in ``-1..1``.

    ``1.0`` means the same pattern. Two flat images count as ``1.0`` when their means agree within
    :data:`FLAT_TOLERANCE` grey levels, else ``0.0``; one flat image against a textured one is ``0.0``.
    Raises :class:`ValueError` when the sizes differ or an image is empty.
    """
    _same_size(a, b)
    ga = _gray(a)
    gb = _gray(b)
    mean_a = float(ga.mean())
    mean_b = float(gb.mean())
    flat_a = float(ga.std()) <= FLAT_TOLERANCE
    flat_b = float(gb.std()) <= FLAT_TOLERANCE
    if flat_a and flat_b:
        return 1.0 if abs(mean_a - mean_b) <= FLAT_TOLERANCE else 0.0
    if flat_a or flat_b:
        return 0.0
    da = ga - mean_a
    db = gb - mean_b
    denom = float(np.sqrt(float((da * da).sum()) * float((db * db).sum())))
    if denom <= 0.0 or not np.isfinite(denom):
        return 0.0
    score = float((da * db).sum()) / denom
    return max(-1.0, min(1.0, score))


def is_blank(img: Image.Image, tolerance: float = 2.0) -> bool:
    """True when the image is nearly uniform: grayscale standard deviation ``<= tolerance``.

    A locked or disconnected Windows session captures as solid black, which this detects.
    """
    if tolerance < 0:
        raise ValueError("tolerance must not be negative")
    return float(_gray(img).std()) <= tolerance


def changed_fraction(a: Image.Image, b: Image.Image, pixel_tolerance: int = 24) -> float:
    """Share of pixels (``0..1``) whose grayscale value differs by more than ``pixel_tolerance``."""
    if pixel_tolerance < 0:
        raise ValueError("pixel_tolerance must not be negative")
    _same_size(a, b)
    diff = np.abs(_gray(a) - _gray(b))
    return float(np.count_nonzero(diff > pixel_tolerance)) / float(diff.size)


def mean_color_delta(a: Image.Image, b: Image.Image) -> float:
    """Largest per-channel difference of the mean RGB colour of two equally sized images (0..255).

    :func:`similarity` is blind to uniform brightness/colour shifts (it is zero-mean normalized), so a
    badge that differs only in colour would still correlate perfectly. Callers that must tell such
    images apart combine both measures.
    """
    _same_size(a, b)
    ma = _rgb(a).reshape(-1, 3).mean(axis=0)
    mb = _rgb(b).reshape(-1, 3).mean(axis=0)
    return float(np.max(np.abs(ma - mb)))

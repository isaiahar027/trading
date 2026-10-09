from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from amongus_desk.execution.unitrader.vision import (
    FLAT_TOLERANCE,
    changed_fraction,
    is_blank,
    mean_color_delta,
    similarity,
)


def gray_image(values: np.ndarray) -> Image.Image:
    arr = np.clip(values, 0, 255).astype(np.uint8)
    return Image.fromarray(np.stack([arr, arr, arr], axis=-1), "RGB")


def pattern(w: int = 40, h: int = 20, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(h, w)).astype(np.float64)


def flat(value: int, w: int = 40, h: int = 20) -> Image.Image:
    return Image.new("RGB", (w, h), (value, value, value))


# ------------------------------------------------------------------------------------------ similarity
def test_identical_images_score_one() -> None:
    img = gray_image(pattern())
    assert similarity(img, img.copy()) == pytest.approx(1.0)


def test_inverted_image_scores_minus_one() -> None:
    values = pattern()
    assert similarity(gray_image(values), gray_image(255 - values)) == pytest.approx(-1.0)


def test_similarity_is_invariant_to_uniform_brightness_and_contrast() -> None:
    values = pattern() * 0.5  # 0..127
    assert similarity(gray_image(values), gray_image(values * 1.5 + 40)) == pytest.approx(1.0, abs=0.01)


def test_unrelated_patterns_score_low() -> None:
    assert abs(similarity(gray_image(pattern(seed=1)), gray_image(pattern(seed=2)))) < 0.2


def test_score_is_bounded() -> None:
    for seed in range(5):
        score = similarity(gray_image(pattern(seed=seed)), gray_image(pattern(seed=seed + 10)))
        assert -1.0 <= score <= 1.0


def test_two_flat_images_with_the_same_mean_count_as_identical() -> None:
    assert similarity(flat(0), flat(0)) == 1.0
    assert similarity(flat(100), flat(101)) == 1.0  # within FLAT_TOLERANCE


def test_two_flat_images_with_different_means_count_as_different() -> None:
    assert similarity(flat(0), flat(200)) == 0.0
    assert similarity(flat(100), flat(100 + int(FLAT_TOLERANCE) + 2)) == 0.0


def test_flat_against_textured_is_zero_fail_closed() -> None:
    # e.g. a locked (black) screen against a calibrated badge: never a match
    textured = gray_image(pattern())
    assert similarity(flat(0), textured) == 0.0
    assert similarity(textured, flat(0)) == 0.0


def test_size_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="sizes differ"):
        similarity(flat(0, 10, 10), flat(0, 10, 11))


def test_empty_image_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        similarity(Image.new("RGB", (0, 0)), Image.new("RGB", (0, 0)))


def test_other_modes_are_converted() -> None:
    values = pattern()
    rgb = gray_image(values)
    assert similarity(rgb.convert("L"), rgb) == pytest.approx(1.0, abs=1e-3)
    assert similarity(rgb.convert("RGBA"), rgb) == pytest.approx(1.0)


def test_color_only_difference_is_invisible_to_ncc_but_seen_by_mean_color_delta() -> None:
    base = np.zeros((20, 40, 3), dtype=np.uint8)
    base[5:15, 5:35] = 255  # white block
    orange = base.copy()
    orange[base.sum(axis=-1) == 0] = (240, 170, 40)
    red = base.copy()
    red[base.sum(axis=-1) == 0] = (190, 30, 30)
    a = Image.fromarray(orange, "RGB")
    b = Image.fromarray(red, "RGB")
    assert similarity(a, b) > 0.95  # same shape, different colour
    assert mean_color_delta(a, b) > 32


# ------------------------------------------------------------------------------------------ is_blank
def test_black_screen_is_blank() -> None:
    assert is_blank(flat(0, 200, 100))


def test_nearly_uniform_is_blank_within_tolerance() -> None:
    values = np.full((20, 40), 120.0)
    values[0, 0] = 125  # a single slightly brighter pixel
    assert is_blank(gray_image(values))
    assert not is_blank(gray_image(values), tolerance=0.0)


def test_textured_is_not_blank() -> None:
    assert not is_blank(gray_image(pattern()))


def test_is_blank_rejects_negative_tolerance() -> None:
    with pytest.raises(ValueError):
        is_blank(flat(0), tolerance=-1)


# ------------------------------------------------------------------------------------------ changed_fraction
def test_changed_fraction_identical_is_zero() -> None:
    img = gray_image(pattern())
    assert changed_fraction(img, img.copy()) == 0.0


def test_changed_fraction_all_changed_is_one() -> None:
    assert changed_fraction(flat(0), flat(255)) == 1.0


def test_changed_fraction_counts_the_changed_share() -> None:
    a = np.zeros((10, 10))
    b = a.copy()
    b[:5, :] = 200
    assert changed_fraction(gray_image(a), gray_image(b)) == pytest.approx(0.5)


def test_pixel_tolerance_boundary() -> None:
    a = np.full((10, 10), 100.0)
    at_tolerance = gray_image(a + 24)
    above = gray_image(a + 25)
    assert changed_fraction(gray_image(a), at_tolerance) == 0.0  # "more than" the tolerance counts
    assert changed_fraction(gray_image(a), above) == 1.0
    assert changed_fraction(gray_image(a), above, pixel_tolerance=30) == 0.0


def test_changed_fraction_size_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        changed_fraction(flat(0, 10, 10), flat(0, 11, 10))


def test_changed_fraction_rejects_negative_tolerance() -> None:
    with pytest.raises(ValueError):
        changed_fraction(flat(0), flat(0), pixel_tolerance=-1)


# ------------------------------------------------------------------------------------------ mean_color_delta
def test_mean_color_delta_same_image_is_zero() -> None:
    img = gray_image(pattern())
    assert mean_color_delta(img, img) == 0.0


def test_mean_color_delta_takes_the_worst_channel() -> None:
    a = Image.new("RGB", (4, 4), (10, 20, 30))
    b = Image.new("RGB", (4, 4), (15, 20, 90))
    assert mean_color_delta(a, b) == pytest.approx(60.0)


def test_mean_color_delta_size_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        mean_color_delta(flat(0, 4, 4), flat(0, 5, 4))

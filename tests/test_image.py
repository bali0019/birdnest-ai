"""Unit tests for _image.prepare_multi_image and downscale_jpeg_b64."""

from __future__ import annotations

import base64

import cv2
import numpy as np
import pytest

from cardinal_nest_monitor._image import (
    downscale_jpeg_b64,
    prepare_multi_image,
)


def _make_jpeg(width: int = 1600, height: int = 1200) -> bytes:
    """Generate a synthetic JPEG with obvious color regions so crops differ."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    # left stripe blue, center stripe green, right stripe red — the center
    # crop should end up dominated by green.
    third = width // 3
    img[:, :third] = (255, 0, 0)          # BGR blue
    img[:, third : 2 * third] = (0, 255, 0)  # BGR green
    img[:, 2 * third :] = (0, 0, 255)      # BGR red
    ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    assert ok
    return enc.tobytes()


def _assert_valid_jpeg_block(block: dict) -> bytes:
    """Shape + content checks for an Anthropic image block. Returns decoded JPEG bytes."""
    assert block["type"] == "image"
    src = block["source"]
    assert src["type"] == "base64"
    assert src["media_type"] == "image/jpeg"
    data = src["data"]
    assert isinstance(data, str)
    assert data  # non-empty
    # raw base64, no data: URI prefix
    assert not data.startswith("data:")
    raw = base64.standard_b64decode(data)
    # JPEG magic bytes
    assert raw[:3] == b"\xff\xd8\xff", "block data is not a valid JPEG"
    return raw


def test_prepare_multi_image_returns_three_blocks() -> None:
    jpeg = _make_jpeg(1600, 1200)
    blocks = prepare_multi_image(jpeg)
    assert isinstance(blocks, list)
    assert len(blocks) == 3


def test_prepare_multi_image_blocks_are_valid_base64_jpegs() -> None:
    jpeg = _make_jpeg(1600, 1200)
    blocks = prepare_multi_image(jpeg)
    for block in blocks:
        _assert_valid_jpeg_block(block)


def test_prepare_multi_image_variants_have_expected_sizes() -> None:
    """Decode each variant and confirm the intended crop strategy."""
    src_w, src_h = 1600, 1200
    jpeg = _make_jpeg(src_w, src_h)
    blocks = prepare_multi_image(jpeg)

    sizes = []
    for block in blocks:
        raw = _assert_valid_jpeg_block(block)
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        assert img is not None
        sizes.append(img.shape[:2])  # (h, w)

    full_h, full_w = sizes[0]
    center_h, center_w = sizes[1]
    overview_h, overview_w = sizes[2]

    # full is ~1024px wide when source is 1600px wide
    assert full_w == 1024, f"full variant width should be 1024, got {full_w}"

    # center crop is the middle 60% — expect about 960x720 (width x height)
    # for a 1600x1200 source. Allow some rounding slack.
    expected_cw = int(round(src_w * 0.6))
    expected_ch = int(round(src_h * 0.6))
    assert abs(center_w - expected_cw) <= 2
    assert abs(center_h - expected_ch) <= 2

    # overview is ~512px wide
    assert overview_w == 512, f"overview variant width should be 512, got {overview_w}"


def test_prepare_multi_image_center_crop_is_actually_center() -> None:
    """Mean pixel color of the center crop should be dominated by green
    (the middle stripe of the synthetic image)."""
    jpeg = _make_jpeg(1600, 1200)
    blocks = prepare_multi_image(jpeg)
    center_raw = _assert_valid_jpeg_block(blocks[1])
    arr = np.frombuffer(center_raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert img is not None
    # OpenCV is BGR. Green-dominant means channel 1 is highest.
    mean_b, mean_g, mean_r = img.reshape(-1, 3).mean(axis=0)
    assert mean_g > mean_b
    assert mean_g > mean_r


def test_prepare_multi_image_invalid_bytes_raises() -> None:
    with pytest.raises(ValueError):
        prepare_multi_image(b"not a jpeg")


def test_prepare_multi_image_small_source_does_not_upscale() -> None:
    """If source is smaller than the 'full' target, full variant keeps original size."""
    jpeg = _make_jpeg(600, 400)
    blocks = prepare_multi_image(jpeg)
    raw = _assert_valid_jpeg_block(blocks[0])
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert img is not None
    h, w = img.shape[:2]
    # Original was 600x400 — full variant should keep that (no upscale).
    assert w == 600
    assert h == 400


def test_downscale_jpeg_b64_still_works() -> None:
    """Sanity check that the original single-image helper still functions."""
    jpeg = _make_jpeg(1600, 1200)
    b64 = downscale_jpeg_b64(jpeg, max_width=1024)
    assert isinstance(b64, str)
    raw = base64.standard_b64decode(b64)
    assert raw[:3] == b"\xff\xd8\xff"

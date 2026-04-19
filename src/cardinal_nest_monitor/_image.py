"""JPEG downscale + base64 helper for Anthropic image blocks."""

from __future__ import annotations

import base64
import logging

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Upper bound on accepted JPEG input. Blink snaps are ~200 KB; 20 MB gives
# 100x headroom for a genuinely high-res camera one day while preventing a
# malformed/malicious multi-GB "JPEG" from decoding into gigapixels of RAM
# (cv2.imdecode has no size ceiling of its own).
_MAX_JPEG_BYTES = 20 * 1024 * 1024  # 20 MB


def downscale_jpeg_b64(jpeg: bytes, max_width: int) -> str:
    """Decode a JPEG, downscale to <= max_width preserving aspect, re-encode
    as JPEG, and return raw base64 (no data: prefix).

    Raises ValueError if the bytes cannot be decoded as a JPEG, or if the
    input exceeds _MAX_JPEG_BYTES.
    """
    if len(jpeg) > _MAX_JPEG_BYTES:
        raise ValueError(
            f"JPEG input too large: {len(jpeg)} bytes > {_MAX_JPEG_BYTES}"
        )
    arr = np.frombuffer(jpeg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cv2.imdecode returned None — bytes are not a valid JPEG")

    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / float(w)
        new_w = max_width
        new_h = max(1, int(round(h * scale)))
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise ValueError("cv2.imencode failed to produce JPEG")
    return base64.standard_b64encode(enc.tobytes()).decode("ascii")


def _image_block(b64: str) -> dict:
    """Wrap a raw base64 JPEG string in an Anthropic vision content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": b64,
        },
    }


def _encode_jpeg_b64(img: "np.ndarray", quality: int = 88) -> str:
    """Re-encode a decoded image array as JPEG + base64."""
    ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise ValueError("cv2.imencode failed to produce JPEG")
    return base64.standard_b64encode(enc.tobytes()).decode("ascii")


def prepare_multi_image(jpeg: bytes) -> list[dict]:
    """Return a list of 3 Anthropic content blocks, one per crop variant.

    Each block is the standard vision format:
        {"type": "image", "source": {"type": "base64",
         "media_type": "image/jpeg", "data": ...}}

    Variants (in order):
      1. full     — current downscale (~1024px wide), full frame.
      2. center   — middle 60% of frame, re-encoded at high quality.
                    Pulls nest-cup detail forward for the analyzer.
      3. overview — ~512px downscale, full frame. Low-detail context so
                    the model can place the zoomed crop in the scene.

    Raises ValueError if the JPEG cannot be decoded, or if the input
    exceeds _MAX_JPEG_BYTES.
    """
    if len(jpeg) > _MAX_JPEG_BYTES:
        raise ValueError(
            f"JPEG input too large: {len(jpeg)} bytes > {_MAX_JPEG_BYTES}"
        )
    arr = np.frombuffer(jpeg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("cv2.imdecode returned None — bytes are not a valid JPEG")

    h, w = img.shape[:2]

    # 1. Full frame — downscaled to ~1024px wide (matches current single-
    #    image behavior at comparable fidelity). Wider than 1024 gets resized.
    full_target_w = 1024
    if w > full_target_w:
        scale = full_target_w / float(w)
        full_img = cv2.resize(
            img,
            (full_target_w, max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        full_img = img
    full_b64 = _encode_jpeg_b64(full_img, quality=85)

    # 2. Center crop — middle 60% (both dimensions). We don't further
    #    downscale: the crop itself is already smaller, and we want to
    #    preserve detail on the nest cup which typically sits near center.
    #    If the crop is unusually large (e.g. huge source frames), cap at
    #    1280px wide to keep token cost bounded.
    crop_frac = 0.6
    cw = max(1, int(round(w * crop_frac)))
    ch = max(1, int(round(h * crop_frac)))
    x0 = (w - cw) // 2
    y0 = (h - ch) // 2
    center_img = img[y0 : y0 + ch, x0 : x0 + cw]
    center_max_w = 1280
    ch_actual, cw_actual = center_img.shape[:2]
    if cw_actual > center_max_w:
        scale = center_max_w / float(cw_actual)
        center_img = cv2.resize(
            center_img,
            (center_max_w, max(1, int(round(ch_actual * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    # Slightly higher quality on the center crop — this is the detail view.
    center_b64 = _encode_jpeg_b64(center_img, quality=90)

    # 3. Overview — ~512px downscale of the full frame. Token-cheap
    #    context that reminds the model where the crop came from.
    overview_target_w = 512
    if w > overview_target_w:
        scale = overview_target_w / float(w)
        overview_img = cv2.resize(
            img,
            (overview_target_w, max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        overview_img = img
    overview_b64 = _encode_jpeg_b64(overview_img, quality=80)

    return [
        _image_block(full_b64),
        _image_block(center_b64),
        _image_block(overview_b64),
    ]

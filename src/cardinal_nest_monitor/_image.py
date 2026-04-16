"""JPEG downscale + base64 helper for Anthropic image blocks."""

from __future__ import annotations

import base64
import logging

import cv2
import numpy as np

log = logging.getLogger(__name__)


def downscale_jpeg_b64(jpeg: bytes, max_width: int) -> str:
    """Decode a JPEG, downscale to <= max_width preserving aspect, re-encode
    as JPEG, and return raw base64 (no data: prefix).

    Raises ValueError if the bytes cannot be decoded as a JPEG.
    """
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

"""Cheap frame-change detection before VLM inference.

Two independent metrics against the last VLM-accepted frame:
- MAD (mean absolute difference) on 64x48 grayscale, 0-255 scale
- dHash (8x8 -> 64-bit) hamming distance

Pure Pillow + integer math: ~1 ms per frame, no numpy dependency.
"""

from __future__ import annotations

import io

from PIL import Image

_GRID_W, _GRID_H = 64, 48


def _grayscale_grid(jpeg: bytes, w: int = _GRID_W, h: int = _GRID_H) -> list[int]:
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize((w, h))
    return list(img.getdata())


def dhash(jpeg: bytes) -> int:
    """64-bit difference hash: bit set when pixel dims its right neighbour."""
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize((9, 8))
    px = list(img.getdata())
    bits = 0
    bit = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            if px[base + col] > px[base + col + 1]:
                bits |= 1 << bit
            bit += 1
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def mean_abs_diff(a: list[int], b: list[int]) -> float:
    if len(a) != len(b) or not a:
        return 255.0
    total = 0
    for x, y in zip(a, b):
        total += abs(x - y)
    return total / len(a)


class ChangeDetector:
    """Stateful detector comparing each frame to the last accepted one."""

    def __init__(self, mad_threshold: float = 6.0, hash_threshold: int = 12) -> None:
        self.mad_threshold = mad_threshold
        self.hash_threshold = hash_threshold
        self._last_grid: list[int] | None = None
        self._last_hash: int | None = None

    def reset(self) -> None:
        self._last_grid = None
        self._last_hash = None

    def evaluate(self, jpeg: bytes) -> tuple[bool, float, int]:
        """Returns (changed, mad, hamming). First frame is always 'changed'."""
        grid = _grayscale_grid(jpeg)
        h = dhash(jpeg)
        if self._last_grid is None or self._last_hash is None:
            self._last_grid, self._last_hash = grid, h
            return True, 255.0, 64
        mad = mean_abs_diff(grid, self._last_grid)
        dist = hamming(h, self._last_hash)
        changed = mad >= self.mad_threshold or dist >= self.hash_threshold
        if changed:
            self._last_grid, self._last_hash = grid, h
        return changed, round(mad, 2), dist

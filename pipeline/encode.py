"""Quantizzazione dei campi e scrittura PNG senza dipendenze grafiche."""

from __future__ import annotations

import binascii
from pathlib import Path
import struct
import zlib

import numpy as np


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def quantize(field: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    values = np.asarray(field, dtype=np.float64)
    finite = np.isfinite(values)
    if not finite.any():
        raise ValueError("campo privo di valori finiti")
    minimum = float(values[finite].min())
    maximum = float(values[finite].max())
    step = (maximum - minimum) / 255.0
    if step == 0.0:
        encoded = np.zeros(values.shape, dtype=np.uint8)
    else:
        safe_values = np.where(finite, values, minimum)
        encoded = np.rint((safe_values - minimum) / step).clip(0, 255).astype(np.uint8)
    encoded[~finite] = 0
    return encoded, {
        "min": minimum,
        "max": maximum,
        "quantization_step": step,
    }


def dequantize(encoded: np.ndarray, minimum: float, maximum: float) -> np.ndarray:
    step = (maximum - minimum) / 255.0
    return minimum + np.asarray(encoded, dtype=np.float64) * step


def _chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", binascii.crc32(payload) & 0xFFFFFFFF)


def write_png(path: str | Path, pixels: np.ndarray) -> None:
    array = np.ascontiguousarray(pixels, dtype=np.uint8)
    if array.ndim == 2:
        height, width = array.shape
        color_type = 0
    elif array.ndim == 3 and array.shape[2] == 4:
        height, width, _ = array.shape
        color_type = 6
    else:
        raise ValueError("il PNG deve essere grayscale (H,W) o RGBA (H,W,4)")
    scanlines = b"".join(b"\x00" + row.tobytes() for row in array)
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    png = PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(scanlines, 9)) + _chunk(b"IEND", b"")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(png)


def read_png(path: str | Path) -> np.ndarray:
    """Legge i PNG prodotti da :func:`write_png` (utile per verifica)."""
    data = Path(path).read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("firma PNG non valida")
    offset = len(PNG_SIGNATURE)
    compressed = bytearray()
    width = height = color_type = None
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if (depth, compression, filtering, interlace) != (8, 0, 0, 0):
                raise ValueError("formato PNG non supportato")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    channels = 1 if color_type == 0 else 4 if color_type == 6 else 0
    if not channels or width is None or height is None:
        raise ValueError("PNG incompleto o tipo colore non supportato")
    raw = zlib.decompress(compressed)
    stride = width * channels
    rows = []
    for row in range(height):
        start = row * (stride + 1)
        if raw[start] != 0:
            raise ValueError("filtro PNG non supportato")
        rows.append(np.frombuffer(raw[start + 1 : start + 1 + stride], dtype=np.uint8))
    result = np.stack(rows)
    return result.reshape(height, width) if channels == 1 else result.reshape(height, width, channels)


def encode_scalar(field: np.ndarray, path: str | Path) -> dict:
    pixels, metadata = quantize(field)
    write_png(path, pixels)
    return {"encoding": "grayscale8", **metadata}


def encode_wind(u: np.ndarray, v: np.ndarray, path: str | Path) -> dict:
    if np.shape(u) != np.shape(v):
        raise ValueError("u e v devono avere la stessa griglia")
    red, u_metadata = quantize(u)
    green, v_metadata = quantize(v)
    rgba = np.zeros((*red.shape, 4), dtype=np.uint8)
    rgba[..., 0] = red
    rgba[..., 1] = green
    rgba[..., 3] = np.where(np.isfinite(u) & np.isfinite(v), 255, 0)
    write_png(path, rgba)
    return {
        "encoding": "rgba8",
        "channels": {"u": "R", "v": "G", "mask": "A"},
        "min": [u_metadata["min"], v_metadata["min"]],
        "max": [u_metadata["max"], v_metadata["max"]],
        "quantization_step": [
            u_metadata["quantization_step"],
            v_metadata["quantization_step"],
        ],
    }

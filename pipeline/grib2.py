"""Lettore GRIB2 minimo per griglie regolari latitudine/longitudine.

Sono supportati il template di griglia 3.0, il simple packing 5.0 e il
packing CCSDS/AEC 5.42 usato dagli open data ECMWF. Non usa ecCodes.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterator

import imagecodecs
import numpy as np


class GribError(ValueError):
    """GRIB2 non valido o non supportato."""


def sign_magnitude(raw: bytes | memoryview) -> int:
    """Decodifica un intero GRIB con segno (segno e modulo)."""
    value = int.from_bytes(raw, "big")
    sign_bit = 1 << (8 * len(raw) - 1)
    return -(value & ~sign_bit) if value & sign_bit else value


# Nome breve dei parametri di superficie usati dalla pipeline. ``tp`` ha un
# numero nella tabella locale ECMWF, mentre gli altri sono WMO.
PARAMETERS = {
    (0, 0, 0): "2t",
    (0, 1, 8): "tp",
    (0, 1, 193): "tp",
    (0, 2, 2): "10u",
    (0, 2, 3): "10v",
    (0, 2, 22): "gust",
    (0, 3, 1): "msl",
}


@dataclass(frozen=True)
class Grid:
    ni: int
    nj: int
    lat1: float
    lon1: float
    lat2: float
    lon2: float
    di: float
    dj: float
    scanning_mode: int

    @property
    def shape(self) -> tuple[int, int]:
        return self.nj, self.ni

    @property
    def point_count(self) -> int:
        return self.ni * self.nj

    def cell(self, lat: float, lon: float) -> tuple[int, int]:
        """Restituisce ``(j, i)`` della cella più vicina."""
        i_sign = -1.0 if self.scanning_mode & 0x80 else 1.0
        j_sign = 1.0 if self.scanning_mode & 0x40 else -1.0
        lon_delta = ((lon - self.lon1 + 180.0) % 360.0) - 180.0
        if i_sign > 0 and lon_delta < 0:
            lon_delta += 360.0
        elif i_sign < 0 and lon_delta > 0:
            lon_delta -= 360.0
        i = int(round(lon_delta / (i_sign * self.di))) % self.ni
        j = int(round((lat - self.lat1) / (j_sign * self.dj)))
        if not 0 <= j < self.nj:
            raise IndexError(f"latitudine fuori griglia: {lat}")
        return j, i


@dataclass(frozen=True)
class Field:
    values: np.ndarray
    grid: Grid
    discipline: int
    category: int
    number: int
    parameter: str
    data_template: int
    reference_value: float
    binary_scale_factor: int
    decimal_scale_factor: int
    bits_per_value: int

    def at(self, lat: float, lon: float) -> float:
        return float(self.values[self.grid.cell(lat, lon)])


def iter_messages(data: bytes | bytearray | memoryview) -> Iterator[memoryview]:
    """Itera i messaggi GRIB2 concatenati senza copiarne il contenuto."""
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        if len(view) - offset < 16 or view[offset : offset + 4].tobytes() != b"GRIB":
            raise GribError(f"firma GRIB mancante all'offset {offset}")
        if view[offset + 7] != 2:
            raise GribError("è supportata soltanto l'edizione GRIB 2")
        length = struct.unpack_from(">Q", view, offset + 8)[0]
        if length < 20 or offset + length > len(view):
            raise GribError("lunghezza del messaggio GRIB non valida")
        message = view[offset : offset + length]
        if message[-4:].tobytes() != b"7777":
            raise GribError("terminatore GRIB mancante")
        yield message
        offset += length


def iter_sections(message: bytes | memoryview) -> Iterator[tuple[int, memoryview]]:
    """Itera ``(numero, dati)`` delle sezioni 1..7."""
    view = memoryview(message)
    offset = 16
    limit = len(view) - 4
    while offset < limit:
        if offset + 5 > limit:
            raise GribError("intestazione di sezione troncata")
        length = struct.unpack_from(">I", view, offset)[0]
        if length < 5 or offset + length > limit:
            raise GribError("lunghezza di sezione non valida")
        section = view[offset : offset + length]
        yield int(section[4]), section
        offset += length
    if offset != limit:
        raise GribError("sezioni GRIB non allineate")


def _angle(raw: memoryview, signed: bool, unit: float) -> float:
    value = sign_magnitude(raw) if signed else int.from_bytes(raw, "big")
    return value * unit


def parse_grid(section3: bytes | memoryview) -> Grid:
    section = memoryview(section3)
    if len(section) < 72:
        raise GribError("sezione 3 troncata")
    template = struct.unpack_from(">H", section, 12)[0]
    if template != 0:
        raise GribError(f"template di griglia 3.{template} non supportato")
    ni = struct.unpack_from(">I", section, 30)[0]
    nj = struct.unpack_from(">I", section, 34)[0]
    basic_angle = struct.unpack_from(">I", section, 38)[0]
    subdivisions = struct.unpack_from(">I", section, 42)[0]
    explicit_angles = (
        basic_angle not in (0, 0xFFFFFFFF)
        and subdivisions not in (0, 0xFFFFFFFF)
    )
    unit = basic_angle / subdivisions if explicit_angles else 1e-6
    return Grid(
        ni=ni,
        nj=nj,
        lat1=_angle(section[46:50], True, unit),
        lon1=_angle(section[50:54], False, unit),
        lat2=_angle(section[55:59], True, unit),
        lon2=_angle(section[59:63], False, unit),
        di=_angle(section[63:67], False, unit),
        dj=_angle(section[67:71], False, unit),
        scanning_mode=int(section[71]),
    )


def _unpack_simple(data: memoryview, count: int, nbits: int) -> np.ndarray:
    if nbits == 0:
        return np.zeros(count, dtype=np.uint64)
    required = count * nbits
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="big")
    if bits.size < required:
        raise GribError("dati simple packing troncati")
    rows = bits[:required].reshape(count, nbits).astype(np.uint64)
    weights = np.left_shift(np.uint64(1), np.arange(nbits - 1, -1, -1, dtype=np.uint64))
    return rows @ weights


def _unpack_aec(data: memoryview, count: int, nbits: int, section5: memoryview) -> np.ndarray:
    if nbits == 0:
        return np.zeros(count, dtype=np.uint64)
    byte_width = (nbits + 7) // 8
    if byte_width not in (1, 2, 3, 4):
        raise GribError(f"ampiezza CCSDS non supportata: {nbits} bit")
    flags = int(section5[21])
    decoded = imagecodecs.aec_decode(
        data,
        bitspersample=nbits,
        flags=flags,
        blocksize=int(section5[22]),
        rsi=struct.unpack_from(">H", section5, 23)[0],
        out=count * byte_width,
    )
    big_endian = bool(flags & 0x08)  # AEC_DATA_MSB
    if byte_width == 3:
        raw = np.frombuffer(decoded, dtype=np.uint8).reshape(-1, 3)
        if big_endian:
            values = (
                (raw[:, 0].astype(np.uint32) << 16)
                | (raw[:, 1].astype(np.uint32) << 8)
                | raw[:, 2].astype(np.uint32)
            )
        else:
            values = (
                raw[:, 0].astype(np.uint32)
                | (raw[:, 1].astype(np.uint32) << 8)
                | (raw[:, 2].astype(np.uint32) << 16)
            )
    else:
        byte_order = ">" if big_endian else "<"
        dtype = {1: "u1", 2: f"{byte_order}u2", 4: f"{byte_order}u4"}[byte_width]
        values = np.frombuffer(decoded, dtype=dtype)
    if values.size < count:
        raise GribError("uscita del decoder CCSDS troncata")
    return values[:count]


def _bitmap(section6: memoryview | None, point_count: int) -> np.ndarray | None:
    if section6 is None:
        return None
    indicator = int(section6[5])
    if indicator == 255:
        return None
    if indicator != 0:
        raise GribError(f"indicatore bitmap {indicator} non supportato")
    bits = np.unpackbits(np.frombuffer(section6[6:], dtype=np.uint8), bitorder="big")
    if bits.size < point_count:
        raise GribError("bitmap troncata")
    return bits[:point_count].astype(bool)


def _reshape_scanning(values: np.ndarray, grid: Grid) -> np.ndarray:
    adjacent_j = bool(grid.scanning_mode & 0x20)
    alternating = bool(grid.scanning_mode & 0x10)
    if adjacent_j:
        result = values.reshape(grid.ni, grid.nj).T
        if alternating:
            result[:, 1::2] = result[::-1, 1::2]
    else:
        result = values.reshape(grid.nj, grid.ni)
        if alternating:
            result[1::2] = result[1::2, ::-1]
    return result


def decode_message(message: bytes | memoryview) -> Field:
    """Decodifica un singolo messaggio in un campo numerico ``(Nj, Ni)``."""
    view = memoryview(message)
    if view[:4].tobytes() != b"GRIB":
        raise GribError("firma GRIB mancante")
    sections = {number: raw for number, raw in iter_sections(view)}
    missing = {3, 4, 5, 7} - sections.keys()
    if missing:
        raise GribError(f"sezioni obbligatorie mancanti: {sorted(missing)}")

    grid = parse_grid(sections[3])
    section4 = sections[4]
    category, number = int(section4[9]), int(section4[10])
    discipline = int(view[6])

    section5 = sections[5]
    represented = struct.unpack_from(">I", section5, 5)[0]
    template = struct.unpack_from(">H", section5, 9)[0]
    reference = struct.unpack_from(">f", section5, 11)[0]
    binary_scale = sign_magnitude(section5[15:17])
    decimal_scale = sign_magnitude(section5[17:19])
    nbits = int(section5[19])
    packed = sections[7][5:]
    if template == 0:
        integers = _unpack_simple(packed, represented, nbits)
    elif template == 42:
        if len(section5) < 25:
            raise GribError("template 5.42 troncato")
        integers = _unpack_aec(packed, represented, nbits, section5)
    else:
        raise GribError(f"template dati 5.{template} non supportato")

    decoded = (reference + integers.astype(np.float64) * (2.0**binary_scale)) / (
        10.0**decimal_scale
    )
    bitmap = _bitmap(sections.get(6), grid.point_count)
    if bitmap is not None:
        if int(bitmap.sum()) != represented:
            raise GribError("bitmap incoerente con il numero di valori")
        expanded = np.full(grid.point_count, np.nan, dtype=np.float64)
        expanded[bitmap] = decoded
        decoded = expanded
    elif represented != grid.point_count:
        raise GribError("numero di valori incoerente con la griglia")

    return Field(
        values=_reshape_scanning(decoded, grid),
        grid=grid,
        discipline=discipline,
        category=category,
        number=number,
        parameter=PARAMETERS.get((discipline, category, number), f"{discipline}.{category}.{number}"),
        data_template=template,
        reference_value=reference,
        binary_scale_factor=binary_scale,
        decimal_scale_factor=decimal_scale,
        bits_per_value=nbits,
    )


def decode(data: bytes | bytearray | memoryview) -> list[Field]:
    """Decodifica tutti i messaggi contenuti in ``data``."""
    return [decode_message(message) for message in iter_messages(data)]


# Alias brevi mantenuti per facilitare l'uso interattivo del lettore.
messages = iter_messages
sections = iter_sections

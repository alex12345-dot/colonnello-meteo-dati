import struct

import numpy as np

from pipeline.grib2 import decode_message, iter_messages, iter_sections, sign_magnitude


def _section(number, size):
    data = bytearray(size)
    struct.pack_into(">I", data, 0, size)
    data[4] = number
    return data


def _simple_packing_bitmap_message():
    section3 = _section(3, 72)
    struct.pack_into(">I", section3, 6, 4)
    struct.pack_into(">H", section3, 12, 0)
    struct.pack_into(">II", section3, 30, 2, 2)
    struct.pack_into(">II", section3, 38, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into(">I", section3, 46, 1_000_000)
    struct.pack_into(">I", section3, 50, 0)
    struct.pack_into(">I", section3, 55, 0)
    struct.pack_into(">I", section3, 59, 1_000_000)
    struct.pack_into(">II", section3, 63, 1_000_000, 1_000_000)

    section4 = _section(4, 34)
    struct.pack_into(">H", section4, 7, 0)
    section4[9:11] = bytes((0, 0))

    section5 = _section(5, 21)
    struct.pack_into(">I", section5, 5, 3)
    struct.pack_into(">H", section5, 9, 0)
    struct.pack_into(">f", section5, 11, 10.0)
    section5[15:17] = b"\x80\x01"
    section5[17:19] = b"\x00\x00"
    section5[19:21] = bytes((4, 0))

    section6 = _section(6, 7)
    section6[5:7] = bytes((0, 0b10110000))
    section7 = _section(7, 7)
    section7[5:7] = bytes((0x12, 0x30))

    sections = bytes(section3 + section4 + section5 + section6 + section7)
    total = 16 + len(sections) + 4
    section0 = b"GRIB\x00\x00\x00\x02" + struct.pack(">Q", total)
    return section0 + sections + b"7777"


def test_temperature_global_statistics(fields):
    temperature = fields["2t"].values
    assert 275.0 < float(temperature.mean()) < 290.0
    assert not np.isnan(temperature).any()


def test_binary_scale_is_sign_magnitude(fixture_bytes):
    wind_u = list(iter_messages(fixture_bytes))[1]
    section5 = dict(iter_sections(wind_u))[5]
    raw = bytes(section5[15:17])
    assert sign_magnitude(raw) == -6
    assert struct.unpack(">h", raw)[0] == -32762


def test_rome_georeferencing_and_temperature(fields):
    field = fields["2t"]
    j, i = field.grid.cell(41.9, 12.5)
    assert (j, i) == (round((90.0 - 41.9) / 0.25), 770)
    celsius = field.values[j, i] - 273.15
    assert 27.0 <= celsius <= 29.0


def test_fixture_grid_definition(fields):
    grid = fields["2t"].grid
    assert (grid.ni, grid.nj) == (1440, 721)
    assert (grid.lat1, grid.lon1, grid.di, grid.dj, grid.scanning_mode) == (
        90.0,
        180.0,
        0.25,
        0.25,
        0,
    )


def test_simple_packing_5_0_and_bitmap():
    field = decode_message(_simple_packing_bitmap_message())
    assert field.data_template == 0
    assert field.binary_scale_factor == -1
    np.testing.assert_allclose(
        field.values,
        np.array([[10.5, np.nan], [11.0, 11.5]]),
        equal_nan=True,
    )

"""Contratto dei livelli Windy verificato su GRIB2 sintetici 2x2, senza rete."""

from pathlib import Path
import json

import numpy as np
import pytest

from pipeline.build import build, cloud_fraction, relative_humidity
from pipeline.encode import dequantize, encode_scalar, encode_wind, read_png
from pipeline.grib2 import decode_message
from pipeline.source_ecmwf import ECMWFSource, ParameterNotAvailable, forecast_url


DATA = Path(__file__).parent / "data"
LAYERS = "wind10m,t2m,precip,gust,msl,tcc,rh2m,cape,snow,swh"


class SyntheticSource:
    def __init__(self, *, missing_gust=False, cloud="tcc", wrong_wave_grid=False):
        self.calls = []
        self.missing_gust = missing_gust
        self.cloud = cloud
        self.wrong_wave_grid = wrong_wave_grid

    def download_parameters(self, model, run, step, parameters, *, optional=(), stream="oper"):
        self.calls.append((step, stream, tuple(parameters)))
        assert (stream == "wave") == (list(parameters) == ["swh"])
        payloads, absent = {}, []
        for name in parameters:
            if name == "10fg" and self.missing_gust and step == 0:
                assert name in optional
                absent.append(name)
                continue
            payload = (DATA / f'{self.cloud if name == "tcc" else name}.grib2').read_bytes()
            if name == "swh" and self.wrong_wave_grid:
                # Origine longitudine della sezione 3, spostata di un grado.
                import struct
                payload = bytearray(payload)
                struct.pack_into(">I", payload, 16 + 21 + 50, 1_000_000)
            payloads[name] = payload
        return payloads, absent


@pytest.mark.parametrize("layer,parameter,code,units,minimum,maximum", [
    ("gust", "10fg", (0, 2, 22), "m/s", 0, 30),
    ("msl", "msl", (0, 3, 0), "Pa", 80000, 82400),
    ("tcc", "tcc", (0, 6, 192), "0-1", 0, 1),
    ("rh2m", "2d", (0, 0, 6), "%", 26.181938976811338, 100),
    ("cape", "mucape", (0, 7, 6), "J/kg", 0, 2000),
    ("snow", "sf", (0, 1, 198), "m", 0, 0.01),
    ("swh", "swh", (10, 0, 3), "m", 0, 4),
])
def test_new_layer_parameter_units_and_encoding(tmp_path, layer, parameter, code, units, minimum, maximum):
    field = decode_message((DATA / f"{parameter}.grib2").read_bytes())
    assert (field.discipline, field.category, field.number) == code
    assert field.parameter == ("gust" if parameter == "10fg" else parameter)
    assert field.units == ("K" if layer == "rh2m" else units)
    manifest = build("ifs", "2026102606", "3", layer, tmp_path, source=SyntheticSource())
    assert manifest["layers"] == [layer]
    entry = manifest["steps"][0]["layers"][layer]
    assert entry["units"] == units
    assert entry["encoding"] == ("rgba8" if layer == "swh" else "grayscale8")
    assert entry["min"] == pytest.approx(minimum, abs=1e-9)
    assert entry["max"] == pytest.approx(maximum)
    assert entry["quantization_step"] == pytest.approx((maximum - entry["min"]) / 255)
    pixels = read_png(tmp_path / entry["url"])
    assert pixels.shape == ((2, 2, 4) if layer == "swh" else (2, 2))
    if layer == "swh":
        assert entry["channels"] == {"value": "R", "mask": "A"}
        np.testing.assert_array_equal(pixels[..., 3], [[255, 0], [255, 255]])
        np.testing.assert_array_equal(pixels[..., 1:3], 0)
        # Un mare calmo valido (0 m) non va scambiato per terra.
        assert pixels[0, 0, 0] == 0 and pixels[0, 0, 3] == 255
        decoded = dequantize(pixels[..., 0], entry["min"], entry["max"])
        valid = np.isfinite(field.values)
        assert np.max(np.abs(decoded[valid] - field.values[valid])) <= entry["quantization_step"] / 2
    if layer == "gust":
        assert entry["period_hours"] == 3


def test_magnus_manual_case_and_saturation_limits():
    # exp(17.625*10/253.04 - 17.625*20/263.04) * 100 = 52.5413 %.
    actual = relative_humidity(np.array([293.15, 293.15, 293.15, 293.15]),
                               np.array([283.15, 293.15, 303.15, np.nan]))
    np.testing.assert_allclose(actual[:3], [52.541325581, 100, 100], atol=1e-8)
    assert np.isnan(actual[3])
    with pytest.raises(ValueError, match="stessa griglia"):
        relative_humidity(np.zeros((2, 2)), np.zeros((2, 1)))


def test_wave_aec_padding_preserves_bitmap_and_values():
    # Il blocco AEC finale contiene padding oltre i tre valori rappresentati.
    field = decode_message((DATA / "swh-aec-padding.grib2").read_bytes())
    assert field.parameter == "swh" and field.units == "m"
    assert field.data_template == 42
    np.testing.assert_allclose(field.values, [[0, np.nan], [1, 4]], equal_nan=True)


@pytest.mark.parametrize("fixture,expected", [
    ("tcc", [[0, .25], [.5, 1]]),
    ("tcc-percent", [[0, .25], [.5, 1]]),
    ("tcc-low-percent", [[0, 0], [0, .01]]),
])
def test_tcc_normalizes_using_grib_units_even_below_one_percent(tmp_path, fixture, expected):
    field = decode_message((DATA / f"{fixture}.grib2").read_bytes())
    np.testing.assert_allclose(cloud_fraction(field), expected)
    manifest = build("ifs", "2026102606", "3", "tcc", tmp_path,
                     source=SyntheticSource(cloud=fixture))
    entry = manifest["steps"][0]["layers"]["tcc"]
    assert entry["units"] == "0-1" and entry["encoding"] == "grayscale8"
    assert entry["max"] == float(np.max(expected))


def test_all_layers_order_units_shared_temperature_and_optional_gust(tmp_path):
    source = SyntheticSource(missing_gust=True)
    manifest = build("ifs", "2026102606", "0-6", LAYERS, tmp_path, source=source)
    assert manifest["layers"] == LAYERS.split(",")
    assert manifest["warnings"] == [{"step": 0, "missing_parameters": ["10fg"]}]
    assert manifest["grid"]["width"] == 2 and manifest["grid"]["height"] == 2
    for step in manifest["steps"]:
        expected = LAYERS.split(",")
        if step["step"] == 0:
            expected.remove("gust")
        assert list(step["layers"]) == expected
        assert all(entry["units"] for entry in step["layers"].values())
        for entry in step["layers"].values():
            assert (tmp_path / entry["url"]).is_file()
    assert [(step, stream) for step, stream, _ in source.calls] == [
        (0, "oper"), (0, "wave"), (3, "oper"), (3, "wave"), (6, "oper"), (6, "wave")]
    assert all(params.count("2t") == 1 for _, stream, params in source.calls if stream == "oper")
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8")) == manifest


def test_existing_png_formats_are_byte_identical_except_manifest_units(tmp_path):
    manifest = build("ifs", "2026102606", "3", "wind10m,t2m,precip", tmp_path,
                     source=SyntheticSource())
    fields = {p: decode_message((DATA / f"{p}.grib2").read_bytes()).values
              for p in ("10u", "10v", "2t", "tp")}
    for layer, parameter, units in [("t2m", "2t", "K"), ("precip", "tp", "m")]:
        path = tmp_path / f"reference-{layer}.png"
        metadata = encode_scalar(fields[parameter], path)
        entry = manifest["steps"][0]["layers"][layer]
        assert entry == {"url": f"{layer}/003.png", **metadata, "units": units}
        assert path.read_bytes() == (tmp_path / entry["url"]).read_bytes()
    path = tmp_path / "reference-wind.png"
    metadata = encode_wind(fields["10u"], fields["10v"], path)
    entry = manifest["steps"][0]["layers"]["wind10m"]
    assert entry == {"url": "wind10m/003.png", **metadata, "units": "m/s"}
    assert path.read_bytes() == (tmp_path / entry["url"]).read_bytes()


def test_wave_grid_mismatch_fails_before_writing_png(tmp_path):
    with pytest.raises(ValueError, match="griglia swh diversa"):
        build("ifs", "2026102606", "3", "t2m,swh", tmp_path,
              source=SyntheticSource(wrong_wave_grid=True))
    assert not list(tmp_path.rglob("*.png"))


def test_only_gust_at_step_zero_can_be_missing(tmp_path):
    manifest = build("ifs", "2026102606", "0", "gust", tmp_path,
                     source=SyntheticSource(missing_gust=True))
    assert manifest["steps"][0]["layers"] == {}
    assert manifest["warnings"] == [{"step": 0, "missing_parameters": ["10fg"]}]
    assert not list(tmp_path.rglob("*.png"))


def test_wave_requires_ifs(tmp_path):
    with pytest.raises(ValueError, match="swh richiede"):
        build("aifs", "2026102606", "6", "swh", tmp_path, source=SyntheticSource())


def test_wave_index_cache_and_byte_range_use_wave_stream():
    payload = (DATA / "swh.grib2").read_bytes()
    calls = []

    class Response:
        def __init__(self, body, status):
            self.body, self.status = body, status
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return self.body

    def opener(request, timeout):
        calls.append((request.full_url, request.get_header("Range")))
        if request.full_url.endswith(".index"):
            param = "swh" if "/wave/" in request.full_url else "2t"
            return Response(json.dumps({"param": param, "levtype": "sfc", "_offset": 17,
                                        "_length": len(payload)}).encode(), 200)
        return Response(payload, 206)

    source = ECMWFSource(opener=opener)
    source.get_index("ifs", "2026102606", 3)
    actual, absent = source.download_parameters("ifs", "2026102606", 3, ["swh"], stream="wave")
    assert actual == {"swh": payload} and absent == []
    assert source.get_index("ifs", "2026102606", 3, stream="wave")[0]["param"] == "swh"
    assert len(calls) == 3  # le due cache di indice non si contaminano
    assert calls[-1] == (forecast_url("ifs", "2026102606", 3, "grib2", stream="wave"),
                         f"bytes=17-{16 + len(payload)}")
    assert calls[-1][0].endswith("/wave/20261026060000-3h-wave-fc.grib2")
    with pytest.raises(ParameterNotAvailable):
        source.download_parameters("ifs", "2026102606", 3, ["mucape"])


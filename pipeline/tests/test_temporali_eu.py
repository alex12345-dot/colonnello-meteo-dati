import bz2
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import struct
from urllib.error import HTTPError, URLError
import zlib

import numpy as np
import pytest

from pipeline.build import build
from pipeline.grib2 import Grid
from pipeline.source_dwd import DWDSource, DWDError, forecast_url, north_south
from pipeline.temporali_eu import build_temporali_eu, classify_weather
from pipeline.tests.test_build import OfflineSource
from pipeline.tests.test_source_dwd import response, synthetic_grib, synthetic_source


RUN = datetime(2026, 11, 20, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures/icon-eu-2026112000-012-ww-crop.grib2.bz2"


@pytest.fixture
def real_weather_field():
    def opener(request, timeout):
        assert request.full_url == forecast_url(RUN, 12, "ww")
        assert request.method == "GET"
        return response(FIXTURE.read_bytes())

    return DWDSource(opener=opener).download_weather(RUN, 12)


def test_decodes_real_dwd_ccsds_crop(real_weather_field):
    field = real_weather_field
    assert (field.parameter, field.data_template) == ("ww", 42)
    assert (field.grid.lat1, field.grid.lon1, field.grid.lat2, field.grid.lon2) == (41.5, 12.75, 41.8125, 13.1875)
    assert field.grid.shape == (6, 8)
    assert field.at(41.625, 12.9375) == 95
    assert field.at(41.625, 12.875) == 80
    assert field.at(41.5, 12.75) == 1
    values, _ = north_south(field)
    classes, unknown = classify_weather(values)
    expected = np.zeros((6, 8), dtype=np.uint8)
    expected[3, 3] = 1
    np.testing.assert_array_equal(classes, expected)
    assert unknown == {}


def test_download_weather_rejects_a_different_grib_parameter():
    source = DWDSource(opener=lambda request, timeout: response(bz2.compress(synthetic_grib())))
    with pytest.raises(DWDError, match="diverso da ww"):
        source.download_weather(RUN, 12)


def test_maps_wmo_codes_preserving_nodata_and_unknowns():
    values = np.array([[0, 3, 67, 80, 95, 96, 97, 98, 99, 42, 42, 96.5, 255, np.nan]])
    classes, unknown = classify_weather(values)
    assert classes.tolist() == [[0, 0, 0, 0, 1, 2, 1, 1, 255, 255, 255, 255, 255, 255]]
    assert unknown == {"42": 2, "96.5": 1, "99": 1, "255": 1}


def png_pixels(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, color, compression, filtering, interlace = struct.unpack_from(">IIBBBBB", data, 16)
    assert (depth, color, compression, filtering, interlace) == (8, 0, 0, 0, 0)
    compressed = bytearray()
    offset = 8
    while offset < len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        if kind == b"IDAT":
            compressed.extend(data[offset + 8:offset + 8 + size])
        offset += size + 12
    rows = np.frombuffer(zlib.decompress(compressed), dtype=np.uint8).reshape(height, width + 1)
    assert not rows[:, 0].any()
    return rows[:, 1:]


@pytest.fixture
def simulated_dwd(real_weather_field):
    # Ritaglio piu' una corona di un pixel per collaudare anche scarto e scansione.
    grid = Grid(435, 275, 34.9375, 353.9375, 52.0625, 21.0625, 0.0625, 0.0625, 64)
    values = np.full(grid.shape, 99.0)
    values[1:-1, 1:-1] = 0
    values[-2, 2] = np.nan
    values[1, -2] = 42
    rain_source, _ = synthetic_source()
    total = rain_source.download_total(RUN, 1)

    class Source(DWDSource):
        probes = []
        downloaded = []

        def latest_run(self):
            self.probes.append("tot_prec")
            return RUN

        def latest_weather_run(self):
            self.probes.append("ww")
            return RUN

        def download_weather(self, run, step):
            self.downloaded.append(step)
            assert run == RUN
            values[-2, 1] = (95, 96, 97, 99)[(step - 1) % 4]
            return replace(real_weather_field, grid=grid, values=values)

        def download_total(self, run, step):
            assert run == RUN
            return total

    return Source()


def test_single_png_order_bounds_counts_and_manifest(tmp_path, simulated_dwd, capsys):
    entry = build_temporali_eu(tmp_path, simulated_dwd)
    assert entry["model"] == "icon-eu" and entry["run"] == "2026112000"
    generated = datetime.fromisoformat(entry["generated_at"])
    assert generated.tzinfo == timezone.utc
    assert entry["attribution"] == "Deutscher Wetterdienst (DWD), ICON-EU, CC BY 4.0"
    assert entry["classification"] == "WMO-4677" and entry["hail_diagnostic"] is False
    assert entry["class_labels"] == {"0": "nessun temporale", "1": "temporale", "2": "temporale forte"}
    assert entry["bbox"] == {"west": -6.0, "south": 35.0, "east": 21.0, "north": 52.0}
    assert entry["grid"] == {"lat0": 52.0, "lon0": -6.0, "scanning_mode": 0, "width": 433,
                             "height": 273, "dlat": 0.0625, "dlon": 0.0625}
    assert entry["steps"] == [
        {"step": k, "valid_time": (RUN + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        for k in range(1, 49)
    ]
    assert simulated_dwd.downloaded == list(range(1, 49))
    assert entry["url"] == "temporali_eu/2026112000.png"
    path = tmp_path / entry["url"]
    assert list(path.parent.iterdir()) == [path]
    assert path.stat().st_size < 300_000
    assert entry["encoding"] == "grayscale8" and entry["tile_columns"] == 6 and entry["nodata"] == 255
    pixels = png_pixels(path)
    assert pixels.shape == (8 * 273, 6 * 433)
    assert [(int(pixels[(i // 6) * 273, (i % 6) * 433])) for i in range(48)] == [1, 2, 1, 255] * 12
    assert pixels[0, 1] == 255 and pixels[-1, -1] == 255
    counts = {str(code): int((pixels == code).sum()) for code in (0, 1, 2, 255)}
    assert not (pixels == 3).any()
    assert entry["class_counts"] == counts == {"0": 48 * (273 * 433 - 3), "1": 24, "2": 12, "255": 108}
    assert entry["unknown_ww_counts"] == {"42": 48, "99": 12}
    assert "codici ww sconosciuti: {'42': 48, '99': 12}" in capsys.readouterr().err


def test_build_keeps_published_weather_with_its_file_during_dwd_failure(tmp_path, simulated_dwd, capsys):
    old, out = tmp_path / "published", tmp_path / "out"
    first = build("ifs", "2026111900", "12", "t2m", old,
                  source=OfflineSource(), dwd_source=simulated_dwd, temporali_eu=True)
    assert first["precip_eu"]["run"] == first["temporali_eu"]["run"] == "2026112000"
    assert simulated_dwd.probes == ["tot_prec", "ww"]
    relative = first["temporali_eu"]["url"]
    out.mkdir()
    (out / "manifest.json").write_bytes((old / "manifest.json").read_bytes())

    def unavailable(request, timeout):
        raise URLError("DWD irraggiungibile")

    down = DWDSource(attempts=1, opener=unavailable)
    manifest = build("ifs", "2026111912", "12", "t2m", out, source=OfflineSource(),
                     dwd_source=down, temporali_eu=True, previous_output=old)
    assert manifest["run"] == "2026111912"
    assert (out / manifest["steps"][0]["layers"]["t2m"]["url"]).is_file()
    assert manifest["temporali_eu"] == first["temporali_eu"]
    assert (out / relative).read_bytes() == (old / relative).read_bytes()
    assert any(item["source"] == "ICON-EU temporali_eu" and "2026112000" in item["message"]
               for item in manifest["warnings"])
    assert "Conservata la corsa 2026112000" in capsys.readouterr().err
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == manifest


def test_missing_ww_step_retains_previous_file_and_rain(tmp_path, simulated_dwd):
    # Il precedente byte per byte resta leggibile dopo un errore nel mezzo della nuova corsa.
    first = build("ifs", "2026111900", "12", "t2m", tmp_path, source=OfflineSource(),
                  dwd_source=simulated_dwd, temporali_eu=True)
    old = first["temporali_eu"]
    content = (tmp_path / old["url"]).read_bytes()
    new_run = RUN + timedelta(hours=6)
    get_weather, get_total = simulated_dwd.download_weather, simulated_dwd.download_total
    simulated_dwd.latest_run = lambda **kw: new_run
    simulated_dwd.latest_weather_run = lambda: new_run
    simulated_dwd.download_total = lambda run, step: get_total(RUN, step)

    def missing(run, step):
        assert run == new_run
        if step == 23:
            raise DWDError("passo 23 assente")
        return get_weather(RUN, step)

    simulated_dwd.download_weather = missing
    second = build("ifs", "2026111912", "12", "t2m", tmp_path, source=OfflineSource(),
                   dwd_source=simulated_dwd, temporali_eu=True)
    assert second["precip_eu"]["run"] == "2026112006"
    assert second["temporali_eu"] == old
    assert (tmp_path / old["url"]).read_bytes() == content
    assert not (tmp_path / "temporali_eu/2026112006.png").exists()


@pytest.mark.parametrize("missing_variable", ["ww", "tot_prec"])
def test_build_selects_icon_eu_cycles_independently(tmp_path, simulated_dwd, missing_variable):
    new_run = RUN + timedelta(hours=6)
    missing_url = forecast_url(new_run, {"ww": 48, "tot_prec": 72}[missing_variable], missing_variable)
    probes = []

    def opener(request, timeout):
        assert request.method == "HEAD"
        probes.append(request.full_url)
        if request.full_url == missing_url:
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return response()

    class Cycles(DWDSource):
        def latest_run(self, **options):
            return super().latest_run(now=new_run, **options)

        def latest_weather_run(self):
            return super().latest_weather_run(now=new_run)

        def download_total(self, run, step):
            assert run == (RUN if missing_variable == "tot_prec" else new_run)
            return simulated_dwd.download_total(RUN, step)

        def download_weather(self, run, step):
            assert run == (RUN if missing_variable == "ww" else new_run)
            return simulated_dwd.download_weather(RUN, step)

    manifest = build("ifs", "2026111900", "12", "t2m", tmp_path, source=OfflineSource(),
                     dwd_source=Cycles(opener=opener), temporali_eu=True)
    assert manifest["precip_eu"]["run"] == ("2026112000" if missing_variable == "tot_prec" else "2026112006")
    assert manifest["temporali_eu"]["run"] == ("2026112000" if missing_variable == "ww" else "2026112006")
    assert len(probes) == 3 and missing_url in probes


def test_build_marks_reused_weather_stale_from_run_time(tmp_path, simulated_dwd, monkeypatch, capsys):
    from pipeline import build as build_module

    class Clock(datetime):
        instant = RUN + timedelta(hours=11, minutes=59)

        @classmethod
        def now(cls, tz):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(build_module, "datetime", Clock)
    first = build("ifs", "2026111900", "12", "t2m", tmp_path, source=OfflineSource(),
                  dwd_source=simulated_dwd, temporali_eu=True)
    entry = first["temporali_eu"]
    assert entry["stale"] is False
    png = (tmp_path / entry["url"]).read_bytes()
    capsys.readouterr()

    Clock.instant = RUN + timedelta(hours=12)
    second = build("ifs", "2026111900", "12", "t2m", tmp_path, source=OfflineSource(),
                   dwd_source=simulated_dwd, temporali_eu=True)
    assert second["temporali_eu"] == {**entry, "stale": True}
    assert (tmp_path / entry["url"]).read_bytes() == png
    assert simulated_dwd.downloaded == list(range(1, 49))
    message = "ICON-EU temporali_eu corsa=2026112000 dato vecchio: 12.0 h (soglia 12 h)"
    assert {"source": "ICON-EU temporali_eu", "message": message} in second["warnings"]
    assert f"::warning::{message}" in capsys.readouterr().err

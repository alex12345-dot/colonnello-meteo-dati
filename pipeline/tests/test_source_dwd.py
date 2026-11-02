"""Fixture GRIB 3x2 sintetiche; nessun download o ritaglio del file reale."""

import bz2
from datetime import datetime, timezone
from http.client import IncompleteRead
from io import BytesIO
import struct
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import numpy as np
import pytest

from pipeline.encode import read_png
from pipeline.source_dwd import (
    DWDError, DWDSource, RunNotAvailable, build_precip_eu, forecast_url, north_south,
)


RUN = datetime(2026, 11, 2, tzinfo=timezone.utc)


def synthetic_grib(values=(1, 2, 3, 4, 5, 6), *, scanning=64, number=8):
    def section(number, length):
        result = bytearray(length)
        struct.pack_into(">I", result, 0, length)
        result[4] = number
        return result

    s3 = section(3, 72)
    struct.pack_into(">I", s3, 6, 6)
    struct.pack_into(">II", s3, 30, 3, 2)
    struct.pack_into(">II", s3, 38, 0xFFFFFFFF, 0xFFFFFFFF)
    for offset, value in ((46, 29_500_000), (50, 336_500_000),
                          (55, 29_562_500), (59, 336_625_000),
                          (63, 62_500), (67, 62_500)):
        struct.pack_into(">I", s3, offset, value)
    s3[71] = scanning
    s4 = section(4, 34)
    s4[9:11] = bytes((1, number))
    s5 = section(5, 21)
    struct.pack_into(">I", s5, 5, 6)
    s5[19] = 8
    s7 = section(7, 11)
    s7[5:] = bytes(values)
    sections = bytes(s3 + s4 + s5 + s7)
    return b"GRIB\x00\x00\x00\x02" + struct.pack(">Q", 20 + len(sections)) + sections + b"7777"


def response(payload=b"", status=200):
    result = BytesIO(payload)
    result.status = status
    return result


def http_error(request, code, headers=None):
    return HTTPError(request.full_url, code, "simulato", headers or {}, None)


def synthetic_source(*, missing=None, payload=None, **options):
    calls = []
    compressed = bz2.compress(payload if payload is not None else synthetic_grib())

    def opener(request, timeout):
        calls.append((request.full_url, request.method, timeout))
        step = int(request.full_url.split("_")[-3])
        if request.method == "HEAD":
            return response()
        if step == missing:
            raise http_error(request, 404)
        return response(compressed)

    source = DWDSource(opener=opener, **options)
    source.latest_run = lambda: RUN
    return source, calls


@pytest.mark.parametrize("run,step", [("2026110200", 0), ("2026110206", 6),
                                      ("2026110212", 72), ("2026110118", 1)])
def test_forecast_url(run, step):
    assert forecast_url(run, step) == (
        f"https://opendata.dwd.de/weather/nwp/icon-eu/grib/{run[-2:]}/tot_prec/"
        f"icon-eu_europe_regular-lat-lon_single-level_{run}_{step:03d}_TOT_PREC.grib2.bz2"
    )


def test_latest_run_probes_072_newest_first_and_handles_day_boundary():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if "2026110118" not in request.full_url:
            raise http_error(request, 404)
        return response()

    source = DWDSource(opener=opener)
    assert source.latest_run(now=RUN.replace(hour=13)) == RUN.replace(day=1, hour=18)
    assert [r.full_url for r in calls] == [forecast_url(run, 72) for run in
        ("2026110212", "2026110206", "2026110200", "2026110118")]
    assert all(r.method == "HEAD" for r in calls)


def test_latest_run_stops_after_four_previous_cycles():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        raise http_error(request, 404)

    with pytest.raises(RunNotAvailable):
        DWDSource(opener=opener).latest_run(now=RUN)
    assert len(calls) == 5
    assert calls[-1].full_url == forecast_url("2026110100", 72)


def test_head_fallback_reads_only_one_byte_even_if_range_ignored():
    calls = []

    class Probe(BytesIO):
        def read(self, size=-1):
            assert size == 1
            return super().read(size)

    def opener(request, timeout):
        calls.append(request)
        if request.method == "HEAD":
            raise http_error(request, 405)
        assert request.get_header("Range") == "bytes=0-0"
        return Probe(b"not downloaded in full")

    assert DWDSource(opener=opener).latest_run(now=RUN) == RUN
    assert len(calls) == 2


def test_download_decompresses_and_reorders_synthetic_grib():
    source, _ = synthetic_source()
    values, grid = north_south(source.download_total(RUN, 1))
    np.testing.assert_array_equal(values, [[4, 5, 6], [1, 2, 3]])
    assert values[0, 0] == 4  # Cella nord-ovest.
    assert grid == {"width": 3, "height": 2, "lat0": 29.5625, "lon0": -23.5,
                    "dlat": 0.0625, "dlon": 0.0625, "scanning_mode": 0}


def test_complete_manifest_has_exact_contract_keys_and_times(tmp_path):
    source, calls = synthetic_source()
    entry = build_precip_eu(tmp_path, source)
    assert set(entry) == {"model", "run", "attribution", "license", "grid", "encoding",
                          "transform", "units", "interval_hours", "steps"}
    assert entry["model"] == "icon-eu" and entry["run"] == "2026110200"
    assert entry["attribution"] == "Deutscher Wetterdienst (DWD), ICON-EU"
    assert entry["license"] == "CC-BY-4.0"
    assert (entry["encoding"], entry["transform"], entry["units"], entry["interval_hours"]) == (
        "grayscale8", "sqrt", "mm/h", 1)
    assert len(entry["steps"]) == 72
    for k, step in enumerate(entry["steps"], 1):
        assert set(step) == {"valid_time", "url", "min", "max", "quantization_step"}
        from datetime import timedelta
        assert step["valid_time"] == (RUN + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert step["url"] == f"precip_eu/{k:03d}.png"
        assert step["min"] == 0
        assert step["quantization_step"] == step["max"] / 255
        pixels = read_png(tmp_path / step["url"])
        assert pixels.shape == (2, 3)
        if k == 1:
            np.testing.assert_allclose((pixels * step["quantization_step"]) ** 2,
                                       [[4, 5, 6], [1, 2, 3]], atol=0.03)
        else:
            assert not pixels.any()  # Cumulata invariata = nessuna pioggia nuova.
    assert len(calls) == 72
    assert not (tmp_path / "precip_eu/000.png").exists()


def test_negative_differences_clipped_without_metre_conversion(tmp_path):
    payloads = {1: synthetic_grib((2, 4, 6, 8, 10, 12)),
                2: synthetic_grib((1, 3, 5, 7, 9, 16))}
    source, _ = synthetic_source()
    original = source.download_total

    def download(run, step):
        if step in payloads:
            other, _ = synthetic_source(payload=payloads[step])
            return other.download_total(run, step)
        return original(run, step)

    source.download_total = download
    entry = build_precip_eu(tmp_path, source)
    second = entry["steps"][1]
    assert second["max"] == 2  # sqrt(4 mm/h), nessun x1000.
    np.testing.assert_array_equal(read_png(tmp_path / second["url"]), [[0, 0, 255], [0, 0, 0]])


@pytest.mark.parametrize("payload", [b"corrotto", synthetic_grib(number=193),
                                     synthetic_grib() + synthetic_grib()])
def test_corrupt_or_wrong_parameter_rejected(payload):
    source, _ = synthetic_source(payload=payload)
    with pytest.raises((DWDError, ValueError)):
        source.download_total(RUN, 1)


def test_real_dwd_parameter_code_52_accepted():
    # Il file vero del DWD codifica TOT_PREC come 0.1.52 (corsa 2026110200): deve passare.
    source, _ = synthetic_source(payload=synthetic_grib(number=52))
    field = source.download_total(RUN, 1)
    assert field.number == 52


def test_nonfinite_domain_and_unexpected_scanning_rejected():
    source, _ = synthetic_source()
    field = source.download_total(RUN, 1)
    field.values[0, 0] = np.nan
    with pytest.raises(DWDError, match="incompleto"):
        north_south(field)
    source, _ = synthetic_source(payload=synthetic_grib(scanning=0))
    with pytest.raises(DWDError, match="scanning"):
        north_south(source.download_total(RUN, 1))


@pytest.mark.parametrize("error", ["503", "url", "timeout", "incomplete"])
def test_transient_errors_retry_with_backoff_and_timeout(error):
    waits, timeouts = [], []

    def opener(request, timeout):
        timeouts.append(timeout)
        if len(timeouts) < 3:
            if error == "503":
                raise http_error(request, 503)
            if error == "url":
                raise URLError("offline")
            if error == "timeout":
                raise TimeoutError()
            raise IncompleteRead(b"half")
        return response(b"ok")

    source = DWDSource(opener=opener, sleeper=waits.append, timeout=7, backoff=0.5)
    assert source._request(forecast_url(RUN, 1)) == b"ok"
    assert waits == [0.5, 1.0]
    assert timeouts == [7, 7, 7]


def test_permanent_http_error_not_retried_or_treated_as_missing_run():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        raise http_error(request, 403)

    with pytest.raises(DWDError, match="403"):
        DWDSource(opener=opener).latest_run(now=RUN)
    assert len(calls) == 1


@pytest.mark.parametrize("total,request_budget,expected", [(2, 20, 2), (20, 2, 2)])
def test_budgets_bound_requests_and_retry_after(total, request_budget, expected):
    now = [0.0]
    waits, timeouts = [], []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    def opener(request, timeout):
        timeouts.append(timeout)
        raise http_error(request, 503, {"Retry-After": "999999"})

    source = DWDSource(opener=opener, sleeper=sleep, clock=lambda: now[0],
                       total_budget=total, max_elapsed=request_budget)
    with pytest.raises(DWDError):
        source._request(forecast_url(RUN, 1))
    assert waits == [expected]
    assert timeouts == [expected]


def test_total_budget_covers_all_downloads_and_encoding(tmp_path):
    now = [0.0]
    source, calls = synthetic_source(clock=lambda: now[0], total_budget=1)
    original = source.download_total

    def download(run, step):
        field = original(run, step)
        now[0] += 0.6
        return field

    source.download_total = download
    with pytest.raises(DWDError, match="budget totale"):
        build_precip_eu(tmp_path, source)
    assert len(calls) == 2

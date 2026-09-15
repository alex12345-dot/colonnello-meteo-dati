from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from pipeline.source_ecmwf import (
    ECMWFSource,
    NetworkError,
    RunNotAvailable,
    find_parameters,
    forecast_url,
    parse_index,
    range_header,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_index_parsing_and_range_headers():
    entries = parse_index((FIXTURES / "ifs-12h.index").read_text(encoding="utf-8"))
    selected, absent = find_parameters(
        entries,
        ["10u", "10v", "2t", "tp", "msl", "10fg"],
        optional=["10fg"],
    )
    assert absent == []
    assert range_header(selected["10fg"]) == {"Range": "bytes=72929041-74354598"}
    assert range_header(selected["10u"]) == {"Range": "bytes=9964219-10841526"}
    assert range_header(selected["2t"]) == {"Range": "bytes=20612669-21273805"}


def test_forecast_url_matches_public_bucket_layout():
    assert forecast_url("ifs", "2026091300", 12, "grib2").endswith(
        "/20260913/00z/ifs/0p25/oper/20260913000000-12h-oper-fc.grib2"
    )


def test_404_is_run_not_available_without_becoming_network_error():
    def missing(request, timeout):
        raise HTTPError(request.full_url, 404, "missing", {}, None)

    source = ECMWFSource(opener=missing, sleeper=lambda _: None)
    with pytest.raises(RunNotAvailable):
        source.get_index("ifs", "2026091300", 12)


def test_network_error_retries_and_stays_distinct():
    calls = []

    def offline(request, timeout):
        calls.append(request.full_url)
        raise URLError("offline")

    source = ECMWFSource(attempts=3, opener=offline, sleeper=lambda _: None)
    with pytest.raises(NetworkError) as raised:
        source.get_index("ifs", "2026091300", 12)
    assert not isinstance(raised.value, RunNotAvailable)
    assert len(calls) == 3


def test_503_slow_down_backs_off_for_minutes_not_seconds():
    # Il bucket ECMWF risponde "503 Slow Down" a raffica: con 3 tentativi da 0,5 s il giro
    # moriva (15/09/2026). Di default si insiste piu' a lungo e con attese crescenti.
    waits = []
    calls = []

    def throttled(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 503, "Slow Down", {}, None)

    source = ECMWFSource(opener=throttled, sleeper=waits.append)
    with pytest.raises(NetworkError):
        source.get_index("ifs", "2026091300", 0)
    assert len(calls) >= 5
    assert sum(waits) >= 120
    assert waits == sorted(waits)


def test_503_honours_retry_after_and_then_succeeds():
    from email.message import Message

    waits = []
    state = {"n": 0}
    index_text = (FIXTURES / "ifs-12h.index").read_bytes()

    class Ok:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return index_text

    def flaky(request, timeout):
        state["n"] += 1
        if state["n"] == 1:
            headers = Message()
            headers["Retry-After"] = "30"
            raise HTTPError(request.full_url, 503, "Slow Down", headers, None)
        return Ok()

    source = ECMWFSource(opener=flaky, sleeper=waits.append)
    assert source.get_index("ifs", "2026091300", 12)
    assert waits == [30.0]
    assert state["n"] == 2

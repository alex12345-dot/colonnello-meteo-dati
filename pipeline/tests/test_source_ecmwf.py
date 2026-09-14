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


def test_network_error_retries_three_times_and_stays_distinct():
    calls = []

    def offline(request, timeout):
        calls.append(request.full_url)
        raise URLError("offline")

    source = ECMWFSource(opener=offline, sleeper=lambda _: None)
    with pytest.raises(NetworkError) as raised:
        source.get_index("ifs", "2026091300", 12)
    assert not isinstance(raised.value, RunNotAvailable)
    assert len(calls) == 3

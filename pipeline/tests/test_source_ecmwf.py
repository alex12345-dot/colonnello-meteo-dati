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
    assert len(calls) == 6
    assert waits == [5.0, 10.0, 20.0, 40.0, 60.0]  # cresce, ma mai oltre MAX_WAIT_SECONDS


def test_permanent_http_errors_are_not_retried():
    calls = []

    def forbidden(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    source = ECMWFSource(opener=forbidden, sleeper=lambda _: pytest.fail("non deve attendere"))
    with pytest.raises(NetworkError):
        source.get_index("ifs", "2026091300", 0)
    assert len(calls) == 1


def test_retry_after_is_clamped_and_accepts_http_date():
    from email.message import Message
    from datetime import datetime, timedelta, timezone
    from pipeline.source_ecmwf import MAX_WAIT_SECONDS, _retry_after_seconds

    def err(value):
        headers = Message()
        headers["Retry-After"] = value
        return HTTPError("u", 503, "Slow Down", headers, None)

    assert _retry_after_seconds(err("3600")) == MAX_WAIT_SECONDS
    assert _retry_after_seconds(err("inf")) is None
    assert _retry_after_seconds(err("-5")) == 0.0
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    later = (now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _retry_after_seconds(err(later), now=now) == 30.0
    assert _retry_after_seconds(err("boh")) is None


def test_incomplete_read_is_retried():
    from http.client import IncompleteRead

    state = {"n": 0}
    index_text = (FIXTURES / "ifs-12h.index").read_bytes()

    class Body:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            state["n"] += 1
            if state["n"] == 1:
                raise IncompleteRead(b"met")
            return index_text

    source = ECMWFSource(opener=lambda request, timeout: Body(), sleeper=lambda _: None)
    assert source.get_index("ifs", "2026091300", 12)
    assert state["n"] == 2


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


def _index_ok():
    index_text = (FIXTURES / "ifs-12h.index").read_bytes()

    class Ok:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return index_text

    return Ok()


def test_latest_run_stops_at_the_first_published_run():
    # 16/09/2026: la sonda chiedeva tutte le 12 candidate e un 503 sulla decima
    # buttava via la corsa buona gia' trovata. Ora: una 404, una 200, stop.
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return _index_ok()

    from datetime import datetime, timezone
    source = ECMWFSource(opener=opener, sleeper=lambda _: None)
    run = source.latest_run("ifs", now=datetime(2026, 9, 16, 18, 30, tzinfo=timezone.utc))
    assert run == datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    assert len(calls) == 2


def test_available_runs_keeps_newer_runs_when_an_older_one_is_throttled():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) <= 2:
            return _index_ok()
        raise HTTPError(request.full_url, 503, "Slow Down", {}, None)

    from datetime import datetime, timezone
    source = ECMWFSource(attempts=2, opener=opener, sleeper=lambda _: None)
    runs = source.available_runs("ifs", now=datetime(2026, 9, 16, 18, 30, tzinfo=timezone.utc))
    assert len(runs) == 2
    assert len(calls) == 4  # due riuscite + due tentativi della terza, poi stop

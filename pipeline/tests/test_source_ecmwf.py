from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from pipeline.source_ecmwf import (
    MAX_BACKOFF_SECONDS,
    ECMWFSource,
    NetworkError,
    RunNotAvailable,
    find_parameters,
    forecast_url,
    parse_index,
    range_header,
)


FIXTURES = Path(__file__).parent / "fixtures"
MIRROR_URLS = (
    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
    "https://data.ecmwf.int/forecasts",
)


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
    calls = []

    def missing(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 404, "missing", {}, None)

    source = ECMWFSource(opener=missing, sleeper=lambda _: None)
    with pytest.raises(RunNotAvailable):
        source.get_index("ifs", "2026091300", 12)
    path = "/20260913/00z/ifs/0p25/oper/20260913000000-12h-oper-fc.index"
    assert calls == [
        f"{host}{path}" for host in MIRROR_URLS
    ]


def test_404_on_one_mirror_can_succeed_on_the_other():
    calls = []

    def delayed_replication(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return _index_ok()

    source = ECMWFSource(opener=delayed_replication, sleeper=lambda _: None)
    assert source.get_index("ifs", "2026091300", 12)
    assert calls[0].startswith(MIRROR_URLS[0])
    assert calls[1].startswith(MIRROR_URLS[1])


def test_503_on_first_mirror_rotates_to_second_and_succeeds(capsys):
    calls = []
    waits = []

    def throttled_once(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 503, "Slow Down", {}, None)
        return _index_ok()

    source = ECMWFSource(opener=throttled_once, sleeper=waits.append)
    assert source.get_index("ifs", "2026091300", 12)
    assert calls[0].startswith(MIRROR_URLS[0])
    assert calls[1].startswith(MIRROR_URLS[1])
    assert waits == [0.5]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert MIRROR_URLS[0] in captured.err
    assert "stato=503" in captured.err
    assert "tentativo=1" in captured.err
    assert "attesa=0.5" in captured.err


def test_transient_failures_rotate_mirrors_on_every_attempt():
    calls = []

    def throttled(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 503, "Slow Down", {}, None)

    source = ECMWFSource(attempts=3, opener=throttled, sleeper=lambda _: None)
    with pytest.raises(NetworkError):
        source.get_index("ifs", "2026091300", 12)
    assert [url.startswith(MIRROR_URLS[0]) for url in calls] == [True, False, True]


def test_network_exception_is_reported_on_stderr(capsys):
    def offline(request, timeout):
        raise URLError("offline reale")

    source = ECMWFSource(attempts=1, opener=offline, sleeper=lambda _: None)
    with pytest.raises(NetworkError):
        source.get_index("ifs", "2026091300", 12)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "URLError" in captured.err
    assert "offline reale" in captured.err
    assert "tentativo=1" in captured.err


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


def test_503_slow_down_insiste_molte_volte_con_attese_brevi():
    # Il "503 Slow Down" del bucket ECMWF colpisce ~55 % delle richieste anche a una al
    # secondo (misura del 17/09/2026): e' una moneta, non una punizione al nostro ritmo.
    # Quindi conta il numero di tentativi, e le prime attese devono restare brevi.
    waits = []
    calls = []

    def throttled(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 503, "Slow Down", {}, None)

    source = ECMWFSource(opener=throttled, sleeper=waits.append)
    with pytest.raises(NetworkError):
        source.get_index("ifs", "2026091300", 0)
    # 0,55^16 = 0,007 % di perdere una richiesta: su ~110 richieste il giro regge.
    assert len(calls) == 16
    assert waits[:5] == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert sum(waits[:5]) <= 16.0  # i primi cinque tentativi entro un quarto di minuto
    assert max(waits) == MAX_BACKOFF_SECONDS  # nessuna attesa da un minuto senza Retry-After


def test_503_ripetuti_finiscono_comunque_in_un_successo():
    # Il difetto vero del 16-17/09/2026: dodici 503 di fila esaurivano i sei tentativi e
    # facevano morire il giro su una corsa che c'era. Ora la tredicesima risposta si prende.
    risposte = []

    class Risposta:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"param": "2t", "_offset": 0, "_length": 10}'

    def flaky(request, timeout):
        risposte.append(request.full_url)
        if len(risposte) <= 12:
            raise HTTPError(request.full_url, 503, "Slow Down", {}, None)
        return Risposta()

    source = ECMWFSource(opener=flaky, sleeper=lambda _: None)
    assert source.get_index("ifs", "2026091300", 0)[0]["param"] == "2t"
    assert len(risposte) == 13


def test_budget_temporale_interrompe_i_tentativi_lenti():
    # Una sequenza di timeout non deve moltiplicare 16 volte il timeout di rete e consumare
    # il job intero. Il secondo tentativo riceve soltanto il tempo ancora disponibile.
    elapsed = [0.0]
    timeouts = []

    def clock():
        return elapsed[0]

    def slow_timeout(request, timeout):
        timeouts.append(timeout)
        elapsed[0] += timeout
        raise TimeoutError("rete lenta")

    def sleep(seconds):
        elapsed[0] += seconds

    source = ECMWFSource(
        opener=slow_timeout,
        sleeper=sleep,
        clock=clock,
        timeout=30.0,
        max_elapsed=60.0,
    )
    with pytest.raises(NetworkError):
        source.get_index("ifs", "2026091300", 0)

    assert timeouts == [30.0, 29.5]
    assert elapsed[0] == 60.0


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
    # buttava via la corsa buona gia' trovata. Ora: entrambi i mirror confermano
    # la 404 della prima candidata, poi la seconda candidata risponde 200 e si ferma.
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) <= 2:
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)
        return _index_ok()

    from datetime import datetime, timezone
    source = ECMWFSource(opener=opener, sleeper=lambda _: None)
    run = source.latest_run("ifs", now=datetime(2026, 9, 16, 18, 30, tzinfo=timezone.utc))
    assert run == datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    assert len(calls) == 3


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

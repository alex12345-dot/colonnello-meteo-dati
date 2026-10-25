from http.client import IncompleteRead
import json
from urllib.error import HTTPError, URLError

import pytest

from pipeline.source_dpc import API_URL, DPCError, DPCSource, latest_complete_bulletin


class Response:
    status = 200

    def __init__(self, value):
        self.payload = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


def test_incomplete_latest_pair_falls_back():
    assert latest_complete_bulletin([
        "20261025_1600_today.json", "20261025_1326_today.json",
        "20261025_1326_tomorrow.json", "20261024_1700_today.json",
        "20261024_1700_tomorrow.json", "20261025_9999_other.json",
    ]) == "20261025_1326"
    with pytest.raises(DPCError, match="nessuna coppia"):
        latest_complete_bulletin(["20261025_1600_today.json", "20261025_1326_tomorrow.json"])


def test_tree_discovery_and_raw_download_at_fixed_commit(dpc_pair):
    calls = []
    listing = ["20261025_1600_today.json", "20261025_1326_today.json", "20261025_1326_tomorrow.json"]
    replies = [
        {"default_branch": "master"}, {"sha": "commit123"},
        {"tree": [{"path": "files", "type": "tree", "sha": "files123"}]},
        {"tree": [{"path": "geojson", "type": "tree", "sha": "geo123"}]},
        {"tree": [{"path": path, "type": "blob"} for path in listing]},
        *dpc_pair,
    ]

    def opener(request, timeout):
        calls.append((request.full_url, dict(request.header_items()), timeout))
        return Response(replies[len(calls) - 1])

    stamp, today, tomorrow = DPCSource(token="test-token", opener=opener).latest_bulletin()
    assert stamp == "20261025_1326" and (today, tomorrow) == dpc_pair
    assert [url for url, _, _ in calls[:5]] == [
        API_URL, API_URL + "/commits/master", API_URL + "/git/trees/commit123",
        API_URL + "/git/trees/files123", API_URL + "/git/trees/geo123",
    ]
    assert all("/commit123/files/geojson/20261025_1326_" in url for url, _, _ in calls[5:])
    assert all(headers["Authorization"] == "Bearer test-token" for _, headers, _ in calls[:5])
    assert all("Authorization" not in headers for _, headers, _ in calls[5:])
    assert all(timeout == 30 for _, _, timeout in calls)


@pytest.mark.parametrize("error", [503, 429, "timeout", "url", "read"])
def test_transient_errors_retry_with_backoff_and_observed_log(error, capsys):
    calls, waits = [], []

    def opener(request, timeout):
        calls.append(request)
        if len(calls) == 3:
            return Response({"ok": True})
        if error == "timeout":
            raise TimeoutError("timeout simulato")
        if error == "url":
            raise URLError("rete simulata")
        if error == "read":
            raise IncompleteRead(b"{")
        raise HTTPError(request.full_url, error, "errore simulato", {}, None)

    source = DPCSource(opener=opener, sleeper=waits.append)
    assert source._json(API_URL) == {"ok": True}
    assert waits == [1, 2]
    log = capsys.readouterr().err
    assert "host=api.github.com" in log
    assert (f"HTTP={error}" if isinstance(error, int) else "errore=") in log
    assert "HTTP=200" in log


@pytest.mark.parametrize("status,attempts", [(403, 1), (404, 1), (503, 3)])
def test_http_failure_is_explicit_and_bounded(status, attempts, capsys):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, status, "osservato", {}, None)

    with pytest.raises(DPCError, match=f"host=api.github.com HTTP={status}"):
        DPCSource(opener=opener, sleeper=lambda _: None)._json(API_URL)
    assert len(calls) == attempts
    assert "non pubblicato" not in capsys.readouterr().err


def test_truncated_tree_is_rejected():
    source = DPCSource(opener=lambda *args, **kwargs: Response({"tree": [], "truncated": True}))
    with pytest.raises(DPCError, match="incompleta"):
        source._tree("commit")


def test_invalid_json_is_not_treated_as_bulletin():
    response = Response({})
    response.payload = b"<html>errore</html>"
    with pytest.raises(DPCError, match="JSON non valido"):
        DPCSource(opener=lambda *args, **kwargs: response)._json(API_URL)


class VigilanzaHTTP:
    def __init__(self, bulletin, names=None, style=False):
        from pipeline.source_dpc import VigilanzaSource
        stamp, self.index, self.topologies, _ = bulletin
        self.calls = []
        self.stamp = stamp
        self.style = style
        self.api = VigilanzaSource.api_url
        self.filenames = [f"{stamp}_fenomeni_{day}.json" for day in self.topologies]
        if style:
            self.filenames.append(f"{stamp}_fenomeni_style.json")
        self.replies = {
            self.api: {"default_branch": "main"},
            self.api + "/commits/main": {"sha": "fixed-commit"},
            self.api + "/git/trees/fixed-commit": {"tree": [{"path": "files", "type": "tree", "sha": "files-tree"}]},
            self.api + "/git/trees/files-tree": {"tree": [
                {"path": name, "type": "blob"} for name in (names or [f"{stamp}.json"])
            ] + [{"path": "topojson", "type": "tree", "sha": "topo-tree"}]},
            self.api + "/git/trees/topo-tree": {"tree": [{"path": name, "type": "blob"} for name in self.filenames]},
        }

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, dict(request.header_items()), timeout))
        if request.full_url in self.replies:
            return Response(self.replies[request.full_url])
        assert "/fixed-commit/files/" in request.full_url
        filename = request.full_url.rsplit("/", 1)[-1]
        if filename == self.stamp + ".json":
            return Response(self.index)
        if filename == self.stamp + "_fenomeni_style.json":
            assert self.style
            return Response({"style_properties": [{"propertyKey": "id_fenomeno", "properties": [
                {"property": "label", "values": [{"key": 1, "value": "Pioggia"}]}
            ]}]})
        for day, topology in self.topologies.items():
            if filename == f"{self.stamp}_fenomeni_{day}.json":
                return Response(topology)
        pytest.fail(f"richiesta inattesa: {request.full_url}")


@pytest.mark.parametrize("stamp,now", [
    ("20261025", "2026-10-25T00:05:00+02:00"),
    ("20261025", "2026-10-26T15:00:00+01:00"),
    ("20261007", "2026-10-08T10:00:00+02:00"),
])
@pytest.mark.parametrize("has_style", [True, False])
def test_vigilanza_discovery_fixed_commit_date_fallback_and_optional_style(stamp, now, has_style, vig_bulletin):
    from datetime import datetime
    from pipeline.source_dpc import VigilanzaSource
    http = VigilanzaHTTP(vig_bulletin(stamp), ["20990101.json", "20260101.json", f"{stamp}.json", "20269999.json", "README.md"], style=has_style)
    result = VigilanzaSource(opener=http, token="simulated-token").latest_bulletin(datetime.fromisoformat(now))
    assert result[:3] == vig_bulletin(stamp)[:3]
    assert (result[3] is not None) == has_style
    assert all("Authorization" not in headers for url, headers, _ in http.calls if "raw.githubusercontent.com" in url)
    assert all(headers["Authorization"] == "Bearer simulated-token" for url, headers, _ in http.calls if "api.github.com" in url)
    assert all(timeout == 30 for _, _, timeout in http.calls)
    raw_calls = [url for url, _, _ in http.calls if "raw.githubusercontent.com" in url]
    assert len(raw_calls) == 4 + int(has_style)
    assert all("_oggi.json" not in url or "_fenomeni_oggi.json" in url for url in raw_calls)
    assert any("_fenomeni_style.json" in url for url in raw_calls) == has_style


def test_vigilanza_current_day_uses_rome_not_utc(vig_bulletin):
    from datetime import datetime, timezone
    from pipeline.source_dpc import VigilanzaSource
    http = VigilanzaHTTP(vig_bulletin(), ["20261024.json", "20261025.json", "20261026.json"])
    result = VigilanzaSource(opener=http).latest_bulletin(datetime(2026, 10, 24, 22, 5, tzinfo=timezone.utc))
    assert result[0] == "20261025"


def test_vigilanza_future_only_is_error(vig_bulletin):
    from datetime import datetime, timezone
    from pipeline.source_dpc import VigilanzaSource
    http = VigilanzaHTTP(vig_bulletin(), ["20990101.json"])
    with pytest.raises(DPCError, match="Europe/Rome"):
        VigilanzaSource(opener=http).latest_bulletin(datetime(2026, 10, 25, tzinfo=timezone.utc))


def test_vigilanza_missing_points_and_truncated_tree_are_errors(vig_bulletin):
    from datetime import datetime, timezone
    from pipeline.source_dpc import VigilanzaSource
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    http = VigilanzaHTTP(vig_bulletin())
    http.replies[http.api + "/git/trees/topo-tree"]["tree"].pop()
    with pytest.raises(DPCError, match="punti vigilanza mancanti"):
        VigilanzaSource(opener=http).latest_bulletin(now)
    http.replies[http.api + "/git/trees/files-tree"]["truncated"] = True
    with pytest.raises(DPCError, match="incompleta"):
        VigilanzaSource(opener=http).latest_bulletin(now)


def test_vigilanza_rejects_missing_or_duplicate_day():
    from pipeline.source_dpc import vigilanza_days
    with pytest.raises(DPCError, match="today"):
        vigilanza_days({"tomorrow": {}})
    with pytest.raises(DPCError, match="duplicato"):
        vigilanza_days({"today": {}, "aftertomorrow": {}, "after_tomorrow": {}})


def test_vigilanza_declared_style_download_failure_is_error(vig_bulletin):
    from datetime import datetime, timezone
    from pipeline.source_dpc import VigilanzaSource
    http = VigilanzaHTTP(vig_bulletin(), style=True)

    def opener(request, timeout):
        if request.full_url.endswith("_fenomeni_style.json"):
            raise HTTPError(request.full_url, 404, "stile mancante", {}, None)
        return http(request, timeout)

    with pytest.raises(DPCError, match="HTTP=404"):
        VigilanzaSource(opener=opener).latest_bulletin(datetime(2026, 10, 25, tzinfo=timezone.utc))

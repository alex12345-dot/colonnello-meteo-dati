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

from pathlib import Path
import json
import socket

import pytest

from pipeline.grib2 import decode
from pipeline.source_dpc import vigilanza_days
import pipeline.build as build_module


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def vig_bulletin():
    def load(stamp="20261025"):
        index = json.loads((FIXTURES / f"vig_{stamp}.json").read_text(encoding="utf-8"))
        today = json.loads((FIXTURES / f"vig_{stamp}_fenomeni_oggi.topo.json").read_text(encoding="utf-8"))
        # Le fixture dei punti esistono solo per oggi. Gli altri giorni simulano
        # esplicitamente una collezione vuota, senza riusare punti di date diverse.
        empty = {"type": "Topology", "objects": {"empty": {"type": "GeometryCollection", "geometries": []}}, "arcs": []}
        topologies = {day: today if day == "oggi" else empty for day, _ in vigilanza_days(index)}
        return stamp, index, topologies, None
    return load


@pytest.fixture
def dpc_pair():
    return tuple(json.loads((FIXTURES / f"dpc_20261025_1326_{day}.json").read_text(encoding="utf-8"))
                 for day in ("today", "tomorrow"))


@pytest.fixture(autouse=True)
def offline_dpc(monkeypatch, dpc_pair, vig_bulletin):
    class OfflineDPC:
        def latest_bulletin(self):
            return "20261025_1326", *dpc_pair

    monkeypatch.setattr(build_module, "DPCSource", OfflineDPC)

    class OfflineVigilanza:
        def latest_bulletin(self):
            return vig_bulletin()

    monkeypatch.setattr(build_module, "VigilanzaSource", OfflineVigilanza)

    def forbidden_network(*args, **kwargs):
        pytest.fail("rete vietata nei test: usare fixture e opener simulati")

    monkeypatch.setattr(socket.socket, "connect", forbidden_network)


@pytest.fixture(scope="session")
def fixture_bytes():
    return (FIXTURES / "ifs-12h-surface.grib2").read_bytes()


@pytest.fixture(scope="session")
def fields(fixture_bytes):
    decoded = decode(fixture_bytes)
    return {field.parameter: field for field in decoded}


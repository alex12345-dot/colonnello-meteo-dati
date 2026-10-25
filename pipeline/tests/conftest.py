from pathlib import Path
import json
import socket

import pytest

from pipeline.grib2 import decode
import pipeline.build as build_module


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def dpc_pair():
    return tuple(json.loads((FIXTURES / f"dpc_20261025_1326_{day}.json").read_text(encoding="utf-8"))
                 for day in ("today", "tomorrow"))


@pytest.fixture(autouse=True)
def offline_dpc(monkeypatch, dpc_pair):
    class OfflineDPC:
        def latest_bulletin(self):
            return "20261025_1326", *dpc_pair

    monkeypatch.setattr(build_module, "DPCSource", OfflineDPC)

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


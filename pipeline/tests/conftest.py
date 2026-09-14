from pathlib import Path

import pytest

from pipeline.grib2 import decode


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_bytes():
    return (FIXTURES / "ifs-12h-surface.grib2").read_bytes()


@pytest.fixture(scope="session")
def fields(fixture_bytes):
    decoded = decode(fixture_bytes)
    return {field.parameter: field for field in decoded}


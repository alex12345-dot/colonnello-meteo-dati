from pathlib import Path

from pipeline.build import build, parse_steps
from pipeline.grib2 import iter_messages


FIXTURE = Path(__file__).parent / "fixtures" / "ifs-12h-surface.grib2"


class OfflineSource:
    def __init__(self):
        messages = [bytes(message) for message in iter_messages(FIXTURE.read_bytes())]
        self.payloads = dict(zip(("tp", "10u", "10v", "2t"), messages, strict=True))

    def download_parameters(self, model, run, step, parameters, optional=()):
        return {name: self.payloads[name] for name in parameters}, []


def test_native_model_steps():
    assert parse_steps("0-12", "ifs") == [0, 3, 6, 9, 12]
    assert parse_steps("0-12", "aifs-single") == [0, 6, 12]


def test_offline_build_writes_layers_and_manifest(tmp_path):
    manifest = build(
        "ifs",
        "2026091300",
        "12",
        "wind10m,t2m,precip",
        tmp_path,
        source=OfflineSource(),
    )
    assert manifest["steps"][0]["valid_time"] == "2026-09-13T12:00:00Z"
    assert manifest["license"] == "CC-BY-4.0"
    assert set(manifest["steps"][0]["layers"]) == {"wind10m", "t2m", "precip"}
    assert (tmp_path / "manifest.json").is_file()
    assert (tmp_path / "wind10m" / "012.png").is_file()



def test_manifest_dichiara_origine_della_griglia(tmp_path):
    """La griglia ECMWF parte a 180 gradi est: senza origine dichiarata il client
    disegna il mondo ruotato di meta' senza che nulla segnali l'errore."""
    manifest = build(
        "ifs", "2026091300", "12", "wind10m,t2m,precip", tmp_path, source=OfflineSource()
    )
    grid = manifest["grid"]
    assert grid["width"] == 1440 and grid["height"] == 721
    assert grid["lon0"] == -180.0, "origine longitudine mancante o sbagliata"
    assert grid["lat0"] == 90.0
    assert grid["dlon"] == 0.25 and grid["dlat"] == 0.25
    assert "scanning_mode" in grid

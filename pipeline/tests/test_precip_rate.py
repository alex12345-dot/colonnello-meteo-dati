"""Verifica offline del contratto precip_rate."""
from types import SimpleNamespace

import numpy as np
import pytest

import pipeline.build as build_module
from pipeline.encode import encode_precip_rate, encode_scalar, read_png


def decoded(path, entry):
    return (entry["min"] + read_png(path) * entry["quantization_step"]) ** 2


@pytest.fixture
def synthetic_build(monkeypatch, tmp_path):
    monkeypatch.setattr(build_module, "_field", lambda payload: payload)

    def run(cumulatives, steps, layers="precip,precip_rate", model="ifs"):
        calls = []

        class Source:
            def download_parameters(self, model, run, step, parameters, optional=()):
                calls.append((step, tuple(parameters)))
                if step not in cumulatives:
                    return {}, ["tp"]
                values = np.atleast_2d(cumulatives[step])
                grid = SimpleNamespace(ni=values.shape[1], nj=values.shape[0],
                                       lat1=90.0, lon1=180.0, dj=0.25, di=0.25, scanning_mode=0)
                return {"tp": SimpleNamespace(parameter="tp", values=values, grid=grid)}, []

        manifest = build_module.build(model, "2026110100", steps, layers, tmp_path, source=Source())
        return manifest, calls

    return run


def test_wet_unchanged_area_is_exact_zero_and_precip_is_unchanged(synthetic_build, tmp_path):
    before = np.array([[0.012, 0.024], [0.036, 0.048]])
    after = before + [[0, 0], [0.00045, 0.150]]
    manifest, calls = synthetic_build({0: before, 3: after}, "0,3")
    assert manifest["layers"] == ["precip", "precip_rate"]
    assert "precip_rate" not in manifest["steps"][0]["layers"]
    assert not (tmp_path / "precip_rate/000.png").exists()
    entry = manifest["steps"][1]["layers"]["precip_rate"]
    assert entry["url"] == "precip_rate/003.png"
    assert entry["units"] == "mm/h" and entry["interval_hours"] == 3
    assert entry["encoding"] == "grayscale8" and entry["transform"] == "sqrt"
    values = decoded(tmp_path / entry["url"], entry)
    assert np.all(values[0] == 0)
    assert abs(values[1, 0] - 0.15) < 0.02
    assert np.isclose(values[1, 1], 50)
    assert calls == [(0, ("tp",)), (3, ("tp",))]
    assert manifest["warnings"] == []
    for index, original in enumerate((before, after)):
        expected_path = tmp_path / "expected.png"
        metadata = encode_scalar(original, expected_path)
        cumulative = manifest["steps"][index]["layers"]["precip"]
        assert cumulative == {"url": f"precip/{index * 3:03d}.png", **metadata, "units": "m"}
        assert (tmp_path / cumulative["url"]).read_bytes() == expected_path.read_bytes()


@pytest.mark.parametrize("model,steps,intervals", [
    ("ifs", "138,141,144,150,156", [3, 3, 6, 6]),
    ("aifs", "0,6,12", [6, 6]),
    ("ifs", "0,12", [12]),
])
def test_actual_intervals(synthetic_build, tmp_path, model, steps, intervals):
    selected = [int(s) for s in steps.split(",")]
    manifest, calls = synthetic_build({s: [[s * 0.00015]] for s in selected},
                                     steps, layers="precip_rate", model=model)
    assert "precip_rate" not in manifest["steps"][0]["layers"]
    for item, hours in zip(manifest["steps"][1:], intervals, strict=True):
        entry = item["layers"]["precip_rate"]
        assert entry["interval_hours"] == hours
        assert type(entry["interval_hours"]) is int
        assert np.allclose(decoded(tmp_path / entry["url"], entry), 0.15)
    assert calls == [(s, ("tp",)) for s in selected]


def test_step_zero_alone_has_no_rate(synthetic_build):
    manifest, _ = synthetic_build({0: [[0]]}, "0", layers="precip_rate")
    assert manifest["steps"][0]["layers"] == {}
    assert manifest["warnings"] == []


def test_missing_previous_is_declared_and_missing_tp_breaks_chain(synthetic_build, tmp_path):
    manifest, calls = synthetic_build({12: [[0.02]], 18: [[0.03]], 21: [[0.04]]}, "12,15,18,21")
    assert all("precip_rate" not in s["layers"] for s in manifest["steps"][:3])
    assert not (tmp_path / "precip_rate/012.png").exists()
    assert not (tmp_path / "precip_rate/018.png").exists()
    for step in (12, 18):
        assert any(w.get("step") == step and w.get("missing_layers") == ["precip_rate"]
                   for w in manifest["warnings"])
    assert {"step": 15, "missing_parameters": ["tp"]} in manifest["warnings"]
    assert manifest["steps"][3]["layers"]["precip_rate"]["interval_hours"] == 3
    assert calls == [(s, ("tp",)) for s in (12, 15, 18, 21)]


def test_negative_differences_and_nonfinite_pixels_encode_zero(synthetic_build, tmp_path):
    manifest, _ = synthetic_build({0: [[0.01, 0.02, 0.03, 0.04]],
                                  3: [[0.009, 0.02, np.nan, 0.043]]}, "0,3")
    entry = manifest["steps"][1]["layers"]["precip_rate"]
    values = decoded(tmp_path / entry["url"], entry)
    assert np.all(values[0, :3] == 0)
    assert np.isclose(values[0, 3], 1)


def test_subtraction_converts_inputs_to_float64_first(synthetic_build):
    before = np.array([[1e-8]], dtype=np.float32)
    after = np.array([[1]], dtype=np.float32)
    manifest, _ = synthetic_build({0: before, 3: after}, "0,3")
    expected = np.sqrt(((after.astype(np.float64) - before.astype(np.float64)) * 1000 / 3)[0, 0])
    assert manifest["steps"][1]["layers"]["precip_rate"]["max"] == expected


@pytest.mark.parametrize("values", [[[0, 0, 0]], [[0.15, 50, np.nan, np.inf, -np.inf]]])
def test_fixed_zero_encoding_and_nonfinite_pixels(tmp_path, values):
    path = tmp_path / "rate.png"
    metadata = encode_precip_rate(np.array(values), path)
    assert metadata["min"] == 0
    assert metadata["quantization_step"] == metadata["max"] / 255
    if metadata["max"] == 0:
        assert np.all(read_png(path) == 0)
    else:
        assert metadata["max"] == np.sqrt(50)
        assert np.all(read_png(path)[0, 2:] == 0)
        assert abs(decoded(path, metadata)[0, 0] - 0.15) < 0.02

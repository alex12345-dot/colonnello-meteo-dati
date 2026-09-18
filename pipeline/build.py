"""CLI per produrre PNG e manifest Colonnello Meteo dagli open data ECMWF."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys
from time import monotonic

try:
    from pipeline.encode import encode_scalar, encode_wind
    from pipeline.grib2 import decode_message, iter_messages
    from pipeline.source_ecmwf import ECMWFSource, normalize_run, run_id
except ModuleNotFoundError:  # esecuzione diretta dalla cartella pipeline
    from encode import encode_scalar, encode_wind
    from grib2 import decode_message, iter_messages
    from source_ecmwf import ECMWFSource, normalize_run, run_id


LAYER_PARAMETERS = {
    "wind10m": ("10u", "10v"),
    "t2m": ("2t",),
    "precip": ("tp",),
    "msl": ("msl",),
    "gust": ("10fg",),
}
OPTIONAL_PARAMETERS = {"10fg"}


def valid_steps(model: str, maximum: int = 360) -> list[int]:
    if model == "ifs":
        return list(range(0, min(maximum, 144) + 1, 3)) + (
            list(range(150, maximum + 1, 6)) if maximum > 144 else []
        )
    return list(range(0, maximum + 1, 6))


def parse_steps(specification: str, model: str) -> list[int]:
    result: set[int] = set()
    native = set(valid_steps(model))
    for part in specification.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"intervallo passi invertito: {part}")
            result.update(step for step in native if start <= step <= end)
        else:
            result.add(int(part))
    invalid = sorted(result - native)
    if invalid or not result:
        raise ValueError(f"passi non validi per {model}: {invalid or specification}")
    return sorted(result)


def parse_layers(specification: str) -> list[str]:
    layers = list(dict.fromkeys(name.strip() for name in specification.split(",") if name.strip()))
    unknown = [name for name in layers if name not in LAYER_PARAMETERS]
    if unknown:
        raise ValueError("layer non supportati: " + ", ".join(unknown))
    return layers


def _field(payload: bytes):
    messages = list(iter_messages(payload))
    if len(messages) != 1:
        raise ValueError(f"atteso un messaggio GRIB, trovati {len(messages)}")
    return decode_message(messages[0])


def build(
    model: str,
    run: str,
    steps_spec: str,
    layers_spec: str,
    output: str | Path,
    *,
    source: ECMWFSource | None = None,
) -> dict:
    model = "aifs-single" if model == "aifs" else model
    steps = parse_steps(steps_spec, "ifs" if model == "ifs" else "aifs-single")
    layers = parse_layers(layers_spec)
    source = source or ECMWFSource()
    run_time = source.latest_run(model, step=steps[0]) if run == "latest" else normalize_run(run)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)

    parameters = list(
        dict.fromkeys(parameter for layer in layers for parameter in LAYER_PARAMETERS[layer])
    )
    optional = OPTIONAL_PARAMETERS.intersection(parameters)
    manifest_steps = []
    grid_meta = None
    warnings = []
    for step in steps:
        step_started = monotonic()
        payloads, absent = source.download_parameters(
            model, run_time, step, parameters, optional=optional
        )
        fields = {name: _field(payload) for name, payload in payloads.items()}
        for name, field in fields.items():
            if field.parameter != name and not (name == "10fg" and field.parameter == "gust"):
                raise ValueError(f"byte-range {name} contiene il parametro {field.parameter}")
        if absent:
            warnings.append({"step": step, "missing_parameters": absent})

        layer_entries = {}
        for layer in layers:
            needed = LAYER_PARAMETERS[layer]
            if any(name not in fields for name in needed):
                continue
            relative = Path(layer) / f"{step:03d}.png"
            path = destination / relative
            if layer == "wind10m":
                metadata = encode_wind(fields["10u"].values, fields["10v"].values, path)
            else:
                metadata = encode_scalar(fields[needed[0]].values, path)
            layer_entries[layer] = {"url": relative.as_posix(), **metadata}

        if grid_meta is None:
            g = next(iter(fields.values())).grid
            grid_meta = {
                "width": g.ni, "height": g.nj,
                "lat0": g.lat1, "lon0": ((g.lon1 + 180.0) % 360.0) - 180.0,
                "dlat": g.dj, "dlon": g.di,
                "scanning_mode": g.scanning_mode,
            }

        valid_time = run_time + timedelta(hours=step)
        manifest_steps.append(
            {
                "step": step,
                "valid_time": valid_time.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "layers": layer_entries,
            }
        )
        print(
            f"passo {step} completato in {monotonic() - step_started:.2f} s",
            file=sys.stderr,
        )

    manifest = {
        "model": model,
        "run": run_id(run_time),
        "grid": grid_meta,
        "layers": layers,
        "steps": manifest_steps,
        "license": "CC-BY-4.0",
        "attribution": (
            "ECMWF Open Data © European Centre for Medium-Range Weather Forecasts "
            "(ECMWF), licensed under CC BY 4.0"
        ),
        "warnings": warnings,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("ifs", "aifs", "aifs-single"), required=True)
    parser.add_argument("--run", required=True, help="latest oppure YYYYMMDDHH")
    parser.add_argument("--steps", required=True, help="es. 0-144 oppure 0,3,6")
    parser.add_argument("--layers", required=True, help="wind10m,t2m,precip,msl,gust")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    build(args.model, args.run, args.steps, args.layers, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

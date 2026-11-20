"""Previsioni orarie di temporali ICON-EU in un unico PNG a tasselli."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np

try:
    from pipeline.encode import write_png
    from pipeline.source_dwd import DWDSource, DWDError, TERMINAL_STEPS, north_south
    from pipeline.source_ecmwf import normalize_run, run_id
except ModuleNotFoundError:
    from encode import write_png
    from source_dwd import DWDSource, DWDError, TERMINAL_STEPS, north_south
    from source_ecmwf import normalize_run, run_id


BBOX = {"west": -6.0, "south": 35.0, "east": 21.0, "north": 52.0}
RESOLUTION = 0.0625
TILE_COLUMNS = 6
NODATA = 255
STALE_AFTER_HOURS = 12
CLASS_LABELS = {"0": "nessun temporale", "1": "temporale", "2": "temporale forte"}
CLASS_CODES = {
    0: (0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
        71, 73, 75, 77, 80, 81, 82, 85, 86),
    1: (95, 97, 98),
    2: (96,),  # DWD ICON-EU: temporale forte. Il 99 e' previsto solo nei modelli D2.
}
CLASS_LOOKUP = np.full(100, NODATA, dtype=np.uint8)
for weather_class, codes in CLASS_CODES.items():
    CLASS_LOOKUP[list(codes)] = weather_class


def classify_weather(values: np.ndarray) -> tuple[np.ndarray, dict[str, int]]:
    classes = np.full(values.shape, NODATA, dtype=np.uint8)
    in_table = (values >= 0) & (values < CLASS_LOOKUP.size) & (values == np.floor(values))
    classes[in_table] = CLASS_LOOKUP[values[in_table].astype(np.uint8)]
    unknown, counts = np.unique(values[(classes == NODATA) & ~np.isnan(values)], return_counts=True)
    return classes, {np.format_float_positional(code, trim="-"): int(count)
                     for code, count in zip(unknown, counts)}


def crop_weather(field):
    values, grid = north_south(field, allow_missing=True)
    if (grid["dlat"], grid["dlon"]) != (RESOLUTION, RESOLUTION):
        raise DWDError(f"risoluzione griglia ww inattesa: {grid}")
    x = (BBOX["west"] - grid["lon0"]) / RESOLUTION
    y = (grid["lat0"] - BBOX["north"]) / RESOLUTION
    width = int((BBOX["east"] - BBOX["west"]) / RESOLUTION) + 1
    height = int((BBOX["north"] - BBOX["south"]) / RESOLUTION) + 1
    if (not x.is_integer() or not y.is_integer() or x < 0 or y < 0
            or x + width > grid["width"] or y + height > grid["height"]):
        raise DWDError(f"griglia ww non allineata al ritaglio atteso: {grid}")
    column, row = int(x), int(y)
    return values[row:row + height, column:column + width], {
        **grid, "lat0": BBOX["north"], "lon0": BBOX["west"], "width": width, "height": height,
    }


def iso_utc(instant: datetime) -> str:
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def build_temporali_eu(destination: Path, source: DWDSource, *, run: datetime | None = None) -> dict:
    run = normalize_run(run if run is not None else source.latest_weather_run())
    steps = []
    class_counts = np.zeros(256, dtype=np.int64)
    unknown_counts = Counter()
    source_grid = None
    step_count = TERMINAL_STEPS["ww"]
    for step in range(1, step_count + 1):
        field = source.download_weather(run, step)
        if source_grid is not None and field.grid != source_grid:
            raise DWDError(f"griglia ww cambiata al passo {step}")
        values, grid = crop_weather(field)
        if source_grid is None:
            source_grid = field.grid
            width, height = grid["width"], grid["height"]
            tile_rows = (step_count + TILE_COLUMNS - 1) // TILE_COLUMNS
            pixels = np.full((tile_rows * height, TILE_COLUMNS * width), NODATA, dtype=np.uint8)
        classes, unknown = classify_weather(values)
        tile_y, tile_x = divmod(step - 1, TILE_COLUMNS)
        x, y = tile_x * width, tile_y * height
        pixels[y:y + height, x:x + width] = classes
        class_counts += np.bincount(classes.ravel(), minlength=256)
        unknown_counts.update(unknown)
        steps.append({"step": step, "valid_time": iso_utc(run + timedelta(hours=step))})
        source.check_budget()

    stamp = run_id(run)
    relative = f"temporali_eu/{stamp}.png"
    path = destination / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".temporali-", dir=path.parent) as temporary:
        candidate = Path(temporary) / path.name
        write_png(candidate, pixels)
        source.check_budget()
        candidate.replace(path)
    unknown_counts = dict(sorted(unknown_counts.items()))
    if unknown_counts:
        print(f"::warning::ICON-EU temporali_eu corsa={stamp} codici ww sconosciuti: {unknown_counts}", file=sys.stderr)
    return {
        "model": "icon-eu", "run": stamp, "generated_at": iso_utc(datetime.now(timezone.utc)),
        "attribution": "Deutscher Wetterdienst (DWD), ICON-EU, CC BY 4.0", "license": "CC-BY-4.0",
        "url": relative, "bbox": BBOX.copy(), "grid": grid, "encoding": "grayscale8",
        "tile_columns": TILE_COLUMNS, "interval_hours": 1, "nodata": NODATA,
        "steps": steps, "class_counts": {str(c): int(class_counts[c]) for c in (*CLASS_CODES, NODATA)},
        "unknown_ww_counts": unknown_counts, "class_labels": CLASS_LABELS.copy(),
        "classification": "WMO-4677", "hail_diagnostic": False,
    }

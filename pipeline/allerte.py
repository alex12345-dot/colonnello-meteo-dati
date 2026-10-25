"""Riduzione pura delle allerte e delle geometrie GeoJSON DPC, senza librerie GIS."""

from __future__ import annotations

from datetime import datetime, timedelta
from collections import Counter
import csv
from functools import lru_cache
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import unicodedata
from zoneinfo import ZoneInfo

try:
    from pipeline.source_dpc import REPOSITORY_URL
except ModuleNotFoundError:
    from source_dpc import REPOSITORY_URL


LEVELS = {"NESSUNA ALLERTA": 0, "ALLERTA GIALLA": 1, "ALLERTA ARANCIONE": 2, "ALLERTA ROSSA": 3}
RISKS = {
    "sintesi": "Rappresentata nella mappa",
    "idrogeologico": "Per rischio idrogeologico",
    "temporali": "Per rischio temporali",
    "idraulico": "Per rischio idraulico",
}
MAX_ZONE_BYTES = 700_000
MIN_PART_AREA = 1e-7  # gradi quadrati, prima dell'arrotondamento
COMUNI_REGIONI = Path(__file__).parent / "data" / "comuni_regioni.csv"


def normalize_comune(name: str) -> str:
    name = name.translate(str.maketrans({"’": "'", "‘": "'", "`": "'", "ʼ": "'", "´": "'"}))
    name = unicodedata.normalize("NFKD", name.casefold())
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"\s*'\s*", "'", " ".join(name.split()))


@lru_cache(maxsize=1)
def load_comuni_regioni() -> dict[str, str]:
    aliases: dict[str, set[str]] = {}
    with COMUNI_REGIONI.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(line for line in stream if not line.startswith("#")):
            for name in [row["comune"], *row["comune"].split("/")]:
                aliases.setdefault(normalize_comune(name), set()).add(row["regione"])
    # Un omonimo presente in regioni diverse non e' una corrispondenza certa.
    return {name: next(iter(regions)) for name, regions in aliases.items() if len(regions) == 1}


def zone_region(name: str, comuni, regions: dict[str, str]) -> str | None:
    names = {normalize_comune(c) for c in comuni if isinstance(c, str) and c.strip()} if isinstance(comuni, list) else set()
    votes = Counter(regions[c] for c in names if c in regions)
    recognized = sum(votes.values())
    coverage = recognized / len(names) if names else 0
    if coverage < 0.5:
        logging.getLogger(__name__).warning("DPC zona %s: comuni riconosciuti %.1f%%; regione nulla", name, 100 * coverage)
        return None
    region, count = votes.most_common(1)[0]
    if count * 2 <= recognized:
        logging.getLogger(__name__).warning("DPC zona %s: nessuna maggioranza regionale (riconosciuti %.1f%%)", name, 100 * coverage)
        return None
    return region


class _UnrepresentableRing(ValueError):
    """Anello degenere o collassato sul reticolo a quattro decimali."""


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def alert_level(value: str) -> int:
    if isinstance(value, str):
        level = value.rsplit("/", 1)[-1].strip()
        if level in LEVELS:
            return LEVELS[level]
    raise ValueError(f"livello di allerta DPC non riconosciuto: {value!r}")


def _zones(collection: dict) -> dict[str, dict]:
    if collection.get("type") != "FeatureCollection" or not collection.get("features"):
        raise ValueError("DPC: attesa FeatureCollection non vuota")
    zones = {}
    for feature in collection["features"]:
        name = feature["properties"]["Nome zona"]
        if not isinstance(name, str) or not name.strip() or name in zones:
            raise ValueError(f"nome zona DPC vuoto o duplicato: {name!r}")
        zones[name] = feature
    return zones


def reduce_alerts(today: dict, tomorrow: dict, bulletin: str) -> dict:
    emitted = datetime.strptime(bulletin, "%Y%m%d_%H%M").replace(tzinfo=ZoneInfo("Europe/Rome"))
    current, following = _zones(today), _zones(tomorrow)
    if current.keys() != following.keys():
        raise ValueError(f"zone DPC today/tomorrow diverse: {sorted(current.keys() ^ following.keys())}")
    zones = []
    regions = load_comuni_regioni()
    for name in sorted(current):
        zone = {"nome": name, "regione": zone_region(name, current[name]["properties"].get("Comuni"), regions)}
        for day, features in (("oggi", current), ("domani", following)):
            properties = features[name]["properties"]
            zone[day] = {key: alert_level(properties.get(source)) for key, source in RISKS.items()}
        zones.append(zone)
    return {
        "fonte": "Dipartimento della Protezione Civile",
        "licenza": "CC BY 4.0",
        "url_fonte": REPOSITORY_URL,
        "bollettino": bulletin,
        "emesso": emitted.isoformat(timespec="seconds"),
        "oggi": emitted.date().isoformat(),
        "domani": (emitted.date() + timedelta(days=1)).isoformat(),
        "zone": zones,
    }


def geometry_fingerprint(collection: dict) -> str:
    geometries = [{"nome": name, "geometry": feature["geometry"]} for name, feature in sorted(_zones(collection).items())]
    # sort_keys rende l'impronta indipendente anche dall'ordine delle chiavi GeoJSON.
    canonical = json.dumps(geometries, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _distance_squared(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    ratio = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy) if dx or dy else 0
    ratio = min(1, max(0, ratio))
    return (point[0] - start[0] - ratio * dx) ** 2 + (point[1] - start[1] - ratio * dy) ** 2


def _douglas_peucker(points, tolerance):
    """Versione iterativa: non esaurisce lo stack su coste molto dettagliate."""
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        start, end = pending.pop()
        index, distance = start, tolerance * tolerance
        for i in range(start + 1, end):
            candidate = _distance_squared(points[i], points[start], points[end])
            if candidate > distance:
                index, distance = i, candidate
        if index != start:
            keep.add(index)
            pending.extend(((start, index), (index, end)))
    return [points[i] for i in sorted(keep)]


def _area(ring):
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:])) / 2


def _round_ring(points):
    result = []
    for point in points:
        rounded = [round(point[0], 4), round(point[1], 4)]
        if not result or result[-1] != rounded:
            result.append(rounded)
    if result and result[-1] != result[0]:
        result.append(result[0][:])
    return result


def _valid_ring(ring):
    return len(ring) >= 4 and len({tuple(p) for p in ring[:-1]}) >= 3 and abs(_area(ring)) > 1e-12


def _quantize_ring(ring):
    rounded = _round_ring(ring)
    if _valid_ring(rounded):
        return rounded
    if not _valid_ring(ring):
        raise _UnrepresentableRing("anello DPC degenere nella fonte")
    raise _UnrepresentableRing("anello DPC non rappresentabile a quattro decimali")


def _simplify_ring(ring, tolerance):
    if len(ring) < 4 or ring[0] != ring[-1]:
        raise ValueError("anello DPC non chiuso o con meno di quattro punti")
    if any(len(p) < 2 or not all(math.isfinite(v) for v in p[:2]) for p in ring):
        raise ValueError("coordinate DPC non valide")
    original = _quantize_ring(ring)
    # Due catene aperte evitano il segmento iniziale di lunghezza zero del DP
    # applicato direttamente a un anello chiuso. Conserviamo anche le piccole isole.
    pivot = max(range(1, len(ring) - 1), key=lambda i: _distance_squared(ring[i], ring[0], ring[0]))
    simplified = _round_ring(_douglas_peucker(ring[:pivot + 1], tolerance)[:-1] + _douglas_peucker(ring[pivot:], tolerance))
    if _valid_ring(simplified) and _area(simplified) * _area(original) > 0:
        return simplified
    return original


def simplify_zones(collection: dict, *, max_bytes: int = MAX_ZONE_BYTES) -> dict:
    zones = _zones(collection)
    tolerance = 0.001  # circa 100 metri; si aumenta soltanto se necessario al limite.
    for _ in range(12):
        features = []
        for name, feature in sorted(zones.items()):
            geometry = feature["geometry"]
            kind = geometry["type"]
            if kind not in ("Polygon", "MultiPolygon"):
                raise ValueError(f"geometria DPC non supportata: {kind}")
            polygons = [geometry["coordinates"]] if kind == "Polygon" else geometry["coordinates"]
            if not polygons or any(not polygon for polygon in polygons):
                raise ValueError(f"poligono DPC vuoto: {name}")
            reduced = []
            discarded = {"parti": 0, "buchi": 0}
            for polygon in polygons:
                try:
                    outer = _simplify_ring(polygon[0], tolerance)
                except _UnrepresentableRing:
                    if kind == "Polygon":
                        raise
                    discarded["parti"] += 1
                    continue
                if kind == "MultiPolygon" and abs(_area(polygon[0])) < MIN_PART_AREA:
                    discarded["parti"] += 1
                    continue
                rings = [outer]
                for hole in polygon[1:]:
                    try:
                        rings.append(_simplify_ring(hole, tolerance))
                    except _UnrepresentableRing:
                        discarded["buchi"] += 1
                reduced.append(rings)
            if not reduced:
                raise ValueError(f"zona DPC senza parti valide: {name}")
            properties = {"nome": name}
            if any(discarded.values()):
                properties["scartate"] = discarded
            features.append({"type": "Feature", "properties": properties, "geometry": {
                "type": kind, "coordinates": reduced[0] if kind == "Polygon" else reduced,
            }})
        result = {"type": "FeatureCollection", "features": features}
        if len(json_bytes(result)) <= max_bytes:
            return result
        tolerance *= 2
    raise ValueError(f"geometrie DPC oltre il limite di {max_bytes} byte")

from copy import deepcopy
import json

import pytest

from pipeline.allerte import (
    alert_level, geometry_fingerprint, json_bytes, load_comuni_regioni,
    reduce_alerts, simplify_zones, zone_region,
)
from pipeline.tests.conftest import FIXTURES


def test_real_quiet_bulletin_and_name_pairing(dpc_pair):
    today, tomorrow = dpc_pair
    tomorrow["features"].reverse()
    result = reduce_alerts(today, tomorrow, "20261025_1326")
    assert result["fonte"] == "Dipartimento della Protezione Civile"
    assert result["licenza"] == "CC BY 4.0"
    assert result["emesso"] == "2026-10-25T13:26:00+01:00"
    assert result["oggi"] == "2026-10-25" and result["domani"] == "2026-10-26"
    assert len(result["zone"]) == 10
    assert all(set(z[day].values()) == {0} for z in result["zone"] for day in ("oggi", "domani"))
    assert "Comuni" not in json_bytes(result).decode()
    assert all(set(z) == {"nome", "regione", "oggi", "domani"} for z in result["zone"])


def test_real_orange_and_yellow_bulletin():
    today = json.loads((FIXTURES / "dpc_20261007_1424_today.json").read_text(encoding="utf-8"))
    result = reduce_alerts(today, today, "20261007_1424")
    by_name = {z["nome"]: z for z in result["zone"]}
    assert by_name["Arno-Firenze"]["oggi"] == {
        "sintesi": 2, "idrogeologico": 2, "temporali": 2, "idraulico": 0,
    }
    assert by_name["Versante Jonico Meridionale"]["oggi"] == {
        "sintesi": 1, "idrogeologico": 1, "temporali": 1, "idraulico": 1,
    }
    firenze = next(f for f in today["features"] if f["properties"]["Nome zona"] == "Arno-Firenze")
    assert "Firenze" in firenze["properties"]["Comuni"]
    assert result["emesso"] == "2026-10-07T14:24:00+02:00"


@pytest.mark.parametrize("value", ["Assenza / LIVELLO NUOVO", "", None, 0, "ALLERTA VERDE"])
def test_unknown_level_fails_closed(value):
    with pytest.raises(ValueError, match="non riconosciuto"):
        alert_level(value)


def test_last_slash_and_red_level():
    assert alert_level("descrizione / ulteriore / ALLERTA ROSSA ") == 3


@pytest.mark.parametrize("day", [0, 1])
def test_unknown_property_fails_entire_bulletin(dpc_pair, day):
    dpc_pair[day]["features"][0]["properties"]["Per rischio temporali"] = "SCONOSCIUTO"
    with pytest.raises(ValueError, match="non riconosciuto"):
        reduce_alerts(*dpc_pair, "20261025_1326")


@pytest.mark.parametrize("day", [0, 1])
def test_unmatched_zone_is_error(dpc_pair, day):
    dpc_pair[day]["features"].pop()
    with pytest.raises(ValueError, match="diverse"):
        reduce_alerts(*dpc_pair, "20261025_1326")


def test_duplicate_zone_is_error(dpc_pair):
    dpc_pair[0]["features"].append(dpc_pair[0]["features"][0])
    with pytest.raises(ValueError, match="duplicato"):
        reduce_alerts(*dpc_pair, "20261025_1326")


@pytest.mark.parametrize("stamp,tomorrow,offset", [
    ("20261231_1600", "2027-01-01", "+01:00"),
    ("20260328_1600", "2026-03-29", "+01:00"),
    ("20260329_1600", "2026-03-30", "+02:00"),
])
def test_calendar_and_rome_offset(dpc_pair, stamp, tomorrow, offset):
    result = reduce_alerts(*dpc_pair, stamp)
    assert result["domani"] == tomorrow
    assert result["emesso"].endswith(offset)


def rings(geometry):
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return [ring for polygon in polygons for ring in polygon]


@pytest.mark.parametrize("fixture", ["dpc_20261007_1424_today.json", "dpc_20261025_1326_today.json"])
def test_simplified_geometry_is_small_closed_and_not_degenerate(fixture):
    original = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    before = deepcopy(original)
    result = simplify_zones(original)
    assert original == before  # riduzione pura
    assert len(json_bytes(result)) <= 700_000
    assert len(json_bytes(result)) < len(json_bytes(original)) / 3
    assert len(result["features"]) == len(original["features"])
    for old, feature in zip(sorted(original["features"], key=lambda f: f["properties"]["Nome zona"]), result["features"], strict=True):
        discarded = 2 if old["properties"]["Nome zona"] == "Bacino del Pescara" else 0
        properties = {"nome": old["properties"]["Nome zona"]}
        if discarded:
            properties["scartate"] = {"parti": discarded, "buchi": 0}
        assert feature["properties"] == properties
        assert feature["geometry"]["type"] == old["geometry"]["type"]
        assert len(rings(feature["geometry"])) + discarded == len(rings(old["geometry"]))
        for ring in rings(feature["geometry"]):
            assert len(ring) >= 4 and ring[0] == ring[-1]
            assert len({tuple(p) for p in ring[:-1]}) >= 3
            assert abs(sum(a[0]*b[1] - b[0]*a[1] for a, b in zip(ring, ring[1:]))) > 1e-12
            assert all(v == round(v, 4) for p in ring for v in p)


def inside_ring(point, ring):
    x, y = point
    inside = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:]):
        if (ay > y) != (by > y) and x < (bx-ax) * (y-ay) / (by-ay) + ax:
            inside = not inside
    return inside


def test_firenze_remains_inside_correct_zone():
    original = json.loads((FIXTURES / "dpc_20261007_1424_today.json").read_text(encoding="utf-8"))
    simplified = simplify_zones(original)
    containing = []
    for feature in simplified["features"]:
        geometry = feature["geometry"]
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        if any(inside_ring((11.2558, 43.7696), polygon[0]) and
               not any(inside_ring((11.2558, 43.7696), hole) for hole in polygon[1:])
               for polygon in polygons):
            containing.append(feature["properties"]["nome"])
    assert containing == ["Arno-Firenze"]


def test_fingerprint_ignores_alerts_comuni_and_feature_order(dpc_pair):
    first, second = dpc_pair
    second["features"].reverse()
    second["features"][0]["properties"]["Comuni"] = []
    second["features"][0]["properties"]["Per rischio temporali"] = "ALLERTA ROSSA"
    assert geometry_fingerprint(first) == geometry_fingerprint(second)
    second["features"][0]["geometry"]["coordinates"][0][0][0] += 0.001
    assert geometry_fingerprint(first) != geometry_fingerprint(second)


def test_tiny_islands_holes_and_oversize_failure():
    outer = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    hole = [[0.1, 0.1], [0.1, 0.2], [0.2, 0.2], [0.2, 0.1], [0.1, 0.1]]
    island = [[2, 2], [2.001, 2], [2, 2.001], [2, 2]]
    data = {"type": "FeatureCollection", "features": [{"properties": {"Nome zona": "Test"},
            "geometry": {"type": "MultiPolygon", "coordinates": [[outer, hole], [island]]}}]}
    result = simplify_zones(data)
    assert len(rings(result["features"][0]["geometry"])) == 3
    assert all(len(r) >= 4 for r in rings(result["features"][0]["geometry"]))
    with pytest.raises(ValueError, match="limite"):
        simplify_zones(data, max_bytes=1)


def geometry_collection(kind, coordinates):
    return {"type": "FeatureCollection", "features": [{
        "properties": {"Nome zona": "Test"},
        "geometry": {"type": kind, "coordinates": coordinates},
    }]}


def test_real_carso_discards_slivers_and_keeps_trieste():
    original = json.loads((FIXTURES / "dpc_20261025_1326_today_carso.json").read_text(encoding="utf-8"))
    before = deepcopy(original)
    result = simplify_zones(original)
    assert original == before
    assert len(result["features"]) == 1
    feature = result["features"][0]
    assert feature["properties"] == {
        "nome": "Bacino di Levante / Carso", "scartate": {"parti": 34, "buchi": 0},
    }
    polygons = feature["geometry"]["coordinates"]
    assert feature["geometry"]["type"] == "MultiPolygon"
    assert len(polygons) == 6
    assert len(json_bytes(result)) <= 700_000
    assert any(inside_ring((13.7768, 45.6495), polygon[0]) and
               not any(inside_ring((13.7768, 45.6495), hole) for hole in polygon[1:])
               for polygon in polygons)
    for ring in rings(feature["geometry"]):
        assert ring[0] == ring[-1] and len({tuple(p) for p in ring[:-1]}) >= 3
        assert all(v == round(v, 4) for p in ring for v in p)


@pytest.mark.parametrize("kind", ["Polygon", "MultiPolygon"])
def test_unrepresentable_holes_are_discarded_and_counted(kind):
    outer = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    hole = [[0.1, 0.1], [0.1, 0.2], [0.2, 0.2], [0.1, 0.1]]
    collapsed = [[0.3, 0.3], [0.30001, 0.3], [0.3, 0.30001], [0.3, 0.3]]
    polygon = [outer, collapsed, hole, collapsed]
    data = geometry_collection(kind, polygon if kind == "Polygon" else [polygon])
    feature = simplify_zones(data)["features"][0]
    assert feature["properties"]["scartate"] == {"parti": 0, "buchi": 2}
    assert rings(feature["geometry"]) == [outer, hole]


def test_multipolygon_discards_small_and_unrepresentable_parts():
    outer = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    small = [[2, 2], [2.0001, 2], [2, 2.0001], [2, 2]]
    # Area > 1e-7, ma tutti i vertici hanno la stessa ordinata a quattro decimali.
    collapsed = [[2, 2], [2.1, 2], [2.1, 2.00001], [2, 2]]
    data = geometry_collection("MultiPolygon", [[small], [outer], [collapsed]])
    feature = simplify_zones(data)["features"][0]
    assert feature["properties"]["scartate"] == {"parti": 2, "buchi": 0}
    assert feature["geometry"] == {"type": "MultiPolygon", "coordinates": [[outer]]}


@pytest.mark.parametrize("kind", ["Polygon", "MultiPolygon"])
@pytest.mark.parametrize("collapsed", [
    [[2, 2], [2.00001, 2], [2, 2.00001], [2, 2]],
    [[2, 2], [2.1, 2], [2.1, 2.00001], [2, 2]],
])
def test_zone_without_representable_exterior_fails_closed(kind, collapsed):
    coordinates = [collapsed] if kind == "Polygon" else [[collapsed]]
    data = geometry_collection(kind, coordinates)
    with pytest.raises(ValueError):
        simplify_zones(data)


def test_multipolygon_with_only_small_representable_parts_fails_closed():
    small = [[2, 2], [2.0001, 2], [2, 2.0001], [2, 2]]
    with pytest.raises(ValueError):
        simplify_zones(geometry_collection("MultiPolygon", [[small]]))


def test_single_small_representable_polygon_is_kept():
    small = [[2, 2], [2.0001, 2], [2, 2.0001], [2, 2]]
    feature = simplify_zones(geometry_collection("Polygon", [small]))["features"][0]
    assert feature["properties"] == {"nome": "Test"}
    assert feature["geometry"] == {"type": "Polygon", "coordinates": [small]}


@pytest.mark.parametrize("ring", [
    [[2, 2], [3, 2], [3, 3], [2, 3]],  # non chiuso
    [[2, 2], [float("nan"), 2], [3, 3], [2, 2]],
])
def test_malformed_parts_are_not_silently_discarded(ring):
    outer = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
    with pytest.raises(ValueError):
        simplify_zones(geometry_collection("MultiPolygon", [[outer], [ring]]))


@pytest.mark.parametrize("fixture", ["dpc_20261025_1326_today.json", "dpc_20261007_1424_today.json"])
def test_regions_on_real_alert_zones(fixture):
    original = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    before = deepcopy(original)
    by_name = {z["nome"]: z["regione"] for z in reduce_alerts(original, original, "20261025_1326")["zone"]}
    assert original == before
    assert by_name["Bacino dell'Aterno"] == "Abruzzo"
    if "Arno-Firenze" in by_name:
        assert by_name["Arno-Firenze"] == "Toscana"
        for name in ("Versante Jonico Centro-settentrionale", "Versante Jionico Centro-meridionale", "Versante Jonico Meridionale"):
            assert by_name[name] == "Calabria"
    else:
        assert by_name["Pianura centrale"] == "Lombardia"
        assert by_name["Pianura ferrarese"] == "Emilia-Romagna"


@pytest.mark.parametrize("comune", ["  CITTÀ   SANT’ ANGELO ", "citta sant\'angelo", "L‘ AQUILA", "l´aquila", "L`AQUILA"])
def test_region_normalizes_case_accents_apostrophes_and_spaces(dpc_pair, comune):
    for day in dpc_pair:
        day["features"][0]["properties"]["Comuni"] = [comune]
    assert reduce_alerts(*dpc_pair, "20261025_1326")["zone"][0]["regione"] == "Abruzzo"


@pytest.mark.parametrize("comuni,expected,percentage", [
    (["Firenze", "non esiste", "altro sconosciuto"], None, "33.3%"),
    (["Firenze", "non esiste"], "Toscana", None),
    ([], None, "0.0%"),
    (["Firenze", "Roma", "Fiesole"], "Toscana", None),
    (["Firenze", "Roma"], None, "100.0%"),
])
def test_region_coverage_and_majority(comuni, expected, percentage, caplog):
    from pipeline.allerte import load_comuni_regioni, zone_region
    assert zone_region("zona prova", comuni, load_comuni_regioni()) == expected
    if percentage:
        assert "zona prova" in caplog.text and percentage in caplog.text
    else:
        assert not caplog.records


def test_bilingual_istat_aliases_and_ambiguous_names(tmp_path, monkeypatch):
    import pipeline.allerte as module
    csv_file = tmp_path / "comuni.csv"
    csv_file.write_text("# ISTAT CC BY 4.0\ncomune,regione\nBolzano/Bozen,Trentino-Alto Adige\nOmonimo,Toscana\nOmonimo,Lazio\n", encoding="utf-8")
    monkeypatch.setattr(module, "COMUNI_REGIONI", csv_file)
    module.load_comuni_regioni.cache_clear()
    try:
        regions = module.load_comuni_regioni()
        for name in ("Bolzano", "Bozen", "Bolzano/Bozen"):
            assert module.zone_region("zona bilingue", [name], regions) == "Trentino-Alto Adige"
        assert "omonimo" not in regions
    finally:
        module.load_comuni_regioni.cache_clear()


@pytest.mark.parametrize("comune,expected", [
    ("Bolzano/Bozen", "Trentino-Alto Adige/Südtirol"),
    ("Bressanone/Brixen", "Trentino-Alto Adige/Südtirol"),
    ("Appiano sulla strada del vino/Eppan an der Weinstrasse", "Trentino-Alto Adige/Südtirol"),
    ("Bozen / Bolzano", "Trentino-Alto Adige/Südtirol"),
    ("Duino Aurisina-Devin-Nabrežina", "Friuli-Venezia Giulia"),
    ("Duino Aurisina - Devin-Nabrežina", "Friuli-Venezia Giulia"),
    ("Monrupino-Repentabor", "Friuli-Venezia Giulia"),
    ("Sgonico-Zgonik", "Friuli-Venezia Giulia"),
    ("San Dorligo della Valle-Dolina", "Friuli-Venezia Giulia"),
    ("Doberdò del Lago-Doberdob", "Friuli-Venezia Giulia"),
    ("Bellaria-Igea Marina - traduzione", "Emilia-Romagna"),
])
def test_dpc_bilingual_names_resolve_individually(comune, expected):
    assert zone_region("zona bilingue", [comune], load_comuni_regioni()) == expected


def test_bolzano_bilingual_list_in_reduced_alerts(dpc_pair):
    for day in dpc_pair:
        day["features"][0]["properties"].update({
            "Nome zona": "Provincia Autonoma di Bolzano",
            "Comuni": ["Bolzano/Bozen", "Bressanone/Brixen",
                       "Appiano sulla strada del vino/Eppan an der Weinstrasse", "Bolzano/Bozen"],
        })
    zones = reduce_alerts(*dpc_pair, "20261007_1424")["zone"]
    assert next(z for z in zones if z["nome"] == "Provincia Autonoma di Bolzano")["regione"] == "Trentino-Alto Adige/Südtirol"


def test_real_carso_bilingual_list_with_duplicates():
    original = json.loads((FIXTURES / "dpc_20261025_1326_today_carso.json").read_text(encoding="utf-8"))
    comuni = original["features"][0]["properties"]["Comuni"]
    comuni.extend(comuni[:])
    assert reduce_alerts(original, original, "20261007_1424")["zone"][0]["regione"] == "Friuli-Venezia Giulia"


@pytest.mark.parametrize("comune", ["Bellaria-Igea Marina", "Bellaria-Igea Marina/traduzione"])
def test_known_hyphenated_istat_name_precedes_prefix(comune):
    regions = {**load_comuni_regioni(), "bellaria": "Lazio"}
    assert regions["bellaria-igea marina"] == "Emilia-Romagna"
    assert zone_region("nome intero", [comune], regions) == "Emilia-Romagna"


@pytest.mark.parametrize("comuni", [
    ["Firenze"] * 10 + ["Roma"],
    ["Bolzano/Bozen"] * 10 + ["Bolzano", "Roma"],
    ["Monrupino-Repentabor"] * 10 + ["Monrupino", "Roma"],
    ["Bolzano/Bozen"] * 10 + ["ignoto 1", "ignoto 2"],
])
def test_duplicate_comuni_do_not_bias_majority_or_coverage(comuni):
    assert zone_region("duplicati", comuni, load_comuni_regioni()) is None

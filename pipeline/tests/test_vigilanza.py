from copy import deepcopy
import json
import logging

import pytest

from pipeline.vigilanza import html_sections, reduce_phenomena, reduce_vigilanza
from pipeline.tests.conftest import FIXTURES


EXPECTED_PHENOMENA = {
    1: ("pioggia", "Piogge sparse o intermittenti"),
    5: ("temporali", "Rovesci o temporali isolati"),
    6: ("temporali", "Rovesci o temporali sparsi"),
    10: ("vento", "Venti forti"),
    13: ("vento", "Frequenti raffiche"),
    40: ("mare", "Mare molto mosso"),
    62: ("temperature", "Temperature basse o in sensibile calo"),
}


@pytest.mark.parametrize("stamp,count", [("20261025", 27), ("20261007", 43)])
def test_real_bulletins(stamp, count, vig_bulletin):
    inputs = vig_bulletin(stamp)
    before = deepcopy(inputs)
    result = reduce_vigilanza(*inputs)
    assert inputs == before
    assert result["data"] == f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    assert result["titolo"] == inputs[1]["name"]
    assert result["fonte"] == "Dipartimento della Protezione Civile"
    assert result["licenza"] == "CC BY 4.0"
    assert result["url_fonte"].endswith("/DPC-Bollettini-Vigilanza-Meteorologica")
    assert [day["giorno"] for day in result["giorni"]] == ["oggi", "domani", "dopodomani"]
    assert result["giorni"][2]["data"] == ("2026-10-27" if stamp == "20261025" else "2026-10-09")
    for day in result["giorni"]:
        assert [s["titolo"] for s in day["sezioni"]] == ["Precipitazioni", "Visibilità", "Temperature", "Venti", "Mari"]
        assert all(s["testo"] and "<" not in s["testo"] and "\xa0" not in s["testo"] for s in day["sezioni"])
    today = result["giorni"][0]
    assert len(today["fenomeni"]) == count
    original = next(iter(inputs[2]["oggi"]["objects"].values()))["geometries"]
    for point, feature in zip(today["fenomeni"], original, strict=True):
        p = feature["properties"]
        identifier = p["id_fenomeno"]
        category, name = EXPECTED_PHENOMENA[identifier]
        assert type(point["id"]) is int
        assert point == {
            "id": identifier, "categoria": category, "nome": name,
            "lat": round(p["lat"], 4), "lon": round(p["lon"], 4),
        }
    if stamp == "20261025":
        assert "Sicilia settentrionale" in today["sezioni"][0]["testo"]
        assert "forti nord-occidentali" in today["sezioni"][3]["testo"]
        assert "molto mossi" in today["sezioni"][4]["testo"]
    else:
        assert "Liguria" in today["sezioni"][0]["testo"]
        assert "Sardegna settentrionale" in today["sezioni"][0]["testo"]


def test_html_entities_nested_titles_and_blocks():
    assert html_sections('<p><b><span>Precipitazioni:</span></b>&nbsp;pioggia &amp; neve<br>in montagna.</p>'
                         '<p><strong>Venti</strong>: deboli&#32;ovunque.</p><script>secret()</script>') == [
        {"titolo": "Precipitazioni", "testo": "pioggia & neve in montagna."},
        {"titolo": "Venti", "testo": "deboli ovunque."},
    ]


@pytest.mark.parametrize("key", ["aftertomorrow", "after_tomorrow", "after-tomorrow"])
def test_third_day_aliases(key, vig_bulletin):
    stamp, index, topologies, style = vig_bulletin()
    index[key] = index.pop("aftertomorrow")
    assert reduce_vigilanza(stamp, index, topologies, style)["giorni"][2]["giorno"] == "dopodomani"


def test_style_labels_do_not_override_local_legend(vig_bulletin):
    stamp, index, topologies, _ = vig_bulletin()
    style = {"style_properties": [{"propertyKey": "id_fenomeno", "properties": [
        {"property": "icon", "values": [{"key": 5, "value": "url(5.png)"}]},
        {"property": "label", "values": [{"key": 1, "value": "Nome errato"}]},
    ]}]}
    points = reduce_vigilanza(stamp, index, topologies, style)["giorni"][0]["fenomeni"]
    assert points == reduce_vigilanza(stamp, index, topologies)["giorni"][0]["fenomeni"]
    assert points[0]["nome"] == "Piogge sparse o intermittenti"
    assert any(p["id"] == 5 and p["categoria"] == "temporali" for p in points)


@pytest.mark.parametrize("stamp", ["20261025", "20261007"])
@pytest.mark.parametrize("identifier", [999, "0999"])
def test_unknown_phenomenon_has_fallback_and_warning(stamp, identifier, vig_bulletin, caplog):
    inputs = vig_bulletin(stamp)
    properties = next(iter(inputs[2]["oggi"]["objects"].values()))["geometries"][0]["properties"]
    properties["id_fenomeno"] = identifier
    with caplog.at_level(logging.WARNING, logger="pipeline.vigilanza"):
        points = reduce_vigilanza(*inputs)["giorni"][0]["fenomeni"]
    assert points[0] == {
        "id": 999, "categoria": "altro", "nome": "Fenomeno 999",
        "lat": round(properties["lat"], 4), "lon": round(properties["lon"], 4),
    }
    assert type(points[0]["id"]) is int
    assert len(points) == (27 if stamp == "20261025" else 43)
    assert any(record.name == "pipeline.vigilanza" and record.levelno == logging.WARNING
               and "999" in record.getMessage() for record in caplog.records)


def test_numeric_string_id_uses_local_legend(vig_bulletin):
    inputs = vig_bulletin()
    next(iter(inputs[2]["oggi"]["objects"].values()))["geometries"][0]["properties"]["id_fenomeno"] = "005"
    point = reduce_vigilanza(*inputs)["giorni"][0]["fenomeni"][0]
    assert type(point["id"]) is int and point["id"] == 5
    assert (point["categoria"], point["nome"]) == EXPECTED_PHENOMENA[5]


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), 91, "45", True])
def test_invalid_coordinates_rejected(coordinate, vig_bulletin):
    inputs = vig_bulletin()
    next(iter(inputs[2]["oggi"]["objects"].values()))["geometries"][0]["properties"]["lat"] = coordinate
    with pytest.raises(ValueError, match="coordinate"):
        reduce_vigilanza(*inputs)


def test_polygon_topology_not_silently_used_as_points():
    topology = json.loads((FIXTURES / "vig_20261025_oggi.topo.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="Point"):
        reduce_phenomena(topology)


@pytest.mark.parametrize("html", [None, "", "<p>nessun titolo</p>", "<b>Mari:</b>"])
def test_missing_sections_rejected(html):
    with pytest.raises(ValueError):
        html_sections(html)

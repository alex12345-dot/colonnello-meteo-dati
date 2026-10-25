from copy import deepcopy
import json

import pytest

from pipeline.vigilanza import html_sections, reduce_phenomena, reduce_vigilanza
from pipeline.tests.conftest import FIXTURES


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
        assert point == {"tipo": f"fenomeno {p['id_fenomeno']}", "lat": round(p["lat"], 4), "lon": round(p["lon"], 4)}
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


def test_legend_labels_and_unmapped_icons(vig_bulletin):
    stamp, index, topologies, _ = vig_bulletin()
    style = {"style_properties": [{"propertyKey": "id_fenomeno", "properties": [
        {"property": "icon", "values": [{"key": 5, "value": "url(5.png)"}]},
        {"property": "label", "values": [{"key": 1, "value": "Pioggia debole"}]},
    ]}]}
    points = reduce_vigilanza(stamp, index, topologies, style)["giorni"][0]["fenomeni"]
    assert points[0]["tipo"] == "Pioggia debole"
    assert any(p["tipo"] == "fenomeno 5" for p in points)


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), 91, "45", True])
def test_invalid_coordinates_rejected(coordinate, vig_bulletin):
    inputs = vig_bulletin()
    next(iter(inputs[2]["oggi"]["objects"].values()))["geometries"][0]["properties"]["lat"] = coordinate
    with pytest.raises(ValueError, match="coordinate"):
        reduce_vigilanza(*inputs)


def test_polygon_topology_not_silently_used_as_points():
    topology = json.loads((FIXTURES / "vig_20261025_oggi.topo.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="Point"):
        reduce_phenomena(topology, {})


@pytest.mark.parametrize("html", [None, "", "<p>nessun titolo</p>", "<b>Mari:</b>"])
def test_missing_sections_rejected(html):
    with pytest.raises(ValueError):
        html_sections(html)

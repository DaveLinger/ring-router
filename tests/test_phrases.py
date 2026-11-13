import tomllib
from pathlib import Path

import pytest

from ringrouter.router import route

CASES = tomllib.loads((Path(__file__).parent / "phrases.toml").read_text())["case"]
CHECKED = ("recipient", "tier", "heard", "kind", "fallback", "rewrite", "verb", "model", "host", "list", "scene", "rule",
           "action", "confirm", "location_trigger", "pronoun")


@pytest.mark.parametrize("case", CASES, ids=[c["say"] for c in CASES])
def test_phrase_routes(case, cfg):
    r = route(case["say"], cfg)
    assert r.route == case["route"], r.describe()
    if "extracted" in case:
        assert r.body == case["extracted"], r.describe()
    for key in CHECKED:
        if key == "verb" and key in case:
            assert r.verb == case[key], r.describe()
        elif key in case:
            assert r.params.get(key) == case[key], f"{key}: {r.describe()}"

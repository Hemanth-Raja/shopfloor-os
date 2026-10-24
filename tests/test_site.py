"""Static replay page: builds offline from the committed examples into one self-contained file."""

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_site_embeds_every_example_and_loads_nothing_external(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OUT", tmp_path / "index.html")
    html = mod.build().read_text(encoding="utf-8")
    runs = json.loads(re.search(r"const RUNS=(.*?), EVAL=", html, re.S).group(1))
    assert len(runs) == len(list(ROOT.glob("output_examples/example_*.json"))) and all(r["plan"] for r in runs)
    assert "http://" not in html and "https://" not in html and "</script>" in html

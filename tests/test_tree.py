"""Kör linjeträdets layout (static/tree.js) i Node mot en syntetisk förgrening."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

TREE = Path(__file__).resolve().parent.parent / "static" / "tree.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node saknas")
def test_branching_layout():
    lines = [
        {"line": "17", "stations": ["A", "B", "C", "D", "S1", "S2"], "lats": [5, 4, 3, 2, 1, 0]},
        {"line": "19", "stations": ["A", "B", "C", "H1", "H2", "H3"], "lats": [5, 4, 3, 2, 1, 0]},
        {"line": "14", "stations": ["M1", "M2", "B", "C"], "lats": [9, 8, 4, 3]},
    ]
    js = f"""
      const T = require({json.dumps(str(TREE))});
      const t = T.buildTree({json.dumps(lines)});
      const out = {{}};
      for (const n of t.nodes.values()) out[n.name] = [n.col, n.row, n.label, [...n.ends]];
      console.log(JSON.stringify({{nodes: out, cols: t.cols, edges: t.edges.length}}));
    """
    res = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    n = res["nodes"]
    assert len(n) == 11 and res["edges"] == 10
    assert n["M1"][1] < n["B"][1]                       # grenen norrut ritas ovanför ankaret
    assert n["S2"][1] > n["C"][1] and n["H3"][1] > n["C"][1]   # grenar söderut nedanför
    assert n["S1"][0] != n["H1"][0]                     # parallella grenar i olika kolumner
    assert n["S2"][3] == ["17"] and n["H3"][3] == ["19"]
    assert all(v[2] for k, v in n.items() if v[3])      # alla ändstationer har namn

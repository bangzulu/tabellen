/* Linjeträd: alla linjer i ett trafikslag i en gemensam schematisk karta med förgreningar.

   Layout i ett rutnät (kolumn, rad), norr överst:
   1. Längsta linjen blir stam i kolumn 0.
   2. Övriga linjer gås igenom, längsta först. Varje sammanhängande sträcka som inte redan
      är utritad blir en gren i närmaste lediga kolumn (växelvis höger/vänster om ankaret).
      - Gren i slutet/början av en linje: ritas uppåt eller nedåt från ankarstationen
        beroende på om grenen ligger norr eller söder om den (latitud).
      - Gren mellan två utritade stationer: fördelas jämnt mellan deras rader.
   3. Raderna förskjuts så att översta raden är 0.

   Etiketter: till höger om stationen om det är fritt, annars till vänster, annars utelämnas
   namnet (det finns kvar som verktygstips). Ändstationer får linjenummer.
*/
(function (root) {
  "use strict";

  /* Prövar alla val av stam, ordning och grensida och väljer den layout där flest
     stationsnamn får plats (ändstationer och knutpunkter väger tyngst), sedan färst kolumner. */
  function buildTree(lines) {
    if (!lines.length) return null;
    let best = null;
    const byLen = [...lines].sort((x, y) => y.stations.length - x.stations.length);
    for (let t = 0; t < lines.length; t++) {
      for (const longestFirst of [true, false]) {
        for (const side of ["alternate", "outward"]) {
          const rest = byLen.filter((l) => l !== byLen[t]);
          if (!longestFirst) rest.reverse();
          const tree = layout([byLen[t], ...rest], side);
          tree.score = score(tree);
          if (!best || tree.score < best.score) best = tree;
        }
      }
    }
    return best;
  }

  function score(tree) {
    let s = tree.cols * 4;
    for (const n of tree.nodes.values()) {
      if (n.label) continue;
      const important = n.ends.size > 0 || n.degree > 2;
      s += important ? 40 : 10;
    }
    return s;
  }

  function layout(ordered, side) {
    const nodes = new Map();          // namn -> {name, col, row, lat, ends:Set(linjer), lines:Set}
    const edges = new Map();          // "a|b" -> {a, b}

    const latOf = (line, i) => (line.lats && line.lats[i] != null ? line.lats[i] : null);
    const occupied = (col, r1, r2) => {
      const lo = Math.min(r1, r2) - 0.5, hi = Math.max(r1, r2) + 0.5;
      for (const n of nodes.values()) if (n.col === col && n.row > lo && n.row < hi) return true;
      return false;
    };
    const pickCol = (anchorCol, r1, r2) => {
      // "alternate": närmast ankaret, växelvis utåt/inåt. "outward": bara utåt från stammen först.
      const out = anchorCol >= 0 ? 1 : -1;
      const order = [];
      if (side === "outward") {
        for (let d = 1; d < 12; d++) order.push(anchorCol + out * d);
        for (let d = 1; d < 12; d++) order.push(anchorCol - out * d);
      } else {
        for (let d = 1; d < 12; d++) order.push(anchorCol + out * d, anchorCol - out * d);
      }
      for (const c of order) if (!occupied(c, r1, r2)) return c;
      return anchorCol + out * 12;
    };
    const add = (name, col, row, lat) => {
      if (!nodes.has(name)) nodes.set(name, { name, col, row, lat, ends: new Set(), lines: new Set() });
      return nodes.get(name);
    };

    // 1. Stam
    const trunk = ordered[0];
    trunk.stations.forEach((s, i) => add(s, 0, i, latOf(trunk, i)));

    // 2. Grenar
    for (const line of ordered.slice(1)) {
      const st = line.stations;
      let i = 0;
      while (i < st.length) {
        if (nodes.has(st[i])) { i++; continue; }
        let j = i;
        while (j < st.length && !nodes.has(st[j])) j++;
        const run = st.slice(i, j);                     // ej utritade stationer i följd
        const a = i > 0 ? nodes.get(st[i - 1]) : null;  // ankare före
        const b = j < st.length ? nodes.get(st[j]) : null;  // ankare efter
        let rows;
        if (a && b) {
          const step = (b.row - a.row) / (run.length + 1);
          rows = run.map((_, k) => a.row + step * (k + 1));
          if (Math.abs(b.row - a.row) <= 1) rows = run.map((_, k) => a.row + (k + 1) * Math.sign(b.row - a.row || 1));
        } else if (a) {
          const la = a.lat, lr = latOf(line, j - 1);
          const dir = la != null && lr != null ? (lr > la ? -1 : 1) : 1;      // söder om ankaret -> nedåt
          rows = run.map((_, k) => a.row + dir * (k + 1));
        } else if (b) {
          const lb = b.lat, lr = latOf(line, i);
          const dir = lb != null && lr != null ? (lr > lb ? -1 : 1) : -1;     // norr om ankaret -> uppåt
          rows = run.map((_, k) => b.row + dir * (run.length - k));
        } else {
          rows = run.map((_, k) => k);  // helt fristående linje (förekommer inte i SL-data)
        }
        const anchorCol = (a || b) ? (a || b).col : 0;
        const col = pickCol(anchorCol, Math.min(...rows), Math.max(...rows));
        run.forEach((s, k) => add(s, col, rows[k], latOf(line, i + k)));
        i = j;
      }
    }

    // Kanter, linjer per station, ändstationer
    for (const line of ordered) {
      const st = line.stations;
      st.forEach((s) => nodes.get(s).lines.add(line.line));
      nodes.get(st[0]).ends.add(line.line);
      nodes.get(st[st.length - 1]).ends.add(line.line);
      for (let k = 0; k + 1 < st.length; k++) {
        const key = [st[k], st[k + 1]].sort().join("|");
        if (!edges.has(key)) edges.set(key, { a: nodes.get(st[k]), b: nodes.get(st[k + 1]) });
      }
    }
    for (const n of nodes.values()) n.degree = 0;
    for (const e of edges.values()) { e.a.degree++; e.b.degree++; }

    // 3. Normalisera rader och kolumner
    let minRow = Infinity, maxRow = -Infinity, minCol = Infinity, maxCol = -Infinity;
    for (const n of nodes.values()) {
      minRow = Math.min(minRow, n.row); maxRow = Math.max(maxRow, n.row);
      minCol = Math.min(minCol, n.col); maxCol = Math.max(maxCol, n.col);
    }
    for (const n of nodes.values()) { n.row -= minRow; n.col -= minCol; }

    const tree = {
      nodes, edges: [...edges.values()],
      rows: maxRow - minRow + 1, cols: maxCol - minCol + 1,
      lineCount: ordered.length,
    };
    placeLabels(tree);
    return tree;
  }

  /* Etikettsida per station: "right", "left" eller null. Blockerad om en annan station eller
     en kant finns på den sidan inom samma radband. */
  function placeLabels(tree) {
    const band = 0.45;
    const blocked = (n, side) => {
      const lo = n.row - band, hi = n.row + band;
      const beyond = (c) => (side === "right" ? c > n.col : c < n.col);
      for (const m of tree.nodes.values()) {
        if (m !== n && beyond(m.col) && m.row > lo && m.row < hi) return true;
      }
      for (const e of tree.edges) {
        const r1 = Math.min(e.a.row, e.b.row), r2 = Math.max(e.a.row, e.b.row);
        if (r2 < lo || r1 > hi) continue;
        if (beyond(e.a.col) || beyond(e.b.col)) return true;
      }
      return false;
    };
    for (const n of tree.nodes.values()) {
      n.label = !blocked(n, "right") ? "right" : !blocked(n, "left") ? "left" : null;
    }
  }

  /* Punkt längs kanten a->b vid andel f (0–1). Kanter mellan kolumner ritas som S-kurvor. */
  function pointOnEdge(a, b, f, X, Y) {
    const x1 = X(a.col), y1 = Y(a.row), x2 = X(b.col), y2 = Y(b.row);
    if (a.col === b.col) return { x: x1, y: y1 + (y2 - y1) * f };
    const ym = (y1 + y2) / 2, t = f, u = 1 - t;
    // kubisk bezier P0=(x1,y1) P1=(x1,ym) P2=(x2,ym) P3=(x2,y2)
    return {
      x: u * u * u * x1 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x2,
      y: u * u * u * y1 + 3 * u * u * t * ym + 3 * u * t * t * ym + t * t * t * y2,
    };
  }

  function edgePath(a, b, X, Y) {
    const x1 = X(a.col), y1 = Y(a.row), x2 = X(b.col), y2 = Y(b.row);
    if (a.col === b.col) return `M${x1} ${y1}L${x2} ${y2}`;
    const ym = (y1 + y2) / 2;
    return `M${x1} ${y1}C${x1} ${ym} ${x2} ${ym} ${x2} ${y2}`;
  }

  const api = { buildTree, pointOnEdge, edgePath };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LineTree = api;
})(typeof window !== "undefined" ? window : globalThis);

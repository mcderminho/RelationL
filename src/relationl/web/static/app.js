/* RelationL explorer.
 *
 * No framework and no build step on purpose: this ships inside a pip package
 * that people self-host, and it has to work offline from a single directory.
 *
 * The graph is drawn to a canvas rather than SVG so that a few hundred nodes
 * stay interactive, and the layout is Fruchterman-Reingold with a cooling
 * temperature, so it settles and stops rather than jittering forever.
 */

const api = {
  async get(path, params = {}) {
    const url = new URL(path, window.location.origin);
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") {
        url.searchParams.set(key, value);
      }
    }
    const response = await fetch(url);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `request failed (${response.status})`);
    }
    return response.json();
  },
};

import { createModelView } from "./model.js";

const el = (id) => document.getElementById(id);
const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

const state = {
  source: "",
  minCount: 1,
  confidentOnly: false,
  search: "",
  nodes: [],
  edges: [],
  byKey: new Map(),
  selectedTable: null,
  selectedEdge: null,
  pathEdgeIds: new Set(),
  pathTables: new Set(),
  routes: [],
  activeRoute: 0,
  view: "graph",
};

/* ------------------------------------------------------------ simulation -- */

const view = { x: 0, y: 0, scale: 1 };
//: Set once the user pans or zooms, after which the view is theirs to control
//: and nothing auto-refits it.
let userAdjusted = false;
const canvas = el("graph");
const stage = document.querySelector(".stage");
const ctx = canvas.getContext("2d");

/* The canvas is sized explicitly from its container in CSS pixels rather than
 * left to a percentage rule.  A canvas with no CSS size falls back to its
 * intrinsic 300x150, and every coordinate derived from it is then wrong. */
const viewport = { width: 300, height: 150 };
let frame = null;
let ideal = 90;
let temperature = 0;

//: How fast the layout cools. Lower settles sooner but can trap a tangle.
const COOLING = 0.975;

//: Iterations used to solve the layout before the first paint.
const SETTLE_STEPS = 300;

function layout(nodes, edges) {
  const count = nodes.length || 1;
  const radius = Math.min(viewport.width, viewport.height) * 0.34;

  nodes.forEach((node, index) => {
    // Seed on a circle rather than at random: the simulation untangles from a
    // ring far more reliably than from a cloud, and it is reproducible.
    const angle = (index / count) * Math.PI * 2;
    node.x = Math.cos(angle) * radius;
    node.y = Math.sin(angle) * radius;
    node.dx = 0;
    node.dy = 0;
    node.r = nodeRadius(node);
    node.degree = 0;
  });

  for (const edge of edges) {
    edge.a = state.byKey.get(edge.left);
    edge.b = state.byKey.get(edge.right);
    if (edge.a) edge.a.degree += 1;
    if (edge.b) edge.b.degree += 1;
  }

  // Fruchterman-Reingold's ideal edge length, derived from the area each node
  // gets to itself.  Deriving it keeps the layout sensibly scaled whether the
  // graph has 8 nodes or 400.
  const area = viewport.width * viewport.height;
  ideal = Math.sqrt(area / count) * 0.62;
  temperature = ideal * 1.6;
}

function nodeRadius(node) {
  return Math.max(5, Math.min(26, 5 + Math.sqrt(node.join_count || 1) * 2.1));
}

/* Fruchterman-Reingold: repulsion between every pair, attraction along edges,
 * and a cooling temperature that caps how far a node may move each step. The
 * cap is what stops the layout oscillating instead of settling. */
function tick() {
  const nodes = state.nodes;
  const k = ideal;

  for (const node of nodes) {
    node.dx = 0;
    node.dy = 0;
  }

  for (let i = 0; i < nodes.length; i += 1) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = nodes[j];
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let distance = Math.hypot(dx, dy);
      if (distance < 0.01) {
        dx = (Math.random() - 0.5) * 0.1;
        dy = (Math.random() - 0.5) * 0.1;
        distance = Math.hypot(dx, dy);
      }
      // Keep circles from overlapping, not just centres apart.
      const gap = Math.max(1, distance - (a.r + b.r) * 0.5);
      const force = (k * k) / gap;
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      a.dx += fx;
      a.dy += fy;
      b.dx -= fx;
      b.dy -= fy;
    }
  }

  for (const edge of state.edges) {
    const { a, b } = edge;
    if (!a || !b) continue;
    const dx = a.x - b.x;
    const dy = a.y - b.y;
    const distance = Math.hypot(dx, dy) || 0.01;
    const force = (distance * distance) / k;
    const fx = (dx / distance) * force;
    const fy = (dy / distance) * force;
    a.dx -= fx;
    a.dy -= fy;
    b.dx += fx;
    b.dy += fy;
  }

  for (const node of nodes) {
    if (node.pinned) continue;
    // Gravity, so disconnected components drift back instead of escaping.
    node.dx -= node.x * 0.09;
    node.dy -= node.y * 0.09;

    const displacement = Math.hypot(node.dx, node.dy) || 1;
    const capped = Math.min(displacement, temperature);
    node.x += (node.dx / displacement) * capped;
    node.y += (node.dy / displacement) * capped;
  }

  temperature = Math.max(0.35, temperature * COOLING);
}

function settle(iterations) {
  for (let i = 0; i < iterations; i += 1) tick();
  temperature = 0.35;
}

/* The initial layout is solved synchronously rather than animated into place.
 * Animating it would mean the first useful frame depends on requestAnimationFrame
 * running to convergence, which does not happen in a backgrounded tab; the graph
 * would sit tangled until the tab was focused.  Motion is kept for interaction,
 * where it actually communicates something. */
function solve() {
  settle(SETTLE_STEPS);
  fit();
  draw();
}

function run() {
  if (frame) cancelAnimationFrame(frame);
  if (prefersReducedMotion.matches) {
    settle(SETTLE_STEPS);
    draw();
    return;
  }
  const step = () => {
    for (let i = 0; i < 2; i += 1) tick();
    draw();
    frame = temperature > 0.6 ? requestAnimationFrame(step) : null;
  };
  frame = requestAnimationFrame(step);
}

function reheat(amount = 0.25) {
  temperature = Math.max(temperature, ideal * amount);
  if (!frame && !prefersReducedMotion.matches) run();
}

/* -------------------------------------------------------------- drawing -- */

function resize() {
  const ratio = window.devicePixelRatio || 1;
  const width = stage.clientWidth;
  const height = stage.clientHeight;
  if (!width || !height) return;
  viewport.width = width;
  viewport.height = height;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  draw();
}

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function draw() {
  const { width, height } = viewport;
  if (!width || !height) return;

  const accent = css("--accent");
  const line = css("--line-strong");
  const text = css("--text");
  const muted = css("--text-faint");
  const outline = css("--node-line");

  ctx.save();
  ctx.clearRect(0, 0, width, height);
  ctx.translate(width / 2 + view.x, height / 2 + view.y);
  ctx.scale(view.scale, view.scale);

  const hasPath = state.pathEdgeIds.size > 0;

  for (const edge of state.edges) {
    const { a, b } = edge;
    if (!a || !b) continue;
    const onPath = state.pathEdgeIds.has(edge.id);
    const isSelected = state.selectedEdge === edge.id;

    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.setLineDash(edge.ambiguous ? [4, 4] : []);
    if (onPath || isSelected) {
      ctx.strokeStyle = accent;
      ctx.lineWidth = 2.6 / view.scale + Math.min(1.6, edge.occurrence_count * 0.14);
      ctx.globalAlpha = 1;
    } else {
      ctx.strokeStyle = line;
      ctx.lineWidth = 1 / view.scale + Math.min(2.2, edge.occurrence_count * 0.16);
      ctx.globalAlpha = hasPath ? 0.22 : 0.75;
    }
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;

  const showLabels = view.scale > 0.55;
  for (const node of state.nodes) {
    const onPath = state.pathTables.has(node.table);
    const isSelected = state.selectedTable === node.table;
    const dimmed = hasPath && !onPath;

    ctx.globalAlpha = dimmed ? 0.3 : 1;
    ctx.beginPath();
    ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
    ctx.fillStyle = onPath || isSelected ? accent : css("--node-fill");
    ctx.fill();
    ctx.lineWidth = (isSelected ? 2.4 : 1.5) / view.scale;
    ctx.strokeStyle = onPath || isSelected ? accent : outline;
    ctx.stroke();

    if (showLabels) {
      // Off-path labels stay readable, just quieter: a dimmed circle with no
      // name is a table the reader cannot identify at all.
      ctx.globalAlpha = dimmed ? 0.45 : 1;
      ctx.font = `${11 / view.scale}px ui-monospace, SFMono-Regular, Menlo, monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = isSelected || onPath ? text : muted;
      ctx.fillText(node.name, node.x, node.y + node.r + 4 / view.scale);
    }
  }

  ctx.restore();
}

function fit() {
  if (!state.nodes.length) return;
  userAdjusted = false;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of state.nodes) {
    minX = Math.min(minX, node.x - node.r);
    minY = Math.min(minY, node.y - node.r);
    maxX = Math.max(maxX, node.x + node.r);
    maxY = Math.max(maxY, node.y + node.r);
  }
  const width = viewport.width;
  const height = viewport.height;
  const spanX = Math.max(1, maxX - minX);
  const spanY = Math.max(1, maxY - minY);
  // Generous padding: node labels are drawn below each circle and would
  // otherwise be clipped at the edges of the canvas.
  const pad = 190;
  view.scale = Math.max(0.15, Math.min(1.7, Math.min(width / (spanX + pad), height / (spanY + pad))));
  view.x = -((minX + maxX) / 2) * view.scale;
  view.y = -((minY + maxY) / 2) * view.scale;
  draw();
}

/* --------------------------------------------------------- interaction -- */

function toWorld(event) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left - viewport.width / 2 - view.x) / view.scale,
    y: (event.clientY - rect.top - viewport.height / 2 - view.y) / view.scale,
  };
}

function nodeAt(point) {
  let best = null;
  let bestDistance = Infinity;
  for (const node of state.nodes) {
    const dx = point.x - node.x;
    const dy = point.y - node.y;
    const distance = Math.sqrt(dx * dx + dy * dy);
    if (distance <= node.r + 6 && distance < bestDistance) {
      best = node;
      bestDistance = distance;
    }
  }
  return best;
}

function edgeAt(point) {
  let best = null;
  let bestDistance = 7 / view.scale;
  for (const edge of state.edges) {
    const { a, b } = edge;
    if (!a || !b) continue;
    const distance = pointToSegment(point, a, b);
    if (distance < bestDistance) {
      best = edge;
      bestDistance = distance;
    }
  }
  return best;
}

function pointToSegment(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lengthSq = dx * dx + dy * dy;
  if (lengthSq === 0) return Math.hypot(p.x - a.x, p.y - a.y);
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / lengthSq;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

let drag = null;

canvas.addEventListener("pointerdown", (event) => {
  canvas.setPointerCapture(event.pointerId);
  const point = toWorld(event);
  const node = nodeAt(point);
  drag = {
    node,
    moved: false,
    startX: event.clientX,
    startY: event.clientY,
    originX: view.x,
    originY: view.y,
  };
  if (node) node.pinned = true;
});

canvas.addEventListener("pointermove", (event) => {
  if (!drag) {
    const point = toWorld(event);
    canvas.style.cursor = nodeAt(point) || edgeAt(point) ? "pointer" : "grab";
    return;
  }
  const dx = event.clientX - drag.startX;
  const dy = event.clientY - drag.startY;
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;

  if (drag.node) {
    const point = toWorld(event);
    drag.node.x = point.x;
    drag.node.y = point.y;
    reheat(0.12);
  } else {
    view.x = drag.originX + dx;
    view.y = drag.originY + dy;
    userAdjusted = true;
  }
  draw();
});

canvas.addEventListener("pointerup", (event) => {
  if (!drag) return;
  const { node, moved } = drag;
  if (node) node.pinned = false;
  if (!moved) {
    const point = toWorld(event);
    const hitNode = nodeAt(point);
    if (hitNode) {
      selectTable(hitNode.table);
    } else {
      const hitEdge = edgeAt(point);
      if (hitEdge) selectEdge(hitEdge.id);
      else closeDetail();
    }
  }
  drag = null;
});

canvas.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const px = event.clientX - rect.left - rect.width / 2;
    const py = event.clientY - rect.top - rect.height / 2;
    const factor = Math.exp(-event.deltaY * 0.0014);
    const next = Math.max(0.12, Math.min(4, view.scale * factor));
    const ratio = next / view.scale;
    view.x = px - (px - view.x) * ratio;
    view.y = py - (py - view.y) * ratio;
    view.scale = next;
    userAdjusted = true;
    draw();
  },
  { passive: false }
);

el("refit").addEventListener("click", () => {
  if (state.view === "model") {
    modelView.fit();
    return;
  }
  userAdjusted = false;
  fit();
});

/* ------------------------------------------------------------ rendering -- */

function showState(name, message) {
  for (const id of ["state-loading", "state-empty", "state-error"]) {
    el(id).hidden = id !== `state-${name}`;
  }
  if (name === "error") el("error-message").textContent = message;
}

function hideStates() {
  for (const id of ["state-loading", "state-empty", "state-error"]) {
    el(id).hidden = true;
  }
}

function renderTableList(nodes) {
  const list = el("table-list");
  list.replaceChildren();

  if (!nodes.length) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = state.search
      ? `No table matches "${state.search}".`
      : "No tables found. Run a scan first.";
    list.append(note);
    return;
  }

  for (const node of nodes.slice(0, 200)) {
    const row = document.createElement("button");
    row.className = "row";
    row.type = "button";
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(state.selectedTable === node.table));
    row.title = node.table;

    const name = document.createElement("span");
    name.className = "row-name";
    const prefix = node.table.slice(0, node.table.length - node.name.length);
    if (prefix) {
      const dim = document.createElement("em");
      dim.textContent = prefix;
      name.append(dim);
    }
    name.append(node.name);

    const meta = document.createElement("span");
    meta.className = "row-meta";
    meta.textContent = `${node.degree} · ${node.file_count}f`;
    meta.title = `${node.degree} distinct joins, seen in ${node.file_count} file(s)`;

    row.append(name, meta);
    row.addEventListener("click", () => selectTable(node.table));
    list.append(row);
  }
}

/* --------------------------------------------------------- detail panel -- */

function closeDetail() {
  state.selectedTable = null;
  state.selectedEdge = null;
  modelView.setSelectedEdge(null);
  el("detail").hidden = true;
  el("layout").classList.remove("has-detail");
  renderTableList(state.nodes);
  draw();
}

function detailShell(title, subtitle) {
  const detail = el("detail");
  detail.replaceChildren();
  detail.hidden = false;
  el("layout").classList.add("has-detail");

  const head = document.createElement("div");
  head.className = "detail-head";

  const titleWrap = document.createElement("div");
  titleWrap.className = "detail-title";
  const heading = document.createElement("h2");
  heading.textContent = title;
  const sub = document.createElement("p");
  sub.textContent = subtitle;
  titleWrap.append(heading, sub);

  const close = document.createElement("button");
  close.className = "close";
  close.type = "button";
  close.setAttribute("aria-label", "Close details");
  close.textContent = "×";
  close.addEventListener("click", closeDetail);

  head.append(titleWrap, close);
  detail.append(head);
  return detail;
}

function factRow(entries) {
  const wrap = document.createElement("div");
  wrap.className = "facts";
  for (const [value, label] of entries) {
    const fact = document.createElement("div");
    fact.className = "fact";
    const strong = document.createElement("b");
    strong.textContent = value;
    const span = document.createElement("span");
    span.textContent = label;
    fact.append(strong, span);
    wrap.append(fact);
  }
  return wrap;
}

function group(title) {
  const section = document.createElement("section");
  section.className = "group";
  if (title) {
    const heading = document.createElement("h2");
    heading.textContent = title;
    section.append(heading);
  }
  return section;
}

async function selectTable(key) {
  state.selectedTable = key;
  state.selectedEdge = null;
  renderTableList(state.nodes);
  draw();

  const node = state.byKey.get(key);
  if (!node) return;

  // Fill the path inputs in order, so clicking two tables sets up a query.
  const from = el("path-from");
  const to = el("path-to");
  if (!from.value) from.value = key;
  else if (!to.value && from.value !== key) to.value = key;

  const detail = detailShell(node.name, node.table);
  detail.append(
    factRow([
      [String(node.degree), "distinct joins"],
      [String(node.join_count), "occurrences"],
      [String(node.file_count), "files"],
    ])
  );

  const joins = group("Joins");
  const neighbours = state.edges
    .filter((edge) => edge.left === key || edge.right === key)
    .sort((a, b) => b.occurrence_count - a.occurrence_count);

  if (!neighbours.length) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = "No joins for this table at the current filters.";
    joins.append(note);
  } else {
    const rows = document.createElement("div");
    rows.className = "rows";
    for (const edge of neighbours) {
      const other = edge.left === key ? edge.right : edge.left;
      const row = document.createElement("button");
      row.className = "row";
      row.type = "button";
      const name = document.createElement("span");
      name.className = "row-name";
      name.textContent = other;
      const meta = document.createElement("span");
      meta.className = "row-meta";
      meta.textContent = `${edge.join_type.split(" ")[0]} · ${edge.occurrence_count}x`;
      row.append(name, meta);
      row.addEventListener("click", () => selectEdge(edge.id));
      rows.append(row);
    }
    joins.append(rows);
  }
  detail.append(joins);
}

async function selectEdge(edgeId) {
  state.selectedEdge = edgeId;
  draw();
  modelView.setSelectedEdge(edgeId);

  let data;
  try {
    data = await api.get(`/api/edges/${edgeId}`);
  } catch (error) {
    const detail = detailShell("Join", "could not load");
    const note = document.createElement("p");
    note.className = "empty-note";
    note.textContent = error.message;
    detail.append(note);
    return;
  }

  const detail = detailShell(`${data.left}  ${data.right}`, `${data.join_type} join`);
  detail.append(
    factRow([
      [String(data.occurrence_count), "occurrences"],
      [String(data.file_count), "files"],
      [String(data.conditions.length), "predicates"],
    ])
  );

  const conditionGroup = group("Condition");
  if (data.ambiguous) {
    const tag = document.createElement("span");
    tag.className = "tag warn";
    tag.textContent = "candidate";
    tag.title =
      "A column in this condition could not be pinned to one table, so this join is a candidate rather than an established fact.";
    conditionGroup.append(tag, document.createElement("br"));
  }
  const predicate = document.createElement("div");
  predicate.className = "predicate";
  predicate.textContent = data.condition || "no condition recorded (cross or natural join)";
  conditionGroup.append(predicate);
  detail.append(conditionGroup);

  const sites = group(`Written in ${data.occurrences.length} place(s)`);
  for (const site of data.occurrences) {
    const row = document.createElement("div");
    row.className = "site";
    const path = document.createElement("div");
    path.className = "site-path";
    path.textContent = `${site.rel_path}:${site.line}`;
    const meta = document.createElement("div");
    meta.className = "site-meta";
    const bits = [site.language];
    if (site.branch) bits.push(site.branch);
    if (site.commit_sha) bits.push(site.commit_sha.slice(0, 8));
    meta.textContent = bits.join("  ");
    row.append(path, meta);
    sites.append(row);
  }
  detail.append(sites);
}

/* ------------------------------------------------------------ path find -- */

async function findPath() {
  const start = el("path-from").value.trim();
  const end = el("path-to").value.trim();
  const hint = el("path-hint");

  if (!start || !end) {
    hint.textContent = "Pick a table for both boxes.";
    return;
  }
  if (start === end) {
    hint.textContent = "Pick two different tables.";
    return;
  }

  hint.textContent = "Searching.";
  let data;
  try {
    data = await api.get("/api/path", {
      start,
      end,
      source: state.source,
      min_occurrences: state.minCount,
      include_ambiguous: !state.confidentOnly,
      k: 3,
    });
  } catch (error) {
    hint.textContent = error.message;
    return;
  }

  state.routes = data.routes;
  state.activeRoute = 0;

  if (!data.routes.length) {
    clearHighlight();
    hint.textContent =
      state.minCount > 1
        ? `No route at minimum ${state.minCount}. Try lowering it.`
        : "No route between those tables.";
    return;
  }

  hint.textContent = `${data.routes.length} route(s) found.`;
  highlightRoute(0);
  renderRoutes();
  writeUrl();
}

function highlightRoute(index) {
  const route = state.routes[index];
  if (!route) return;
  state.activeRoute = index;
  state.pathEdgeIds = new Set(route.edges.map((edge) => edge.id));
  state.pathTables = new Set(route.tables);
  draw();
}

function clearHighlight() {
  state.routes = [];
  state.pathEdgeIds = new Set();
  state.pathTables = new Set();
  draw();
}

function renderRoutes() {
  const detail = detailShell(
    "Join path",
    `${el("path-from").value} to ${el("path-to").value}`
  );

  state.routes.forEach((route, index) => {
    const card = document.createElement("div");
    card.className = "route";
    card.setAttribute("aria-selected", String(index === state.activeRoute));

    const head = document.createElement("div");
    head.className = "route-head";
    const hops = document.createElement("span");
    hops.innerHTML = `<b>${route.length}</b> hop${route.length === 1 ? "" : "s"}`;
    const weakest = document.createElement("span");
    weakest.textContent = `weakest link ${route.weakest_link}x`;
    head.append(hops, weakest);
    card.append(head);

    route.tables.forEach((table, position) => {
      const hop = document.createElement("div");
      hop.className = "hop";
      const name = document.createElement("div");
      name.className = "hop-table";
      name.textContent = table;
      hop.append(name);

      const edge = route.edges[position];
      if (edge) {
        const join = document.createElement("div");
        join.className = "hop-join";
        join.textContent = `${edge.join_type} · ${edge.occurrence_count}x`;
        const predicate = document.createElement("div");
        predicate.className = "predicate";
        predicate.textContent = edge.condition || "no condition recorded";
        hop.append(join, predicate);
      }
      card.append(hop);
    });

    card.addEventListener("click", () => {
      highlightRoute(index);
      renderRoutes();
    });
    detail.append(card);
  });
}

el("find-path").addEventListener("click", findPath);
el("clear-path").addEventListener("click", () => {
  el("path-from").value = "";
  el("path-to").value = "";
  el("path-hint").textContent = "Click a table in the graph to fill the next empty box.";
  clearHighlight();
  closeDetail();
  writeUrl();
});

/* ----------------------------------------------------------- view switch -- */

const modelView = createModelView({
  mount: el("model"),
  onSelectEdge: (edgeId) => selectEdge(edgeId),
  onSelectTable: (table) => selectTable(table),
});

let modelLoaded = false;

function setView(name) {
  state.view = name;
  const isGraph = name === "graph";
  el("view-graph").setAttribute("aria-pressed", String(isGraph));
  el("view-model").setAttribute("aria-pressed", String(!isGraph));
  el("graph").hidden = !isGraph;
  el("model").hidden = isGraph;
  document.querySelector(".legend").hidden = !isGraph;

  if (isGraph) {
    resize();
  } else if (!modelLoaded) {
    loadModel();
  } else {
    modelView.fit();
  }
  writeUrl();
}

async function loadModel() {
  showState("loading");
  try {
    const data = await api.get("/api/model", {
      source: state.source,
      min_occurrences: state.minCount,
      include_ambiguous: !state.confidentOnly,
      limit: 200,
    });
    modelLoaded = true;
    if (!data.tables.length) {
      showState("empty");
      return;
    }
    hideStates();
    modelView.render(data);
    modelView.setSelectedEdge(state.selectedEdge);
  } catch (error) {
    showState("error", error.message);
  }
}

el("view-graph").addEventListener("click", () => setView("graph"));
el("view-model").addEventListener("click", () => setView("model"));

/* ------------------------------------------------------------- deep link -- */

/* The URL carries the filters and the current path query, so a route between
 * two tables can be pasted into a ticket and reopened exactly as found. */

function writeUrl() {
  const params = new URLSearchParams();
  if (state.source) params.set("source", state.source);
  if (state.minCount > 1) params.set("min", String(state.minCount));
  if (state.confidentOnly) params.set("confident", "1");
  if (state.view !== "graph") params.set("view", state.view);
  const from = el("path-from").value.trim();
  const to = el("path-to").value.trim();
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  const query = params.toString();
  window.history.replaceState(null, "", query ? `?${query}` : window.location.pathname);
}

function readUrl() {
  const params = new URLSearchParams(window.location.search);
  state.source = params.get("source") || "";
  state.minCount = Math.max(1, Number(params.get("min") || 1));
  state.confidentOnly = params.get("confident") === "1";
  el("min-count").value = String(state.minCount);
  el("min-count-value").textContent = String(state.minCount);
  el("confident").checked = state.confidentOnly;
  el("path-from").value = params.get("from") || "";
  el("path-to").value = params.get("to") || "";
  state.view = params.get("view") === "model" ? "model" : "graph";
  return Boolean(params.get("from") && params.get("to"));
}

/* ----------------------------------------------------------------- load -- */

/* Both views read the same filters, so invalidate the model whenever they
 * change and reload it if it is the one on screen. */
function refresh() {
  modelLoaded = false;
  if (state.view === "model") {
    loadModel();
  } else {
    loadGraph();
  }
}

async function loadGraph({ relayout = true } = {}) {
  if (relayout) showState("loading");
  try {
    const data = await api.get("/api/graph", {
      source: state.source,
      min_occurrences: state.minCount,
      include_ambiguous: !state.confidentOnly,
      limit: 400,
    });

    state.nodes = data.nodes;
    state.edges = data.edges;
    state.byKey = new Map(data.nodes.map((node) => [node.table, node]));

    // Drop any highlight that no longer exists under the new filters.
    state.pathEdgeIds = new Set(
      [...state.pathEdgeIds].filter((id) => data.edges.some((edge) => edge.id === id))
    );
    if (!state.pathEdgeIds.size) state.pathTables = new Set();

    renderTableList(filteredNodes());

    if (!data.nodes.length) {
      showState("empty");
      return;
    }
    el("isolated-note").textContent = data.isolated
      ? `${data.isolated} table(s) hidden: no join at this threshold.`
      : "";
    hideStates();
    resize();
    layout(state.nodes, state.edges);
    solve();
  } catch (error) {
    showState("error", error.message);
  }
}

function filteredNodes() {
  if (!state.search) return state.nodes;
  const needle = state.search.toLowerCase();
  return state.nodes.filter((node) => node.table.includes(needle));
}

async function loadSummary() {
  try {
    const summary = await api.get("/api/summary");
    el("bar-stats").innerHTML = [
      `<span><b>${summary.nodes}</b> tables</span>`,
      `<span><b>${summary.edges}</b> joins</span>`,
      `<span><b>${summary.files}</b> files</span>`,
    ].join("");

    if (summary.sources.length > 1) {
      const select = el("source");
      for (const name of summary.sources) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.append(option);
      }
      el("source-field").hidden = false;
    }
  } catch (error) {
    showState("error", error.message);
  }
}

/* ------------------------------------------------------------- controls -- */

let searchTimer = null;
el("search").addEventListener("input", (event) => {
  state.search = event.target.value.trim().toLowerCase();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderTableList(filteredNodes()), 120);
});

el("min-count").addEventListener("input", (event) => {
  state.minCount = Number(event.target.value);
  el("min-count-value").textContent = String(state.minCount);
});

el("min-count").addEventListener("change", () => {
  writeUrl();
  refresh();
});

el("confident").addEventListener("change", (event) => {
  state.confidentOnly = event.target.checked;
  writeUrl();
  refresh();
});

el("source").addEventListener("change", (event) => {
  state.source = event.target.value;
  writeUrl();
  refresh();
});

/* Watch the stage, not the window: opening the detail panel narrows the canvas
 * without the window changing size, and the graph would otherwise be clipped. */
const stageObserver = new ResizeObserver(() => {
  resize();
  if (!frame && !userAdjusted) fit();
});
stageObserver.observe(stage);
prefersReducedMotion.addEventListener("change", () => run());

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDetail();
  if (event.key === "/" && document.activeElement !== el("search")) {
    event.preventDefault();
    el("search").focus();
  }
});

const hasPathQuery = readUrl();
const startInModel = state.view === "model";
state.view = "graph";
loadSummary()
  .then(() => loadGraph())
  .then(() => {
    if (hasPathQuery) findPath();
    if (startInModel) setView("model");
  });

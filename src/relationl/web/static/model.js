/* The semantic model view: tables as column lists, relationships drawn between
 * the specific columns they key on.
 *
 * Tables are HTML so the column lists stay selectable and accessible; the
 * relationships are one SVG layer sharing the same transformed space. A
 * relationship is a single trunk that fans out at each end to every column in
 * the join, so a composite key reads as one relationship on two columns rather
 * than as two separate lines.
 *
 * Routes are orthogonal, as in any modelling tool: horizontal stubs off the
 * column rows, a vertical gather, then one horizontal run across. Diagonals
 * are easier to draw but become unreadable as soon as several of them cross.
 *
 * Nothing here claims cardinality. RelationL reads code, not a schema, so there
 * is no basis for the 1/* markers a modelling tool shows; the join type and the
 * number of times it was written are shown instead.
 */

const CARD_WIDTH = 210;
const HEADER_HEIGHT = 32;
const ROW_HEIGHT = 19;
const MAX_ROWS = 14;
const STUB = 30;
//: Corner radius on the orthogonal routes.
const CORNER = 9;

//: Iterations of the box layout solved before the first paint.
const SETTLE_STEPS = 260;

export function createModelView({ mount, onSelectEdge, onSelectTable }) {
  const canvas = document.createElement("div");
  canvas.className = "model-canvas";

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "model-links");
  svg.innerHTML = `
    <defs>
      <marker id="rel-arrow" viewBox="0 0 10 10" refX="9" refY="5"
              markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" />
      </marker>
    </defs>
    <g class="link-layer"></g>`;

  canvas.append(svg);
  mount.append(canvas);

  const view = { x: 0, y: 0, scale: 1 };
  const boxes = new Map();
  let relationships = [];
  let selectedEdge = null;
  let hoveredEdge = null;
  let drag = null;

  /* ------------------------------------------------------------ layout -- */

  function measure(table) {
    const columns = table.columns || [];
    const shown = columns.slice(0, MAX_ROWS);
    const hidden = columns.length - shown.length;
    const rows = shown.length + (hidden > 0 ? 1 : 0);
    return {
      table: table.table,
      name: table.name,
      qualifier: table.table.slice(0, table.table.length - table.name.length),
      meta: table,
      columns: shown,
      hidden,
      index: new Map(shown.map((column, i) => [column.name, i])),
      w: CARD_WIDTH,
      h: HEADER_HEIGHT + Math.max(rows, 1) * ROW_HEIGHT,
      x: 0,
      y: 0,
    };
  }

  /* Fruchterman-Reingold over box centres.  Each box repels as a circle that
   * encloses it, which is what keeps cards from overlapping. */
  function solveLayout() {
    const list = [...boxes.values()];
    const count = list.length || 1;
    // Boxes already occupy a lot of space, so the ideal *gap* between their
    // edges is what matters; the repulsion term subtracts the enclosing radii.
    const ideal = CARD_WIDTH * 0.95;
    const radius = Math.max(240, Math.sqrt(count) * ideal * 0.6);

    list.forEach((box, index) => {
      const angle = (index / count) * Math.PI * 2;
      box.x = Math.cos(angle) * radius;
      box.y = Math.sin(angle) * radius;
      box.enclosing = Math.hypot(box.w, box.h) / 2;
    });

    let temperature = ideal * 1.6;
    for (let step = 0; step < SETTLE_STEPS; step += 1) {
      for (const box of list) {
        box.dx = 0;
        box.dy = 0;
      }

      for (let i = 0; i < list.length; i += 1) {
        const a = list[i];
        for (let j = i + 1; j < list.length; j += 1) {
          const b = list[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let distance = Math.hypot(dx, dy);
          if (distance < 0.01) {
            dx = (Math.random() - 0.5) * 0.1;
            dy = (Math.random() - 0.5) * 0.1;
            distance = Math.hypot(dx, dy);
          }
          const gap = Math.max(1, distance - (a.enclosing + b.enclosing));
          const force = (ideal * ideal) / gap;
          a.dx += (dx / distance) * force;
          a.dy += (dy / distance) * force;
          b.dx -= (dx / distance) * force;
          b.dy -= (dy / distance) * force;
        }
      }

      for (const relationship of relationships) {
        const a = boxes.get(relationship.left);
        const b = boxes.get(relationship.right);
        if (!a || !b) continue;
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distance = Math.hypot(dx, dy) || 0.01;
        const force = (distance * distance) / ideal;
        a.dx -= (dx / distance) * force;
        a.dy -= (dy / distance) * force;
        b.dx += (dx / distance) * force;
        b.dy += (dy / distance) * force;
      }

      for (const box of list) {
        box.dx -= box.x * 0.14;
        box.dy -= box.y * 0.14;
        const displacement = Math.hypot(box.dx, box.dy) || 1;
        const capped = Math.min(displacement, temperature);
        box.x += (box.dx / displacement) * capped;
        box.y += (box.dy / displacement) * capped;
      }
      temperature = Math.max(0.4, temperature * 0.975);
    }
  }

  /* ------------------------------------------------------------- render -- */

  function render(data) {
    boxes.clear();
    for (const table of data.tables || []) {
      boxes.set(table.table, measure(table));
    }
    relationships = (data.relationships || []).filter(
      (r) => boxes.has(r.left) && boxes.has(r.right)
    );

    canvas.querySelectorAll(".model-table").forEach((node) => node.remove());
    if (!boxes.size) {
      drawLinks();
      return;
    }

    solveLayout();
    for (const box of boxes.values()) {
      canvas.append(buildCard(box));
    }
    drawLinks();
    fit();
  }

  function buildCard(box) {
    const card = document.createElement("div");
    card.className = "model-table";
    card.dataset.table = box.table;
    card.style.width = `${box.w}px`;

    const header = document.createElement("button");
    header.type = "button";
    header.className = "model-head";
    header.title = `${box.table}\n${box.meta.join_count} join occurrence(s) in ${box.meta.file_count} file(s)`;
    if (box.qualifier) {
      const dim = document.createElement("em");
      dim.textContent = box.qualifier;
      header.append(dim);
    }
    header.append(box.name);
    header.addEventListener("click", (event) => {
      if (!drag || !drag.moved) onSelectTable(box.table);
      event.stopPropagation();
    });
    card.append(header);

    for (const column of box.columns) {
      const row = document.createElement("div");
      row.className = "model-column";
      row.dataset.column = column.name;
      if (column.is_join_key) row.classList.add("is-key");
      row.style.height = `${ROW_HEIGHT}px`;
      const label = document.createElement("span");
      label.textContent = column.name;
      row.append(label);
      card.append(row);
    }

    if (box.hidden > 0) {
      const more = document.createElement("div");
      more.className = "model-column model-more";
      more.style.height = `${ROW_HEIGHT}px`;
      more.textContent = `+${box.hidden} more`;
      more.title = "Columns not referenced by any visible join";
      card.append(more);
    }

    header.addEventListener("pointerdown", (event) => startCardDrag(event, box, card));
    return card;
  }

  function columnY(box, name) {
    const index = name === undefined ? undefined : box.index.get(name);
    if (index === undefined) return box.y + box.h / 2;
    return box.y + HEADER_HEIGHT + index * ROW_HEIGHT + ROW_HEIGHT / 2;
  }

  function drawLinks() {
    const layer = svg.querySelector(".link-layer");
    layer.replaceChildren();
    if (!boxes.size) {
      svg.setAttribute("viewBox", "0 0 1 1");
      return;
    }

    const bounds = contentBounds();
    svg.setAttribute(
      "viewBox",
      `${bounds.minX} ${bounds.minY} ${bounds.width} ${bounds.height}`
    );
    svg.style.left = `${bounds.minX}px`;
    svg.style.top = `${bounds.minY}px`;
    svg.style.width = `${bounds.width}px`;
    svg.style.height = `${bounds.height}px`;

    const active = hoveredEdge ?? selectedEdge;
    for (const relationship of relationships) {
      layer.append(buildLink(relationship, active));
    }
  }

  function buildLink(relationship, active) {
    const a = boxes.get(relationship.left);
    const b = boxes.get(relationship.right);
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute("class", "link");
    if (relationship.ambiguous) group.classList.add("is-candidate");
    if (active === relationship.id) group.classList.add("is-active");
    else if (active !== null && active !== undefined) group.classList.add("is-muted");

    const pairs = relationship.pairs || [];
    const aTargets = pairs.length
      ? pairs.map((pair) => columnY(a, pair.left_column))
      : [a.y + a.h / 2];
    const bTargets = pairs.length
      ? pairs.map((pair) => columnY(b, pair.right_column))
      : [b.y + b.h / 2];
    const aHub = average(aTargets);
    const bHub = average(bTargets);

    const route = chooseRoute(a, b, aHub, bHub);
    const { aEdge, bEdge, aJunction, bJunction, rail } = route;

    // Branches first, so the trunk and its arrowhead draw on top of them.
    for (const y of aTargets) group.append(branch(aEdge, y, aJunction, aHub));
    for (const y of bTargets) group.append(branch(bEdge, y, bJunction, bHub));

    const trunk = document.createElementNS("http://www.w3.org/2000/svg", "path");
    trunk.setAttribute(
      "d",
      elbow([
        [aJunction, aHub],
        [rail, aHub],
        [rail, bHub],
        [bJunction, bHub],
      ])
    );
    trunk.setAttribute("class", "link-trunk");
    trunk.setAttribute("marker-end", "url(#rel-arrow)");
    group.append(trunk);

    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("class", "link-label");
    label.setAttribute("x", String(rail));
    label.setAttribute("y", String((aHub + bHub) / 2 - 7));
    label.setAttribute("text-anchor", "middle");
    label.textContent =
      pairs.length > 1
        ? `${relationship.join_type} on ${pairs.length}`
        : relationship.join_type;
    group.append(label);

    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent =
      `${relationship.left} ${relationship.join_type} ${relationship.right}\n` +
      `${relationship.condition || "no condition recorded"}\n` +
      `written ${relationship.occurrence_count}x in ${relationship.file_count} file(s)` +
      (relationship.ambiguous ? "\ncandidate: a column could not be pinned to one table" : "");
    group.append(title);

    group.addEventListener("click", (event) => {
      event.stopPropagation();
      onSelectEdge(relationship.id);
    });
    group.addEventListener("pointerenter", () => {
      hoveredEdge = relationship.id;
      highlight(relationship, true);
      drawLinks();
    });
    group.addEventListener("pointerleave", () => {
      hoveredEdge = null;
      highlight(relationship, false);
      drawLinks();
    });
    return group;
  }

  /* Pick which edges the route leaves from, and where its vertical run sits.
   *
   * All four side combinations are costed as the orthogonal path they would
   * actually produce, and the cheapest wins.  Two cards stacked above one
   * another therefore leave from the same side and share a rail beside them,
   * rather than looping around to face each other. */
  function chooseRoute(a, b, aHub, bHub) {
    const options = [];
    for (const aRight of [true, false]) {
      for (const bRight of [true, false]) {
        const aEdge = aRight ? a.x + a.w : a.x;
        const bEdge = bRight ? b.x + b.w : b.x;
        const aDirection = aRight ? 1 : -1;
        const bDirection = bRight ? 1 : -1;
        const aJunction = aEdge + aDirection * STUB;
        const bJunction = bEdge + bDirection * STUB;

        // Leaving the same side means the rail has to clear *both* cards, not
        // just sit between the two junctions, or it would run behind one.
        const rail =
          aDirection === bDirection
            ? aDirection > 0
              ? Math.max(a.x + a.w, b.x + b.w) + STUB
              : Math.min(a.x, b.x) - STUB
            : (aJunction + bJunction) / 2;

        let cost =
          Math.abs(aJunction - rail) +
          Math.abs(aHub - bHub) +
          Math.abs(rail - bJunction);
        // A rail drawn over a card reads as a line vanishing behind it.
        if (overlapsCard(rail, a) || overlapsCard(rail, b)) cost += 600;
        // Prefer facing sides when they are genuinely side by side.
        if (aDirection === bDirection) cost += STUB;

        options.push({ aEdge, bEdge, aDirection, bDirection, aJunction, bJunction, rail, cost });
      }
    }
    options.sort((first, second) => first.cost - second.cost);
    return options[0];
  }

  function overlapsCard(x, box) {
    return x > box.x - 1 && x < box.x + box.w + 1;
  }

  function branch(edgeX, edgeY, junctionX, junctionY) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute(
      "d",
      elbow([
        [edgeX, edgeY],
        [junctionX, edgeY],
        [junctionX, junctionY],
      ])
    );
    path.setAttribute("class", "link-branch");
    return path;
  }

  /* Build an orthogonal path through `points`, rounding each corner.
   *
   * Relationships are routed as right angles rather than diagonals: it is what
   * every modelling tool does, and with several relationships on a board the
   * shared vertical runs stay readable where crossing diagonals would not.
   * Corners are eased with a quadratic through the vertex, which keeps the
   * runs strictly horizontal and vertical while taking the hard edge off. */
  function elbow(points) {
    const path = [`M ${round(points[0][0])} ${round(points[0][1])}`];

    for (let i = 1; i < points.length - 1; i += 1) {
      const [px, py] = points[i - 1];
      const [cx, cy] = points[i];
      const [nx, ny] = points[i + 1];

      // Never round by more than half a segment, or corners would overlap.
      const radius = Math.min(
        CORNER,
        Math.hypot(cx - px, cy - py) / 2,
        Math.hypot(nx - cx, ny - cy) / 2
      );
      if (radius < 0.5) {
        path.push(`L ${round(cx)} ${round(cy)}`);
        continue;
      }
      const into = towards([cx, cy], [px, py], radius);
      const outOf = towards([cx, cy], [nx, ny], radius);
      path.push(`L ${round(into[0])} ${round(into[1])}`);
      path.push(`Q ${round(cx)} ${round(cy)} ${round(outOf[0])} ${round(outOf[1])}`);
    }

    const last = points[points.length - 1];
    path.push(`L ${round(last[0])} ${round(last[1])}`);
    return path.join(" ");
  }

  function towards([x, y], [tx, ty], distance) {
    const length = Math.hypot(tx - x, ty - y) || 1;
    return [x + ((tx - x) / length) * distance, y + ((ty - y) / length) * distance];
  }

  function round(value) {
    return Math.round(value * 100) / 100;
  }

  function highlight(relationship, on) {
    for (const [tableKey, column] of [
      ...(relationship.pairs || []).map((p) => [relationship.left, p.left_column]),
      ...(relationship.pairs || []).map((p) => [relationship.right, p.right_column]),
    ]) {
      const card = canvas.querySelector(`.model-table[data-table="${cssEscape(tableKey)}"]`);
      const row = card?.querySelector(`.model-column[data-column="${cssEscape(column)}"]`);
      row?.classList.toggle("is-lit", on);
    }
  }

  function cssEscape(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : value;
  }

  /* --------------------------------------------------------- viewport -- */

  function contentBounds() {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const box of boxes.values()) {
      minX = Math.min(minX, box.x);
      minY = Math.min(minY, box.y);
      maxX = Math.max(maxX, box.x + box.w);
      maxY = Math.max(maxY, box.y + box.h);
    }
    const pad = STUB * 2 + 20;
    return {
      minX: minX - pad,
      minY: minY - pad,
      width: maxX - minX + pad * 2,
      height: maxY - minY + pad * 2,
    };
  }

  function place() {
    for (const box of boxes.values()) {
      const card = canvas.querySelector(`.model-table[data-table="${cssEscape(box.table)}"]`);
      if (card) {
        card.style.transform = `translate(${box.x}px, ${box.y}px)`;
      }
    }
  }

  function apply() {
    canvas.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  }

  function fit() {
    if (!boxes.size) return;
    const bounds = contentBounds();
    const width = mount.clientWidth || 1;
    const height = mount.clientHeight || 1;
    view.scale = Math.max(
      0.18,
      Math.min(1.25, Math.min(width / bounds.width, height / bounds.height))
    );
    view.x = width / 2 - (bounds.minX + bounds.width / 2) * view.scale;
    view.y = height / 2 - (bounds.minY + bounds.height / 2) * view.scale;
    place();
    apply();
  }

  /* ------------------------------------------------------ interaction -- */

  function startCardDrag(event, box, card) {
    event.stopPropagation();
    card.setPointerCapture(event.pointerId);
    drag = {
      kind: "card",
      box,
      card,
      moved: false,
      startX: event.clientX,
      startY: event.clientY,
      originX: box.x,
      originY: box.y,
    };
  }

  mount.addEventListener("pointerdown", (event) => {
    if (drag) return;
    drag = {
      kind: "pan",
      moved: false,
      startX: event.clientX,
      startY: event.clientY,
      originX: view.x,
      originY: view.y,
    };
  });

  mount.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;

    if (drag.kind === "card") {
      drag.box.x = drag.originX + dx / view.scale;
      drag.box.y = drag.originY + dy / view.scale;
      place();
      drawLinks();
    } else {
      view.x = drag.originX + dx;
      view.y = drag.originY + dy;
      apply();
    }
  });

  const endDrag = () => {
    drag = null;
  };
  mount.addEventListener("pointerup", endDrag);
  mount.addEventListener("pointercancel", endDrag);

  mount.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const rect = mount.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const factor = Math.exp(-event.deltaY * 0.0014);
      const next = Math.max(0.12, Math.min(2.5, view.scale * factor));
      const ratio = next / view.scale;
      view.x = px - (px - view.x) * ratio;
      view.y = py - (py - view.y) * ratio;
      view.scale = next;
      apply();
    },
    { passive: false }
  );

  return {
    render,
    fit,
    setSelectedEdge(edgeId) {
      selectedEdge = edgeId;
      drawLinks();
    },
    destroy() {
      canvas.remove();
    },
  };
}

function average(values) {
  return values.reduce((total, value) => total + value, 0) / (values.length || 1);
}

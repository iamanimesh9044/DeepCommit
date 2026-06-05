// mstone-highlight: adds a "Highlight" toggle button next to the existing
// "Filter" button (in the bundled React toolbar). Unlike Filter, Highlight
// keeps every node/edge visible and only dims the unrelated ones, with
// blue outlines on selected nodes and amber outlines on prerequisite-only
// closure nodes.
//
// Activates only when `selected_milestone_ids.txt` exists for the current
// dataset (served via /api/mstone/selected-ids). Loaded by app.py via HTML
// injection so the minified bundled frontend stays untouched.

(function () {
  const STYLE_ID = "mstone-selected-highlight-style";
  const BUTTON_ID = "mstone-highlight-toggle";
  let knownBasePath = null;
  let selectedIds = null;
  let nonGradedIds = [];
  let prerequisiteIds = [];
  let selectedEdges = [];
  let prerequisiteEdges = [];
  let toggleOn = false;

  const cssEscape = (s) =>
    window.CSS && CSS.escape
      ? CSS.escape(s)
      : String(s).replace(/([!"#$%&'()*+,./:;<=>?@[\\\]^`{|}~])/g, "\\$1");

  // ── 1. Discover basePath from the bundle's own fetch calls ──────────────
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input && input.url;
    if (url) {
      const m = url.match(/^(.+)\/milestones\.csv(?:\?|$)/);
      if (m && m[1] !== knownBasePath) {
        knownBasePath = m[1];
        loadSelectedIds(knownBasePath);
      }
    }
    return origFetch.apply(this, arguments);
  };

  async function loadSelectedIds(basePath) {
    const apiBasePath = basePath.startsWith("/DATA/") ? basePath.slice(6) : basePath;
    try {
      const r = await fetch(
        `/api/mstone/selected-ids?basePath=${encodeURIComponent(apiBasePath)}`
      );
      const data = await r.json();
      // non-graded marking is always-on (executed-but-not-evaluated milestones),
      // independent of the Highlight toggle.
      nonGradedIds = Array.isArray(data.non_graded_ids) ? data.non_graded_ids : [];
      applyNonGradedMarking();
      if (data.exists && Array.isArray(data.ids) && data.ids.length) {
        selectedIds = data.ids;
        prerequisiteIds = Array.isArray(data.prerequisite_ids)
          ? data.prerequisite_ids
          : [];
        selectedEdges = Array.isArray(data.selected_edges)
          ? data.selected_edges
          : buildCompleteEdgeList(selectedIds);
        prerequisiteEdges = Array.isArray(data.prerequisite_edges)
          ? data.prerequisite_edges
          : [];
        // Keep current toggleOn state across dataset switches if same len, else reset.
        if (toggleOn) applyHighlight();
        scheduleButtonPlacement();
      } else {
        selectedIds = null;
        prerequisiteIds = [];
        selectedEdges = [];
        prerequisiteEdges = [];
        toggleOn = false;
        removeButton();
        removeStyle();
      }
    } catch (e) {
      console.warn("[mstone-highlight] failed to load selected-ids:", e);
    }
  }

  // ── 2. Find the existing "Filter" button and place ours next to it ──────
  //
  // The bundled React toolbar renders a button whose label is either
  // "Filter" or "Selected (N)" depending on its toggle. We locate it via
  // text content (with a MutationObserver because React mounts async),
  // then insert our Highlight button as its right-hand sibling, reusing
  // a stylistic clone of the same Tailwind classes for visual coherence.

  let placementScheduled = false;
  function scheduleButtonPlacement() {
    if (placementScheduled) return;
    placementScheduled = true;
    // Try immediately; if filter button isn't in DOM yet, observe.
    if (!tryPlaceButton()) {
      const obs = new MutationObserver(() => {
        if (tryPlaceButton()) obs.disconnect();
      });
      obs.observe(document.body, { childList: true, subtree: true });
      // Safety timeout — stop watching after 15s either way.
      setTimeout(() => obs.disconnect(), 15000);
    }
  }

  function findFilterButton() {
    // Look for a <button> whose visible <span> reads "Filter" or
    // "Selected (N)" — that's the existing toggle from the bundle.
    const buttons = document.querySelectorAll("button");
    for (const btn of buttons) {
      const txt = (btn.textContent || "").trim();
      if (txt === "Filter" || /^Selected \(\d+\)$/.test(txt)) return btn;
    }
    return null;
  }

  function tryPlaceButton() {
    if (document.getElementById(BUTTON_ID)) return true;
    const filterBtn = findFilterButton();
    if (!filterBtn) return false;
    const btn = makeButton(filterBtn);
    filterBtn.parentNode.insertBefore(btn, filterBtn.nextSibling);
    return true;
  }

  // Mirror the Tailwind class pattern from the bundled Filter button so our
  // Highlight button is visually identical, but with our OWN toggle state
  // driving inactive/active (instead of inheriting whatever state Filter is
  // in at clone time).
  const BTN_BASE_CLASSES =
    "flex items-center gap-1.5 px-2.5 py-1 border rounded-md text-xs font-medium shadow-sm transition-all";
  const BTN_INACTIVE_CLASSES =
    "bg-white border-slate-200 text-slate-600 hover:bg-slate-50";
  const BTN_ACTIVE_CLASSES =
    "bg-blue-500 border-blue-500 text-white hover:bg-blue-600";

  function makeButton(_referenceBtn) {
    const btn = document.createElement("button");
    btn.id = BUTTON_ID;
    btn.type = "button";
    btn.style.marginLeft = "0.25rem";
    btn.innerHTML =
      '<span style="display:inline-flex;align-items:center;gap:0.35rem">' +
      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M12 2l2.4 5 5.6.8-4 3.9.9 5.6L12 14.8 7.1 17.3 8 11.7 4 7.8l5.6-.8z"/></svg>' +
      '<span class="mstone-highlight-label">Highlight</span></span>';
    btn.title = "Highlight selected milestones and prerequisite closure";
    btn.onclick = () => {
      toggleOn = !toggleOn;
      if (toggleOn) applyHighlight();
      else removeStyle();
      updateButtonLabel(btn);
    };
    updateButtonLabel(btn);
    return btn;
  }

  function updateButtonLabel(btn) {
    const n = selectedIds ? selectedIds.length : 0;
    const p = prerequisiteIds ? prerequisiteIds.length : 0;
    const labelEl = btn.querySelector(".mstone-highlight-label");
    if (labelEl) {
      labelEl.textContent =
        toggleOn && p > 0 ? `Highlighted (${n}+${p})` :
        toggleOn ? `Highlighted (${n})` :
        "Highlight";
    }
    btn.className = `${BTN_BASE_CLASSES} ${
      toggleOn ? BTN_ACTIVE_CLASSES : BTN_INACTIVE_CLASSES
    }`;
  }

  function removeButton() {
    const btn = document.getElementById(BUTTON_ID);
    if (btn) btn.remove();
  }

  // ── 3. CSS injection: dim everything, then promote selected nodes and
  //       prerequisite-only closure nodes with distinct colors ───────────
  function applyHighlight() {
    removeStyle();
    if (!selectedIds || !selectedIds.length) return;

    const selectedNodeSelectors = buildNodeSelectors(selectedIds);
    const prerequisiteNodeSelectors = buildNodeSelectors(prerequisiteIds);
    const selectedEdgeSelectorParts = buildEdgeSelectorParts(selectedEdges);
    const prerequisiteEdgeSelectorParts = buildEdgeSelectorParts(prerequisiteEdges);
    const selectedEdgeSelectors = selectedEdgeSelectorParts.join(",\n  ");
    const prerequisiteEdgeSelectors = prerequisiteEdgeSelectorParts.join(",\n  ");
    const selectedEdgePathSelectors = selectedEdgeSelectorParts
      .map((s) => `${s} .react-flow__edge-path`)
      .join(",\n  ");
    const prerequisiteEdgePathSelectors = prerequisiteEdgeSelectorParts
      .map((s) => `${s} .react-flow__edge-path`)
      .join(",\n  ");

    const selectedNodeRingRules = buildNodeRingRules(
      selectedIds,
      "#3b82f6",
      "rgba(59,130,246,0.30)"
    );
    const prerequisiteNodeRingRules = buildNodeRingRules(
      prerequisiteIds,
      "#f59e0b",
      "rgba(245,158,11,0.32)"
    );

    const cssParts = [`
/* mstone-highlight injected */
.react-flow__node {
  opacity: 0.20 !important;
  filter: grayscale(0.5) !important;
  transition: opacity .25s ease, filter .25s ease;
}
.react-flow__edge {
  opacity: 0.08 !important;
  transition: opacity .25s ease;
}
.react-flow__edge .react-flow__edge-path {
  transition: stroke .25s ease, stroke-width .25s ease;
}
`];

    if (selectedNodeSelectors) {
      cssParts.push(`
${selectedNodeSelectors} {
  opacity: 1 !important;
  filter: none !important;
}

${selectedNodeRingRules}
`);
    }

    if (prerequisiteNodeSelectors) {
      cssParts.push(`
${prerequisiteNodeSelectors} {
  opacity: 0.96 !important;
  filter: none !important;
}

${prerequisiteNodeRingRules}
`);
    }

    if (selectedEdgeSelectors) {
      cssParts.push(`
${selectedEdgeSelectors} {
  opacity: 1 !important;
}
${selectedEdgePathSelectors} {
  stroke: #2563eb !important;
  stroke-width: 3 !important;
}
`);
    }

    if (prerequisiteEdgeSelectors) {
      cssParts.push(`
${prerequisiteEdgeSelectors} {
  opacity: 1 !important;
}
${prerequisiteEdgePathSelectors} {
  stroke: #f59e0b !important;
  stroke-width: 3.2 !important;
}
`);
    }

    const styleTag = document.createElement("style");
    styleTag.id = STYLE_ID;
    styleTag.textContent = cssParts.join("\n");
    document.head.appendChild(styleTag);
  }

  function buildCompleteEdgeList(ids) {
    const edges = [];
    for (const s of ids) {
      for (const t of ids) {
        if (s !== t) edges.push({ source: s, target: t });
      }
    }
    return edges;
  }

  function buildNodeSelectors(ids) {
    return (ids || [])
      .map((id) => `.react-flow__node[data-id="${cssEscape(id)}"]`)
      .join(",\n  ");
  }

  function buildEdgeSelectorParts(edges) {
    return (edges || [])
      .filter((edge) => edge && edge.source && edge.target)
      .map((edge) => {
        const sEsc = cssEscape(String(edge.source));
        const tEsc = cssEscape(String(edge.target));
        return `.react-flow__edge[data-testid="rf__edge-e-${sEsc}-${tEsc}"]`;
      });
  }

  function buildNodeRingRules(ids, color, shadowColor) {
    return (ids || [])
      .map(
        (id) =>
          `.react-flow__node[data-id="${cssEscape(id)}"] {\n` +
          `  box-shadow: 0 0 0 3px ${color}, 0 6px 14px ${shadowColor} !important;\n` +
          `  border-radius: 8px;\n` +
          `}`
      )
      .join("\n");
  }

  // Always-on marking for non-graded milestones (still built/run, but excluded from
  // the eval metrics). A red ring + dashed outline — visually opposite to the blue
  // "selected" highlight. Independent of the Highlight toggle and its dimming.
  function applyNonGradedMarking() {
    const old = document.getElementById("mstone-non-graded-style");
    if (old) old.remove();
    if (!nonGradedIds || !nonGradedIds.length) return;
    const rules = nonGradedIds
      .map((id) => {
        const e = cssEscape(id);
        return (
          `.react-flow__node[data-id="${e}"] {\n` +
          `  box-shadow: 0 0 0 3px #ef4444, 0 0 14px rgba(239,68,68,0.45) !important;\n` +
          `  outline: 2px dashed #ef4444 !important;\n` +
          `  outline-offset: 2px;\n` +
          `  border-radius: 8px !important;\n` +
          `}`
        );
      })
      .join("\n");
    const style = document.createElement("style");
    style.id = "mstone-non-graded-style";
    style.textContent =
      "/* mstone non-graded marking — executed but NOT evaluated */\n" + rules;
    document.head.appendChild(style);
  }

  function removeStyle() {
    const old = document.getElementById(STYLE_ID);
    if (old) old.remove();
  }
})();

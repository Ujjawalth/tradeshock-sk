/* TradeShock SK front end (vanilla JS).
 * Security: all user/AI text is inserted with textContent, never innerHTML.
 */
(() => {
  "use strict";

  // ------------------------------------------------------------ state
  const S = {
    config: null,
    zone: "Dark Brown",
    allowed: {},          // zone -> Set of allowed crops
    scenario: null,       // {affected, duration_months, assumptions, is_trade_related, dropped_crops}
    scenarioMeta: null,   // {source, headline}
    explainedKey: null,   // scenario JSON the current explanation describes
    charts: {},
    sweepCrop: null,
  };

  // ------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  const money0 = new Intl.NumberFormat("en-CA", { style: "currency", currency: "CAD", maximumFractionDigits: 0 });
  const money2 = new Intl.NumberFormat("en-CA", { style: "currency", currency: "CAD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const int0 = new Intl.NumberFormat("en-CA", { maximumFractionDigits: 0 });
  const fmtMoney = (v) => money0.format(v).replace("CA$", "$");
  const fmtMoney2 = (v) => money2.format(v).replace("CA$", "$");
  const fmtSigned = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + fmtMoney(Math.abs(v));
  const fmtPct = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(0) + "%";
  const fmtShort = (v) => {
    const a = Math.abs(v);
    const s = a >= 1e6 ? (a / 1e6).toFixed(2) + "M" : a >= 1e3 ? (a / 1e3).toFixed(0) + "k" : a.toFixed(0);
    return (v < 0 ? "−$" : "$") + s;
  };
  const fmtPrice = (v, unit) => (unit === "lb" ? "$" + v.toFixed(3) : fmtMoney2(v)) + "/" + unit;
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  async function api(path, payload) {
    const opts = payload === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    };
    let res;
    try {
      res = await fetch(path, opts);
    } catch (_) {
      throw new Error("Can't reach the TradeShock server. Check your connection.");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  // "Latest request wins": when sliders fire overlapping requests, a slow older
  // response must never overwrite a newer one. ticket(k) starts a request;
  // stale(k, t) is true if a newer request of the same kind has started since.
  const tickets = {};
  const ticket = (k) => (tickets[k] = (tickets[k] || 0) + 1);
  const stale = (k, t) => tickets[k] !== t;

  function showError(id, msg) {
    const node = $(id);
    node.textContent = msg || "";
    node.hidden = !msg;
  }

  function busy(btn, on) { if (btn) btn.disabled = on; }

  // ------------------------------------------------------------ profile
  function zoneCrops() { return S.config.zones[S.zone] || []; }

  function getProfile() {
    const num = (id, dflt) => {
      const v = parseFloat($(id).value);
      return Number.isFinite(v) ? v : dflt;
    };
    const lim = S.config.default_limits;
    return {
      soil_zone: S.zone,
      total_acres: num("acres", 2000),
      crops_allowed: [...S.allowed[S.zone]],
      limits: {
        canola_max_pct: num("lim-canola", lim.canola_max_pct),
        pulses_max_pct: num("lim-pulses", lim.pulses_max_pct),
        single_crop_max_pct: num("lim-single", lim.single_crop_max_pct),
        cereals_min_pct: num("lim-cereals", lim.cereals_min_pct),
      },
    };
  }

  function renderZones() {
    const group = $("zone-group");
    group.replaceChildren();
    for (const zone of Object.keys(S.config.zones)) {
      group.append(el("button", {
        type: "button", role: "radio", "aria-checked": String(zone === S.zone), text: zone,
        onclick: () => { S.zone = zone; renderZones(); renderCrops(); onProfileChange(); },
      }));
    }
  }

  function renderCrops() {
    const box = $("crop-chips");
    box.replaceChildren();
    const allowed = S.allowed[S.zone];
    for (const c of zoneCrops()) {
      const input = el("input", { type: "checkbox" });
      input.checked = allowed.has(c.crop);
      input.addEventListener("change", () => {
        input.checked ? allowed.add(c.crop) : allowed.delete(c.crop);
        label.classList.toggle("off", !input.checked);
        onProfileChange();
      });
      const label = el("label", { class: "chip" + (input.checked ? "" : " off") },
        input, c.crop, el("small", { text: c.crop_group }));
      box.append(label);
    }
  }

  async function buildBaseline() {
    showError("profile-error", "");
    busy($("btn-baseline"), true);
    const t = ticket("plan");
    try {
      const { plan } = await api("/api/plan", { profile: getProfile() });
      if (stale("plan", t)) return false;
      renderBaseline(plan);
      return true;
    } catch (e) {
      if (stale("plan", t)) return false;
      showError("profile-error", e.message);
      $("baseline-out").hidden = true;
      return false;
    } finally {
      busy($("btn-baseline"), false);
    }
  }

  function renderBaseline(plan) {
    $("baseline-out").hidden = false;
    $("step-ask").hidden = false;
    $("baseline-total").textContent = fmtMoney(plan.totals.return_over_variable);
    $("baseline-per-acre").textContent =
      `${fmtMoney2(plan.totals.return_over_variable_per_acre)}/ac · after all costs ${fmtMoney(plan.totals.return_over_total)}`;
    const tbody = $("baseline-table").querySelector("tbody");
    tbody.replaceChildren(...plan.crops.map((r) => el("tr", {},
      el("td", { text: r.crop }),
      el("td", { class: "num", text: int0.format(r.acres) }),
      el("td", { class: "num", text: r.share_pct.toFixed(0) + "%" }),
      el("td", { class: "num", text: fmtMoney2(r.return_over_variable_per_acre) }),
      el("td", { class: "num", text: fmtPrice(r.breakeven_price, r.unit) }),
    )));
  }

  const onProfileChange = debounce(async () => {
    saveFarm();
    const ok = await buildBaseline();
    if (ok && S.scenario && S.scenario.is_trade_related) runShock({ explain: true });
  }, 350);

  // ------------------------------------------------------------ presets & headline
  function renderPresets() {
    const box = $("presets");
    box.replaceChildren(...S.config.presets.map((p) => el("button", {
      type: "button", class: "preset", "data-id": p.id,
      onclick: (ev) => {
        document.querySelectorAll(".preset").forEach((b) => b.classList.remove("active"));
        ev.currentTarget.classList.add("active");
        $("headline").value = p.headline;
        updateCount();
        runScenario({ preset_id: p.id });
      },
    }, el("strong", { text: p.label }), el("span", { text: "HYPOTHETICAL · works offline" }))));
  }

  function updateCount() {
    $("headline-count").textContent = `${$("headline").value.length} / ${S.config.max_headline}`;
  }

  async function runScenario(payload) {
    showError("headline-error", "");
    $("headline-loading").hidden = false;
    busy($("btn-analyze"), true);
    try {
      const data = await api("/api/scenario", { ...payload, profile: getProfile() });
      S.scenario = data.scenario;
      S.scenarioMeta = { source: data.source, headline: data.headline, hypothetical: data.hypothetical };
      S.explainedKey = null;
      renderScenario();
      if (S.scenario.is_trade_related) await runShock({ explain: true, sweep: true });
      else hideResults();
    } catch (e) {
      showError("headline-error", e.message);
    } finally {
      $("headline-loading").hidden = true;
      busy($("btn-analyze"), false);
    }
  }

  function manualScenario() {
    S.scenario = { affected: [], duration_months: 12, assumptions: ["Prices set by hand by the user."], is_trade_related: true, dropped_crops: [] };
    S.scenarioMeta = { source: "manual", headline: "" };
    S.explainedKey = null;
    renderScenario();
    runShock({ explain: true, sweep: true });
  }

  function hideResults() {
    for (const id of ["step-results", "step-explain", "step-sweep", "step-risk"]) $(id).hidden = true;
  }

  // ------------------------------------------------------------ scenario UI
  function renderScenario() {
    const sc = S.scenario;
    $("scenario-out").hidden = false;
    $("not-trade").hidden = sc.is_trade_related;
    $("scenario-body").hidden = !sc.is_trade_related;
    if (!sc.is_trade_related) return;

    const src = S.scenarioMeta.source;
    const badge = $("scenario-source");
    badge.textContent = { ai: "AI · live", cache: "AI · cached preset", manual: "Manual", watch: "Trade Watch", shared: "Shared link" }[src] || src;
    badge.className = "badge" + (src === "manual" ? "" : " ai");
    $("scenario-headline").textContent = S.scenarioMeta.headline ? `“${S.scenarioMeta.headline}”` : "";

    renderSliders();

    $("duration").value = sc.duration_months;
    $("assumptions").replaceChildren(...sc.assumptions.map((a) => el("li", { text: a })));
    const dropped = $("dropped");
    dropped.hidden = !sc.dropped_crops.length;
    dropped.textContent = sc.dropped_crops.length
      ? `Ignored (no budget data for this crop): ${sc.dropped_crops.join(", ")}` : "";
  }

  function renderSliders() {
    const sc = S.scenario;
    S.rangeSetters = {};
    const box = $("sliders");
    box.replaceChildren();
    if (!sc.affected.length) {
      box.append(el("p", { class: "hint", text: "No crop prices changed yet. Add a crop below to set a price change." }));
    }
    sc.affected.forEach((a, i) => {
      const pct = el("span", { class: "pct" });
      const setPct = (v) => {
        pct.textContent = fmtPct(v);
        pct.className = "pct " + (v < 0 ? "neg" : v > 0 ? "pos" : "");
      };
      setPct(a.price_change_pct);
      const range = el("input", {
        type: "range", min: "-60", max: "60", step: "1", value: String(Math.round(a.price_change_pct)),
        "aria-label": `${a.crop} price change percent`,
      });
      const rangeText = el("span", { class: "range" });
      const setRange = () => {
        a.low_pct = Math.min(a.low_pct ?? a.price_change_pct, a.price_change_pct);
        a.high_pct = Math.max(a.high_pct ?? a.price_change_pct, a.price_change_pct);
        rangeText.textContent = `bear ${fmtPct(a.low_pct)} · bull ${fmtPct(a.high_pct)}${a.range_assumed ? " (assumed)" : ""}`;
      };
      setRange();
      S.rangeSetters[a.crop] = setRange;
      range.addEventListener("input", () => {
        a.price_change_pct = Number(range.value);
        setPct(a.price_change_pct);
        setRange();
        a.edited = true;
        onScenarioEdit();
      });
      box.append(el("div", { class: "slider-row" },
        el("div", { class: "slider-top" },
          el("span", { class: "crop", text: a.crop }),
          el("span", { class: `conf conf-${a.confidence}`, text: `${a.confidence} confidence` }),
          pct,
          el("button", {
            class: "remove", type: "button", title: `Remove ${a.crop}`, "aria-label": `Remove ${a.crop}`, text: "×",
            onclick: () => { sc.affected.splice(i, 1); renderSliders(); onScenarioEdit(); },
          }),
        ),
        range,
        rangeText,
        a.reasoning ? el("p", { class: "reason", text: a.reasoning }) : null,
      ));
    });
    renderAddCrop();
  }

  function renderAddCrop() {
    const used = new Set(S.scenario.affected.map((a) => a.crop));
    const sel = $("add-crop");
    sel.replaceChildren(el("option", { value: "", text: "Choose…" }),
      ...zoneCrops().filter((c) => !used.has(c.crop)).map((c) => el("option", { value: c.crop, text: c.crop })));
  }

  const onScenarioEdit = debounce(() => runShock({ explain: false }), 250);

  // ------------------------------------------------------------ shock plan
  const scenarioPayload = () => ({
    affected: S.scenario.affected.map(({ crop, price_change_pct, confidence, reasoning, low_pct, high_pct, range_assumed }) =>
      // assumed ranges are re-derived on the server from the (edited) estimate
      (range_assumed ? { crop, price_change_pct, confidence, reasoning }
                     : { crop, price_change_pct, confidence, reasoning, low_pct, high_pct })),
    duration_months: S.scenario.duration_months,
    assumptions: S.scenario.assumptions,
    is_trade_related: S.scenario.is_trade_related,
  });

  // Assumed bear/bull ranges are derived on the server; show the server's numbers.
  function syncAssumedRanges(validated) {
    if (!validated || !S.scenario) return;
    const byCrop = Object.fromEntries(validated.affected.map((a) => [a.crop, a]));
    for (const a of S.scenario.affected) {
      const v = byCrop[a.crop];
      if (!v || !a.range_assumed && a.low_pct != null) continue;
      a.low_pct = v.low_pct;
      a.high_pct = v.high_pct;
      a.range_assumed = v.range_assumed;
      if (S.rangeSetters && S.rangeSetters[a.crop]) S.rangeSetters[a.crop]();
    }
  }

  async function runShock({ explain = false, sweep = false } = {}) {
    const t = ticket("shock");
    try {
      const { comparison, scenario: validated } = await api("/api/shock", { profile: getProfile(), scenario: scenarioPayload() });
      if (stale("shock", t)) return;
      syncAssumedRanges(validated);
      showError("profile-error", "");
      renderResults(comparison);
      const key = JSON.stringify([getProfile(), scenarioPayload()]);
      if (explain) runExplain();
      else if (S.explainedKey && S.explainedKey !== key) $("btn-explain").hidden = false;
      updateSweepOptions(sweep);
      runRiskDebounced();
      runCasesDebounced();
    } catch (e) {
      if (stale("shock", t)) return;
      showError("profile-error", e.message);
      hideResults();
    }
  }

  function renderResults(cmp) {
    S.lastCmp = cmp;
    $("step-results").hidden = false;
    const s = cmp.summary;
    $("k-baseline").textContent = fmtMoney(s.baseline_return);
    $("k-stand").textContent = fmtMoney(s.stand_still_return);
    $("k-shock").textContent = fmtMoney(s.shock_return);
    const sub = $("k-shock-sub");
    sub.replaceChildren(el("span", {
      class: s.change_vs_baseline < 0 ? "delta-neg" : s.change_vs_baseline > 0 ? "delta-pos" : "",
      text: `${fmtSigned(s.change_vs_baseline)}${s.change_vs_baseline_pct !== null ? ` (${fmtPct(s.change_vs_baseline_pct)})` : ""} vs baseline`,
    }));
    $("k-value").textContent = s.mix_changed ? fmtSigned(s.value_of_replanning) : "$0 · mix holds";

    const shockByCrop = Object.fromEntries(cmp.shock.crops.map((r) => [r.crop, r]));
    const allCrops = Object.fromEntries([...cmp.baseline.crops, ...cmp.shock.crops].map((r) => [r.crop, r]));
    const tbody = $("compare-table").querySelector("tbody");
    tbody.replaceChildren(...cmp.changes.map((c) => {
      const r = shockByCrop[c.crop] || allCrops[c.crop];
      const ch = c.change_acres;
      return el("tr", {},
        el("td", { text: c.crop }),
        el("td", { class: "num", text: c.price_change_pct ? fmtPct(c.price_change_pct) : "–" }),
        el("td", { class: "num", text: int0.format(c.baseline_acres) }),
        el("td", { class: "num", text: int0.format(c.shock_acres) }),
        el("td", { class: "num " + (ch < 0 ? "delta-neg" : ch > 0 ? "delta-pos" : ""), text: ch ? (ch > 0 ? "+" : "−") + int0.format(Math.abs(ch)) : "–" }),
        el("td", { class: "num", text: shockByCrop[c.crop] ? fmtMoney2(r.return_over_variable_per_acre) : "–" }),
      );
    }));
    drawAcresChart(cmp);
    renderIncomeLine();
  }

  const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function chartDefaults() {
    if (reducedMotion) Chart.defaults.animation = false;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = cssVar("--ink-2");
    Chart.defaults.borderColor = cssVar("--line");
  }

  function drawAcresChart(cmp) {
    chartDefaults();
    const labels = cmp.changes.map((c) => c.crop);
    const data = {
      labels,
      datasets: [
        { label: "Baseline", data: cmp.changes.map((c) => c.baseline_acres), backgroundColor: cssVar("--series-1"), borderRadius: 4, maxBarThickness: 18 },
        { label: "Shock plan", data: cmp.changes.map((c) => c.shock_acres), backgroundColor: cssVar("--series-2"), borderRadius: 4, maxBarThickness: 18 },
      ],
    };
    if (S.charts.acres) {
      S.charts.acres.data = data;
      S.charts.acres.update();
      return;
    }
    S.charts.acres = new Chart($("acres-chart"), {
      type: "bar",
      data,
      options: {
        indexAxis: "y",
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 350 },
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${int0.format(ctx.parsed.x)} ac` } },
        },
        scales: {
          x: { beginAtZero: true, grid: { color: cssVar("--line") }, ticks: { callback: (v) => int0.format(v) } },
          y: { grid: { display: false } },
        },
      },
    });
  }

  // ------------------------------------------------------------ explanation
  function renderExplanation(text) { renderRichText($("explanation"), text); }

  function renderRichText(box, text) {
    box.replaceChildren();
    for (const line of text.split(/\n+/).map((l) => l.trim()).filter(Boolean)) {
      const p = el("p");
      // Support **bold** only; everything stays as text nodes (no HTML injection).
      line.split(/(\*\*[^*]+\*\*)/g).forEach((part) => {
        if (/^\*\*[^*]+\*\*$/.test(part)) p.append(el("strong", { text: part.slice(2, -2) }));
        else if (part) p.append(document.createTextNode(part));
      });
      box.append(p);
    }
  }

  async function runExplain() {
    $("step-explain").hidden = false;
    $("explain-loading").hidden = false;
    $("btn-explain").hidden = true;
    const key = JSON.stringify([getProfile(), scenarioPayload()]);
    const t = ticket("explain");
    try {
      const data = await api("/api/explain", { profile: getProfile(), scenario: scenarioPayload() });
      if (stale("explain", t)) return;
      renderExplanation(data.explanation);
      S.lastExplain = { text: data.explanation, source: data.source };
      const badge = $("explain-source");
      badge.textContent = { ai: "AI · live", cache: "AI · cached", template: "Offline summary" }[data.source] || data.source;
      badge.className = "badge" + (data.source === "template" ? "" : " ai");
      S.explainedKey = key;
    } catch (e) {
      if (!stale("explain", t)) renderExplanation("Couldn't write the summary: " + e.message);
    } finally {
      if (!stale("explain", t)) $("explain-loading").hidden = true;
    }
  }

  // ------------------------------------------------------------ stress sweep
  function updateSweepOptions(runNow) {
    const sel = $("sweep-crop");
    const crops = [...S.allowed[S.zone]];
    const affected = S.scenario.affected.map((a) => a.crop);
    if (!S.sweepCrop || !crops.includes(S.sweepCrop) || runNow) {
      S.sweepCrop = affected.find((c) => crops.includes(c)) || crops[0];
    }
    sel.replaceChildren(...crops.map((c) => el("option", { value: c, text: c })));
    sel.value = S.sweepCrop;
    $("step-sweep").hidden = false;
    runSweepDebounced();
  }

  async function runSweep() {
    if (!S.sweepCrop) return;
    const t = ticket("sweep");
    try {
      const { sensitivity } = await api("/api/sensitivity", {
        profile: getProfile(), scenario: scenarioPayload(), crop: S.sweepCrop,
      });
      if (stale("sweep", t)) return;
      drawSweep(sensitivity);
    } catch (e) {
      if (stale("sweep", t)) return;
      $("tipping").replaceChildren(el("li", {},
        `Sweep didn't finish: ${e.message} `,
        el("button", { type: "button", class: "btn ghost", text: "Retry", onclick: runSweep })));
    }
  }
  const runSweepDebounced = debounce(runSweep, 400);

  function drawSweep(sens) {
    chartDefaults();
    const tipSet = new Set(sens.tipping_points.map((t) => t.price_change_pct));
    const current = (S.scenario.affected.find((a) => a.crop === sens.crop) || {}).price_change_pct || 0;
    const pts = sens.points;
    const line = cssVar("--series-1");
    const data = {
      labels: pts.map((p) => p.price_change_pct),
      datasets: [{
        label: "Farm return",
        data: pts.map((p) => p.return_over_variable),
        borderColor: line, backgroundColor: line, borderWidth: 2, tension: 0,
        pointRadius: pts.map((p) => (tipSet.has(p.price_change_pct) ? 6 : 0)),
        pointHoverRadius: 7, pointHitRadius: 12,
        pointBackgroundColor: pts.map((p) => (tipSet.has(p.price_change_pct) ? cssVar("--series-2") : line)),
        pointBorderColor: cssVar("--surface"), pointBorderWidth: 2,
      }],
    };
    const markerPlugin = {
      id: "currentMarker",
      afterDraw(chart) {
        const x = chart.scales.x, y = chart.scales.y;
        // Interpolate between category ticks so -18% sits between -20% and -15%
        const labels = chart.data.labels;
        const cur = Math.max(labels[0], Math.min(labels[labels.length - 1], S._sweepCurrent));
        let i = labels.findIndex((v) => v >= cur);
        if (i <= 0) i = 1;
        const frac = (cur - labels[i - 1]) / (labels[i] - labels[i - 1]);
        const px = x.getPixelForValue(i - 1) + frac * (x.getPixelForValue(i) - x.getPixelForValue(i - 1));
        const ctx = chart.ctx;
        ctx.save();
        ctx.strokeStyle = cssVar("--muted");
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(px, y.top); ctx.lineTo(px, y.bottom); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = cssVar("--ink-2");
        ctx.font = "12px " + Chart.defaults.font.family;
        ctx.textAlign = px > (x.left + x.right) / 2 ? "right" : "left";
        ctx.fillText("your scenario", px + (ctx.textAlign === "left" ? 6 : -6), y.top + 12);
        ctx.restore();
      },
    };
    S._sweepCurrent = current;
    S._sweepTips = sens.tipping_points;
    if (S.charts.sweep) S.charts.sweep.destroy();
    S.charts.sweep = new Chart($("sweep-chart"), {
      type: "line",
      data,
      plugins: [markerPlugin],
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => `${sens.crop} price ${fmtPct(Number(items[0].label))}`,
              label: (ctx) => `Farm return: ${fmtMoney(ctx.parsed.y)}`,
              afterLabel: (ctx) => {
                const p = pts[ctx.dataIndex];
                return `${sens.crop}: ${int0.format(p.crop_acres)} ac`;
              },
            },
          },
        },
        scales: {
          x: { title: { display: true, text: `${sens.crop} price change (%)` }, grid: { display: false },
               ticks: { callback: function (v) { const l = this.getLabelForValue(v); return l % 20 === 0 ? fmtPct(l) : ""; }, autoSkip: false, maxRotation: 0 } },
          y: { grid: { color: cssVar("--line") }, ticks: { callback: (v) => fmtShort(v) } },
        },
      },
    });

    const list = $("tipping");
    if (!sens.tipping_points.length) {
      list.replaceChildren(el("li", { text: `The best crop mix keeps the same crops across the whole range for ${sens.crop}.` }));
    } else {
      list.replaceChildren(...sens.tipping_points.map((t) => {
        const parts = [];
        if (t.crops_in.length) parts.push(`${t.crops_in.join(", ")} enters`);
        if (t.crops_out.length) parts.push(`${t.crops_out.join(", ")} drops out`);
        return el("li", {}, el("strong", { text: `At ${fmtPct(t.price_change_pct)}: ` }), parts.join("; "));
      }));
    }
  }

  // ------------------------------------------------------------ Other farm income
  S.lease = null;
  const stableIncome = () => (S.lease && S.lease.annual_payment && $("lease-include").checked ? S.lease.annual_payment : 0);

  async function runLease(payload) {
    showError("lease-error", "");
    $("lease-loading").hidden = false;
    try {
      let res;
      if (payload instanceof FormData) {
        res = await fetch("/api/lease", { method: "POST", body: payload });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
        renderLease(data.lease);
      } else {
        renderLease((await api("/api/lease", payload)).lease);
      }
    } catch (e) {
      showError("lease-error", e.message);
    } finally {
      $("lease-loading").hidden = true;
    }
  }

  function renderLease(l) {
    S.lease = l;
    $("lease-out").hidden = false;
    const dl = $("lease-dl");
    const items = [];
    const add = (k, v, quote) => {
      items.push(el("dt", { text: k }), el("dd", { text: v }));
      if (quote) items.push(el("dd", { class: "quote", text: `“${quote}”` }));
    };
    add("Read by", l.method === "ai" ? "AI (quotes verified)" : "Offline extractor (quotes verified)");
    if (!l.is_lease) add("Note", "This doesn't look like a lease.");
    add("Annual payment", l.annual_payment != null ? fmtMoney2(l.annual_payment) + " / year" : "not found", l.payment_quote);
    add("Next compensation review", l.next_review_date
      ? `${l.next_review_date}${l.months_to_review != null ? ` (in ${l.months_to_review} months)` : ""}` : "not found", l.review_quote);
    if (l.lessee) add("Lessee", l.lessee);
    if (l.acres != null) add("Acres affected", `${l.acres}`);
    if (!l.synthetic) add("Reminder", "Use SYNTHETIC documents only in this demo.");
    if (l.issues.length) add("Rejected values", l.issues.join("; "));
    dl.replaceChildren(...items);
    renderIncomeLine();
  }

  function renderIncomeLine() {
    const line = $("income-line");
    const inc = stableIncome();
    if (!inc || !S.lastCmp) { line.hidden = true; return; }
    const s = S.lastCmp.summary;
    line.hidden = false;
    const review = S.lease.months_to_review != null && S.lease.months_to_review <= 24
      ? ` The lease's compensation review is due ${S.lease.next_review_date}.` : "";
    line.replaceChildren(
      "With ", el("strong", { text: `${fmtMoney(inc)}/yr` }), " stable lease income, the re-planned farm brings in ",
      el("strong", { text: fmtMoney(s.shock_return + inc) }), ` (baseline ${fmtMoney(s.baseline_return + inc)}).` +
      (s.change_vs_baseline < -1 ? ` It offsets ${Math.min(100, Math.round(100 * inc / -s.change_vs_baseline))}% of this shock's hit.` : "") + review);
  }

  // ------------------------------------------------------------ 1-page print report
  function buildPrintReport() {
    const cmp = S.lastCmp;
    if (!cmp) return false;
    const p = getProfile();
    const s = cmp.summary;
    const box = $("print-report");
    const kpi = (label, value) => el("div", { class: "pr-kpi" }, el("span", { text: label }), el("b", { text: value }));
    const head = (cells, numFrom = 1) => el("tr", {}, ...cells.map((c, i) => el("th", { class: i >= numFrom ? "num" : "", text: c })));
    const row = (cells, numFrom = 1) => el("tr", {}, ...cells.map((c, i) => el("td", { class: i >= numFrom ? "num" : "", text: c })));
    const sc = S.scenario || { affected: [], assumptions: [] };
    const scenarioLine = sc.affected.map((a) => `${a.crop} ${fmtPct(a.price_change_pct)} (bear ${fmtPct(a.low_pct ?? a.price_change_pct)}, bull ${fmtPct(a.high_pct ?? a.price_change_pct)})`).join("; ") || "no price changes";

    const parts = [
      el("h1", { text: "TradeShock SK: crop plan stress test" }),
      el("p", { class: "pr-sub", text: `${p.soil_zone} soil zone · ${int0.format(p.total_acres)} acres · ` +
        `limits: canola ≤${p.limits.canola_max_pct}%, pulses ≤${p.limits.pulses_max_pct}%, any crop ≤${p.limits.single_crop_max_pct}%, cereals ≥${p.limits.cereals_min_pct}% (assumptions) · ` +
        `printed ${new Date().toLocaleString("en-CA")}` }),
      el("h2", { text: "Scenario" }),
      el("p", { text: S.scenarioMeta && S.scenarioMeta.headline ? `Headline: “${S.scenarioMeta.headline}”` : "Prices set by hand." }),
      el("p", { text: `Price changes: ${scenarioLine}. Duration: ${sc.duration_months ?? "–"} months.` }),
      el("div", { class: "pr-kpis" },
        kpi("Baseline plan", fmtMoney(s.baseline_return)),
        kpi("Keep old plan", fmtMoney(s.stand_still_return)),
        kpi("Re-planned", fmtMoney(s.shock_return)),
        kpi("Value of re-planning", fmtSigned(s.value_of_replanning))),
      el("h2", { text: "Acres by crop (return over variable costs)" }),
      el("table", {}, head(["Crop", "Price Δ", "Baseline ac", "Shock ac", "Change"]),
        ...cmp.changes.map((c) => row([c.crop, c.price_change_pct ? fmtPct(c.price_change_pct) : "–",
          int0.format(c.baseline_acres), int0.format(c.shock_acres), c.change_acres ? fmtSigned(c.change_acres).replace("$", "") : "–"]))),
    ];
    if (stableIncome()) {
      parts.push(el("p", { text: `Stable income (surface lease, ${S.lease.method === "ai" ? "AI-extracted" : "extracted"}, quote-verified): ` +
        `${fmtMoney(stableIncome())}/yr → re-planned farm total ${fmtMoney(s.shock_return + stableIncome())}` +
        (S.lease.next_review_date ? `; next compensation review ${S.lease.next_review_date}.` : ".") }));
    }
    if (S.lastCases) {
      const pick = S.lastCases.plans.reduce((a, b) => (b.max_regret < a.max_regret ? b : a));
      parts.push(el("h2", { text: "Bear / base / bull" }),
        el("table", {}, head(["Plan", "Bear", "Base", "Bull", "Worst regret"]),
          ...S.lastCases.plans.map((pl) => row([pl.labels.map((l) => PLAN_LABEL[l] || l).join(" = ") + (pl === pick ? " ★" : ""),
            fmtMoney(pl.returns.bear), fmtMoney(pl.returns.base), fmtMoney(pl.returns.bull), fmtMoney(pl.max_regret)]))));
    }
    if (S.lastRisk) {
      const r = S.lastRisk;
      parts.push(el("h2", { text: `${r.include_yield ? "Price + yield" : "Price"} risk check (Statistics Canada history)` }),
        el("p", { text: `Return-max plan: expected ${fmtMoney(r.max_return_plan.risk.expected)}, bad year ${fmtMoney(r.max_return_plan.risk.bad_year)}. ` +
          `Risk-aware plan (${Object.entries(r.risk_aware_plan.acres).map(([c, a]) => `${c} ${int0.format(a)} ac`).join(", ")}): ` +
          `expected ${fmtMoney(r.risk_aware_plan.risk.expected)}, bad year ${fmtMoney(r.risk_aware_plan.risk.bad_year)}.` }),
        el("p", { class: "pr-small", text: r.sources.map((s) => `${s.kind}: ${s.n} historical ${s.kind === "price" ? "12-month moves" : "years"}, ${s.window}.`).join(" ") +
          ` ${int0.format(r.n_scenarios)} scenarios. ${r.include_yield ? "Price and yield treated as independent. " : ""}${r.model.note}` }));
    }
    if (S.lastExplain) {
      const ex = el("div");
      renderRichText(ex, S.lastExplain.text);
      parts.push(el("h2", { text: "What it means" }), ex);
    }
    parts.push(el("p", { class: "pr-warn", text: "Planning aid, not financial advice. Scenarios are hypothetical estimates." }));
    if (!S.config.data_verified) {
      parts.push(el("p", { class: "pr-small", text: "UNVERIFIED: crop budget numbers are placeholders until the official 2026 Saskatchewan Crop Planning Guide is loaded." }));
    }
    parts.push(el("p", { class: "pr-small", text: `Crop budgets: ${S.config.data_source}` }));
    box.replaceChildren(...parts);
    return true;
  }

  function printPlan() {
    if (!buildPrintReport()) return;
    window.print();
  }

  // ------------------------------------------------------------ Trade Watch
  const TRIAGE_LABEL = {
    "keyword-skip": "no trade or crop keywords", "needs-ai": "needs AI key to triage", "ai": "AI: not trade-related",
    cache: "AI (cached): not trade-related", sample: "not trade-related", deferred: "queued for next scan",
    "ai-error": "AI error",
  };

  async function runWatch(mode) {
    showError("watch-error", "");
    $("watch-loading").hidden = false;
    busy($("btn-watch-live"), true); busy($("btn-watch-sample"), true);
    try {
      const { watch } = await api("/api/watch", { profile: getProfile(), mode });
      renderWatch(watch, false);
    } catch (e) {
      showError("watch-error", e.message);
    } finally {
      $("watch-loading").hidden = true;
      busy($("btn-watch-live"), false); busy($("btn-watch-sample"), false);
    }
  }

  function renderWatch(w, scheduled) {
    $("watch-out").hidden = false;
    const live = w.source === "live";
    $("watch-meta").textContent =
      `${scheduled ? "Last automatic scan" : "Scanned"} ${w.scanned_at} · ${live ? "official feeds: " + w.feeds.join(", ") : "HYPOTHETICAL sample feed (offline demo)"}` +
      ` · ${w.alerts.length} alert${w.alerts.length === 1 ? "" : "s"}` +
      (w.errors.length ? ` · feed problems: ${w.errors.join("; ")}` : "") +
      (!w.ai_available && live ? " · AI offline: trade items can't be triaged without an API key" : "");

    const list = $("watch-alerts");
    if (!w.alerts.length) {
      list.replaceChildren(el("li", { class: "hint", text: "No trade-related items affecting your crops in this scan." }));
    } else {
      list.replaceChildren(...w.alerts.map((a) => {
        const imp = a.impact;
        const cls = imp ? (imp.change_vs_baseline < 0 ? "neg" : imp.change_vs_baseline > 0 ? "pos" : "") : "";
        const title = a.link ? el("a", { href: a.link, target: "_blank", rel: "noopener noreferrer", text: a.title }) : a.title;
        return el("li", { class: `alert ${cls}` },
          el("div", { class: "alert-title" }, title),
          el("div", { class: "alert-meta", text: `${a.source} · ${a.published || ""}` }),
          el("div", { class: "alert-row" },
            imp ? el("span", { class: `impact ${cls}`, text: `${fmtSigned(imp.change_vs_baseline)} to your plan` }) : null,
            imp && imp.mix_changed ? el("span", { class: "hint", text: `re-planning worth ${fmtSigned(imp.value_of_replanning)}` }) : null,
            ...a.scenario.affected.map((c) => el("span", { class: "crop-chip", text: `${c.crop} ${fmtPct(c.price_change_pct)}` })),
            el("button", { type: "button", class: "btn ghost", text: "Stress-test this →", onclick: () => loadWatchItem(a) }),
          ));
      }));
    }
    const other = w.other || [];
    $("watch-other-box").hidden = !other.length;
    $("watch-other-sum").textContent = `${other.length} other item${other.length === 1 ? "" : "s"} filtered out`;
    $("watch-other").replaceChildren(...other.map((o) => el("li", {},
      o.link ? el("a", { href: o.link, target: "_blank", rel: "noopener noreferrer", text: o.title }) : o.title,
      " ", el("small", { text: `(${TRIAGE_LABEL[o.triage] || o.triage})` }))));
  }

  function loadWatchItem(a) {
    S.scenario = JSON.parse(JSON.stringify(a.scenario));
    S.scenarioMeta = { source: "watch", headline: a.title };
    S.explainedKey = null;
    $("headline").value = a.title;
    updateCount();
    renderScenario();
    runShock({ explain: true, sweep: true });
    $("step-headline").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth" });
  }

  async function loadLatestWatch() {
    try {
      const { watch } = await api("/api/watch/latest");
      if (watch) renderWatch(watch, true);
    } catch (_) { /* optional */ }
  }

  // ------------------------------------------------------------ Bear / base / bull
  const PLAN_LABEL = {
    bear_plan: "Best if bear", base_plan: "Best if base", bull_plan: "Best if bull",
    maxmin_plan: "Safest worst case", regret_plan: "Least regret",
  };

  async function runCases() {
    const t = ticket("cases");
    if (!S.scenario || !S.scenario.affected.length) { $("cases-box").hidden = true; return; }
    try {
      const { cases } = await api("/api/cases", { profile: getProfile(), scenario: scenarioPayload() });
      if (!stale("cases", t)) renderCases(cases);
    } catch (_) {
      if (!stale("cases", t)) $("cases-box").hidden = true;
    }
  }
  const runCasesDebounced = debounce(runCases, 350);

  function renderCases(cs) {
    S.lastCases = cs;
    $("cases-box").hidden = false;
    const desc = (k) => Object.entries(cs.cases[k]).map(([c, p]) => `${c} ${fmtPct(p)}`).join(", ") || "no change";
    $("cases-hint").textContent =
      `Bear: ${desc("bear")}. Base: ${desc("base")}. Bull: ${desc("bull")}. ` +
      "Regret = how much less a plan earns than the best plan for the case that actually happens.";
    const pick = cs.plans.reduce((a, b) => (b.max_regret < a.max_regret ? b : a));
    $("cases-table").querySelector("tbody").replaceChildren(...cs.plans.map((p) => el("tr", { class: p === pick ? "pick" : "" },
      el("td", {}, p.labels.map((l) => PLAN_LABEL[l] || l).join(" = ") + (p === pick ? " ★" : ""),
        el("small", { text: Object.entries(p.acres).sort((a, b) => b[1] - a[1]).map(([c, a]) => `${c} ${int0.format(a)}`).join(" · ") })),
      ...["bear", "base", "bull"].map((k) => el("td", { class: "num", text: fmtShort(p.returns[k]) })),
      el("td", { class: "num", text: p.max_regret < 1 ? "$0" : fmtMoney(p.max_regret) }),
    )));
    const worstOther = Math.max(...cs.plans.filter((p) => p !== pick).map((p) => p.max_regret), 0);
    $("cases-verdict").replaceChildren(
      el("strong", { text: "★ " + pick.labels.map((l) => PLAN_LABEL[l] || l).join(" = ") + ": " }),
      `whichever case happens, this plan is never more than ${fmtMoney(pick.max_regret)} behind the best plan for that case` +
      (worstOther > pick.max_regret ? ` (other plans: up to ${fmtMoney(worstOther)}).` : "."));
  }

  // ------------------------------------------------------------ Risk lens
  const AVERSION_LABEL = { 0: "Maximize average", 25: "Mostly average", 50: "Balanced", 75: "Mostly protect", 100: "Protect bad years" };

  async function runRisk() {
    if (!S.config.risk_available || !S.scenario) return;
    const lam = Number($("risk-aversion").value) / 100;
    const t = ticket("risk");
    try {
      const { risk } = await api("/api/risk", { profile: getProfile(), scenario: scenarioPayload(), risk_aversion: lam,
                                                include_yield: $("risk-yield").checked });
      if (!stale("risk", t)) renderRisk(risk);
    } catch (e) {
      if (!stale("risk", t)) $("risk-tradeoff").textContent = "Risk check didn't run: " + e.message;
    }
  }
  const runRiskDebounced = debounce(runRisk, 450);

  function riskList(dl, part) {
    const r = part.risk;
    const rows = [
      ["Expected return", fmtMoney(r.expected)],
      ["Bad year (avg of worst 10%)", fmtMoney(r.bad_year)],
      [`Worst replay (${r.worst_label})`, fmtMoney(r.worst)],
      ["Typical range (10th–90th)", `${fmtShort(r.p10)} – ${fmtShort(r.p90)}`],
    ];
    if (r.chance_below_zero_pct > 0) rows.push(["Chance return < variable costs", `${r.chance_below_zero_pct.toFixed(1)}%`]);
    dl.replaceChildren(...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]),
      el("dd", { class: "mix", text: Object.entries(part.acres).map(([c, a]) => `${c} ${int0.format(a)} ac`).join(" · ") }));
  }

  function renderRisk(risk) {
    S.lastRisk = risk;
    $("step-risk").hidden = false;
    $("risk-intro").textContent = risk.include_yield
      ? `Replays ${risk.model.n_windows} real 12-month Saskatchewan price moves (${risk.model.window}) combined with ` +
        `${risk.sources[1].n} real yield years (${risk.sources[1].window}): ${int0.format(risk.n_scenarios)} scenarios. ` +
        "Averages stay at the budget values. Shows how bad a bad year gets, and a mix that cushions it."
      : `Replays ${risk.model.n_windows} real 12-month Saskatchewan price moves (${risk.model.window}) on your plan, ` +
        "with average prices held at the budget prices. Shows how bad a bad year gets, and a mix that cushions it.";
    $("risk-yield-wrap").hidden = !S.config.yield_risk_available;
    riskList($("risk-max"), risk.max_return_plan);
    riskList($("risk-safe"), risk.risk_aware_plan);
    const t = risk.tradeoff;
    $("risk-tradeoff").replaceChildren(
      t.bad_year_gain > 1
        ? el("span", {}, "The risk-aware plan gives up ", el("strong", { text: fmtMoney(t.expected_cost) }),
            " on average but protects ", el("strong", { text: fmtMoney(t.bad_year_gain) }), " in a bad year.")
        : el("span", { text: "At this setting the return-max plan is already the risk-aware plan. Slide right to protect bad years more." }));

    const maxA = risk.max_return_plan.acres, safeA = risk.risk_aware_plan.acres;
    const crops = [...new Set([...Object.keys(maxA), ...Object.keys(safeA)])];
    $("risk-table").querySelector("tbody").replaceChildren(...crops.map((c) => el("tr", {},
      el("td", { text: c }),
      el("td", { class: "num", text: int0.format(maxA[c] || 0) }),
      el("td", { class: "num", text: int0.format(safeA[c] || 0) }),
      el("td", { class: "num", text: risk.volatility[c] !== undefined ? `±${risk.volatility[c].toFixed(0)}%` : "–" }),
      el("td", { class: "num", text: risk.yield_volatility[c] !== undefined ? `±${risk.yield_volatility[c].toFixed(0)}%` : "–" }),
    )));
    $("risk-source").textContent = "Sources: " + risk.sources.map((s) => `${s.source} ${s.method} ${s.note}`).join(" ") +
      (risk.include_yield ? " Price and yield swings are treated as independent (an assumption)." : "");
    drawRiskHist(risk.histogram);
  }

  function drawRiskHist(h) {
    chartDefaults();
    const labels = h.edges.slice(0, -1).map((e, i) => `${fmtShort(e)}–${fmtShort(h.edges[i + 1])}`);
    const data = {
      labels,
      datasets: [
        { label: "Return-max plan", data: h.max_return_plan, backgroundColor: cssVar("--series-1"), borderRadius: 3, categoryPercentage: 0.9, barPercentage: 0.9 },
        { label: "Risk-aware plan", data: h.risk_aware_plan, backgroundColor: cssVar("--series-3"), borderRadius: 3, categoryPercentage: 0.9, barPercentage: 0.9 },
      ],
    };
    if (S.charts.risk) { S.charts.risk.data = data; S.charts.risk.update(); return; }
    S.charts.risk = new Chart($("risk-hist"), {
      type: "bar", data,
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${int0.format(ctx.parsed.y)} of ${int0.format(h.max_return_plan.reduce((a, b) => a + b, 0))} scenarios` } } },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 6,
               callback: function (v) { return this.getLabelForValue(v).split("–")[0]; } },
               title: { display: true, text: "Farm return over variable costs" } },
          y: { beginAtZero: true, grid: { color: cssVar("--line") }, ticks: { precision: 0 }, title: { display: true, text: "Scenarios" } },
        },
      },
    });
  }

  // ------------------------------------------------------------ Ask TradeShock agent
  const SUGGESTIONS = [
    "What if peas drop 20% and I cap canola at 25%?",
    "Where does canola tip out of the plan?",
    "Which plan is safest if I'm wrong about prices?",
    "How bad could a bad price year get?",
    "What if I rent 500 more acres?",
  ];
  S.chat = [];

  function renderSuggestions() {
    $("ask-suggestions").replaceChildren(...SUGGESTIONS.map((q) => el("button", {
      type: "button", class: "suggestion", text: q,
      onclick: () => { $("ask-input").value = q; ask(); },
    })));
  }

  function addMsg(role, text, extra) {
    const body = el("div");
    renderRichText(body, text);
    const msg = el("div", { class: `msg ${role === "user" ? "user" : "bot"}` }, body);
    if (extra) msg.append(extra);
    $("chat").append(msg);
    msg.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return msg;
  }

  async function ask() {
    const input = $("ask-input");
    const question = input.value.trim();
    if (!question) return;
    input.value = "";
    addMsg("user", question);
    const thinking = addMsg("bot", "Running the optimizer…");
    busy($("btn-ask"), true);
    try {
      const scenario = S.scenario ? scenarioPayload() : {};
      const data = await api("/api/ask", { question, profile: getProfile(), scenario, history: S.chat.slice(-6) });
      thinking.remove();
      const meta = el("div", { class: "msg-meta" },
        el("span", { class: "badge" + (data.source === "ai" ? " ai" : ""), text: data.source === "ai" ? "AI agent" : "Offline parser" }),
        ...data.steps.map((s) => el("span", { class: "step-chip" + (s.error ? " err" : ""), text: `${s.tool}()` })),
      );
      if (data.proposal) {
        meta.append(el("button", { type: "button", class: "btn ghost", text: "Apply this plan",
          onclick: (ev) => { applyProposal(data.proposal); ev.currentTarget.disabled = true; ev.currentTarget.textContent = "Applied ✓"; } }));
      }
      addMsg("bot", data.answer, meta);
      S.chat.push({ role: "user", text: question }, { role: "assistant", text: data.answer });
    } catch (e) {
      thinking.remove();
      addMsg("bot", "Sorry: " + e.message);
    } finally {
      busy($("btn-ask"), false);
    }
  }

  function applyProposal(p) {
    // Farm settings
    $("acres").value = p.profile.total_acres;
    const lim = p.profile.limits;
    $("lim-canola").value = lim.canola_max_pct;
    $("lim-pulses").value = lim.pulses_max_pct;
    $("lim-single").value = lim.single_crop_max_pct;
    $("lim-cereals").value = lim.cereals_min_pct;
    if (p.profile.crops_allowed) S.allowed[S.zone] = new Set(p.profile.crops_allowed);
    renderCrops();
    // Prices -> sliders (keep AI reasoning for crops already in the scenario)
    const old = Object.fromEntries((S.scenario ? S.scenario.affected : []).map((a) => [a.crop, a]));
    const affected = Object.entries(p.price_changes).map(([crop, pct]) => ({
      crop, price_change_pct: pct,
      confidence: old[crop] ? old[crop].confidence : "low",
      reasoning: old[crop] ? old[crop].reasoning : "Set by Ask TradeShock.",
    }));
    if (!S.scenario || !S.scenario.is_trade_related) {
      S.scenario = { affected, duration_months: 12, assumptions: ["Prices set via Ask TradeShock."], is_trade_related: true, dropped_crops: [] };
      S.scenarioMeta = { source: "manual", headline: "" };
    } else {
      S.scenario.affected = affected;
    }
    renderScenario();
    buildBaseline().then((ok) => ok && runShock({ explain: true, sweep: true }));
  }

  // ------------------------------------------------------------ admin AI lock
  function renderAiStatus() {
    const cfg = S.config;
    const pill = $("ai-pill");
    pill.textContent = cfg.ai_available ? "AI: live"
      : cfg.ai_locked ? "AI: locked (offline features still work)"
      : "AI: offline (presets + summaries still work)";
    pill.classList.toggle("on", cfg.ai_available);
    const btn = $("btn-lock");
    btn.hidden = !cfg.ai_lock_enabled;
    btn.textContent = cfg.ai_locked ? "🔒 Unlock AI" : "🔓 Lock AI";
  }

  async function refreshConfig() {
    try { S.config = await api("/api/config"); renderAiStatus(); } catch (_) { /* keep old */ }
  }

  async function onLockButton() {
    if (!S.config.ai_locked) {  // currently unlocked -> lock this browser again
      try { await api("/api/lock", {}); } catch (_) { /* ignore */ }
      await refreshConfig();
      return;
    }
    showError("unlock-error", "");
    $("unlock-password").value = "";
    $("unlock-dialog").showModal();
    $("unlock-password").focus();
  }

  async function submitUnlock(ev) {
    ev.preventDefault();
    showError("unlock-error", "");
    busy($("btn-unlock"), true);
    try {
      await api("/api/unlock", { password: $("unlock-password").value });
      $("unlock-password").value = "";
      $("unlock-dialog").close();
      await refreshConfig();
    } catch (e) {
      showError("unlock-error", e.message);
    } finally {
      busy($("btn-unlock"), false);
    }
  }

  // ------------------------------------------------------------ save on this device + share links
  const STORE_KEY = "tradeshock.farm.v1";

  function applyProfile(p) {
    if (!p || typeof p !== "object") return;
    if (S.config.zones[p.soil_zone]) S.zone = p.soil_zone;
    if (Number.isFinite(Number(p.total_acres))) $("acres").value = Number(p.total_acres);
    const lim = p.limits || {};
    for (const [id, k] of [["lim-canola", "canola_max_pct"], ["lim-pulses", "pulses_max_pct"],
      ["lim-single", "single_crop_max_pct"], ["lim-cereals", "cereals_min_pct"]]) {
      if (Number.isFinite(Number(lim[k]))) $(id).value = Number(lim[k]);
    }
    if (Array.isArray(p.crops_allowed)) {
      const valid = new Set(zoneCrops().map((c) => c.crop));
      S.allowed[S.zone] = new Set(p.crops_allowed.filter((c) => valid.has(c)));
    }
    renderZones();
    renderCrops();
  }

  function saveFarm() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(getProfile())); } catch (_) { /* storage blocked */ }
  }

  function loadFarm() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch (_) { return null; }
  }

  const b64encode = (obj) => btoa(unescape(encodeURIComponent(JSON.stringify(obj))))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const b64decode = (s) => JSON.parse(decodeURIComponent(escape(atob(s.replace(/-/g, "+").replace(/_/g, "/")))));

  async function copyShareLink() {
    const state = { p: getProfile(), s: S.scenario ? scenarioPayload() : null,
                    h: S.scenarioMeta ? (S.scenarioMeta.headline || "") : "" };
    const url = `${location.origin}${location.pathname}#s=${b64encode(state)}`;
    const status = $("share-status");
    try {
      await navigator.clipboard.writeText(url);
      status.textContent = "Link copied. It opens this exact farm and scenario.";
    } catch (_) {
      window.prompt("Copy this link:", url);
      status.textContent = "";
    }
  }

  // Shared link -> restore farm + scenario. Returns true if one was applied.
  function applySharedLink() {
    const m = location.hash.match(/^#s=([A-Za-z0-9_-]{10,8000})$/);
    if (!m) return false;
    let st;
    try { st = b64decode(m[1]); } catch (_) { return false; }
    applyProfile(st.p);
    if (st.s && Array.isArray(st.s.affected)) {
      // Untrusted: only known fields; the server re-validates (clamps, crop names) on every request.
      S.scenario = {
        affected: st.s.affected.slice(0, 12).map((a) => ({
          crop: String(a.crop || ""), price_change_pct: Number(a.price_change_pct) || 0,
          confidence: ["low", "medium", "high"].includes(a.confidence) ? a.confidence : "low",
          reasoning: String(a.reasoning || "").slice(0, 400),
          low_pct: a.low_pct, high_pct: a.high_pct, range_assumed: a.low_pct == null || a.high_pct == null,
        })),
        duration_months: Number(st.s.duration_months) || 12,
        assumptions: (Array.isArray(st.s.assumptions) ? st.s.assumptions : []).slice(0, 8).map((x) => String(x).slice(0, 300)),
        is_trade_related: true, dropped_crops: [],
      };
      S.scenarioMeta = { source: "shared", headline: String(st.h || "").slice(0, 600) };
    }
    return true;
  }

  // ------------------------------------------------------------ demo tour (offline-safe pitch walkthrough)
  let tourRun = 0;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function tourStep(n, total, sectionId, text, waitMs) {
    const run = tourRun;
    document.querySelectorAll(".tour-focus").forEach((c) => c.classList.remove("tour-focus"));
    const sec = $(sectionId);
    if (sec && !sec.hidden) {
      sec.classList.add("tour-focus");
      sec.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    }
    $("tour-step").textContent = `Step ${n} of ${total}`;
    $("tour-text").textContent = text;
    await sleep(waitMs);
    return run === tourRun;  // false if the tour was stopped
  }

  function stopTour() {
    tourRun++;
    $("tour-toast").hidden = true;
    document.querySelectorAll(".tour-focus").forEach((c) => c.classList.remove("tour-focus"));
  }

  async function startTour() {
    stopTour();
    const run = ++tourRun;
    $("tour-toast").hidden = false;
    const steps = 8;
    if (!(await tourStep(1, steps, "step-profile", "A 2,000-acre Dark Brown farm. The optimizer builds the best crop mix within rotation limits.", 3500))) return;
    await runWatch("sample");
    if (run !== tourRun) return;
    if (!(await tourStep(2, steps, "step-watch", "Trade Watch scans news, flags trade items and ranks them by dollar impact on THIS farm.", 4500))) return;
    const top = document.querySelector("#watch-alerts .alert .btn");
    if (top) top.click(); else document.querySelector(".preset")?.click();
    await sleep(1200);
    if (!(await tourStep(3, steps, "step-headline", "One click turns a headline into a scenario. The farmer keeps the final say on every slider.", 4500))) return;
    if (!(await tourStep(4, steps, "step-results", "Re-planning vs keeping the old plan: that green number is money left on the table.", 5000))) return;
    if (!(await tourStep(5, steps, "step-results", "Bear / base / bull: the ★ least-regret plan is never far behind, whichever case happens.", 4500))) return;
    if (!(await tourStep(6, steps, "step-risk", "Risk lens replays 140 real Saskatchewan price years from Statistics Canada.", 5000))) return;
    $("ask-input").value = "Which plan is safest if I'm wrong about prices?";
    ask();
    await sleep(1500);
    if (!(await tourStep(7, steps, "step-ask", "Ask in plain English. The AI agent calls the optimizer as tools, so every number is computed, not guessed.", 5500))) return;
    if (!(await tourStep(8, steps, "step-explain", "A plain-English summary and a 1-page PDF to take to the lender. Planning aid, not financial advice.", 5000))) return;
    stopTour();
  }

  // ------------------------------------------------------------ quick nav
  function initQuickNav() {
    const links = [...document.querySelectorAll("#quicknav a")];
    const sync = () => links.forEach((a) => {
      const sec = document.querySelector(a.getAttribute("href"));
      a.hidden = !sec || sec.hidden;
    });
    sync();
    const mo = new MutationObserver(sync);
    links.forEach((a) => {
      const sec = document.querySelector(a.getAttribute("href"));
      if (sec) mo.observe(sec, { attributes: true, attributeFilter: ["hidden"] });
    });
    if ("IntersectionObserver" in window) {
      const io = new IntersectionObserver((entries) => {
        entries.forEach((e) => {
          if (!e.isIntersecting) return;
          links.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + e.target.id));
        });
      }, { rootMargin: "-40% 0px -55% 0px" });
      links.forEach((a) => { const sec = document.querySelector(a.getAttribute("href")); if (sec) io.observe(sec); });
    }
  }

  // ------------------------------------------------------------ init
  async function init() {
    try {
      S.config = await api("/api/config");
    } catch (e) {
      showError("profile-error", e.message);
      return;
    }
    const cfg = S.config;
    for (const [zone, crops] of Object.entries(cfg.zones)) S.allowed[zone] = new Set(crops.map((c) => c.crop));
    if (!cfg.zones[S.zone]) S.zone = Object.keys(cfg.zones)[0];

    renderAiStatus();

    if (!cfg.data_verified) {
      $("data-banner").hidden = false;
      $("data-banner-text").textContent =
        " Crop budget numbers are placeholders until the official 2026 Saskatchewan Crop Planning Guide is loaded. Don't use them for decisions.";
    }
    $("data-source").textContent = "Crop budgets: " + cfg.data_source;

    const lim = cfg.default_limits;
    $("lim-canola").value = lim.canola_max_pct;
    $("lim-pulses").value = lim.pulses_max_pct;
    $("lim-single").value = lim.single_crop_max_pct;
    $("lim-cereals").value = lim.cereals_min_pct;

    renderZones();
    renderCrops();
    renderPresets();
    renderSuggestions();
    initQuickNav();
    updateCount();
    $("ask-form").addEventListener("submit", (ev) => { ev.preventDefault(); ask(); });
    $("btn-print").addEventListener("click", printPlan);
    $("btn-tour").addEventListener("click", startTour);
    $("tour-stop").addEventListener("click", stopTour);
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("tour-toast").hidden) stopTour(); });
    $("btn-lease-sample").addEventListener("click", () => runLease({ sample: true }));
    $("lease-file").addEventListener("change", (ev) => {
      const f = ev.target.files[0];
      if (!f) return;
      if (f.size > 2 * 1024 * 1024) { showError("lease-error", "File too large (max 2 MB)."); return; }
      const fd = new FormData();
      fd.append("file", f);
      runLease(fd);
      ev.target.value = "";
    });
    $("lease-include").addEventListener("change", renderIncomeLine);
    window.addEventListener("beforeprint", buildPrintReport);  // Ctrl+P also gets the 1-page report
    $("btn-watch-live").addEventListener("click", () => runWatch("live"));
    $("btn-watch-sample").addEventListener("click", () => runWatch("sample"));
    loadLatestWatch();

    $("btn-baseline").addEventListener("click", buildBaseline);
    for (const id of ["acres", "lim-canola", "lim-pulses", "lim-single", "lim-cereals"]) {
      $(id).addEventListener("input", onProfileChange);
    }
    $("headline").addEventListener("input", updateCount);
    $("btn-analyze").addEventListener("click", () => {
      document.querySelectorAll(".preset").forEach((b) => b.classList.remove("active"));
      runScenario({ headline: $("headline").value });
    });
    $("btn-manual").addEventListener("click", manualScenario);
    $("btn-explain").addEventListener("click", runExplain);
    $("add-crop").addEventListener("change", (ev) => {
      const crop = ev.target.value;
      if (!crop) return;
      S.scenario.affected.push({ crop, price_change_pct: 0, confidence: "low", reasoning: "Added by you.", edited: true, range_assumed: true });
      renderSliders();
      onScenarioEdit();
    });
    $("duration").addEventListener("input", (ev) => {
      const v = parseInt(ev.target.value, 10);
      if (Number.isFinite(v)) { S.scenario.duration_months = Math.max(0, Math.min(60, v)); }
    });
    $("risk-yield").addEventListener("change", runRiskDebounced);
    $("risk-aversion").addEventListener("input", (ev) => {
      $("risk-aversion-val").textContent = AVERSION_LABEL[ev.target.value] || "";
      runRiskDebounced();
    });
    $("sweep-crop").addEventListener("change", (ev) => { S.sweepCrop = ev.target.value; runSweep(); });
    $("btn-share").addEventListener("click", copyShareLink);
    $("btn-lock").addEventListener("click", onLockButton);
    $("unlock-form").addEventListener("submit", submitUnlock);
    $("btn-unlock-cancel").addEventListener("click", () => $("unlock-dialog").close());

    // A shared link wins; otherwise restore the farm saved on this device.
    const shared = applySharedLink();
    if (!shared) applyProfile(loadFarm());

    const ok = await buildBaseline();
    if (shared && ok && S.scenario) {
      $("headline").value = S.scenarioMeta.headline;
      updateCount();
      renderScenario();
      runShock({ explain: true, sweep: true });
      return;
    }

    // ?demo=<preset-id> auto-runs a preset (handy for the live pitch)
    const demo = new URLSearchParams(location.search).get("demo");
    const btn = demo && document.querySelector(`.preset[data-id="${CSS.escape(demo)}"]`);
    if (btn) btn.click();
  }

  document.addEventListener("DOMContentLoaded", init);
})();

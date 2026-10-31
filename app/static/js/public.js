/*
 * Public status page: server cards, sort, search, open card with one range control for its numbers,
 * status bar and ping chart, a shared tooltip/cursor for bars and chart (mouse and touch), 60s refresh.
 */
(function () {
  "use strict";

  var REFRESH_MS = 60000;
  var RANGES = ["1h", "24h", "7d", "30d"];
  var DEFAULT_RANGE = "1h";
  var T = window.UptimeTime;
  // LTR isolate, not first-strong: "99.86%" and "12:00–12:30" have no strong letter, so in a
  // Persian tooltip a first-strong isolate would follow the surrounding RTL direction.
  var ISO_L = "\u2066", ISO_R = "\u2069";
  var list = document.getElementById("live-list");
  var tooltip = document.getElementById("tooltip");
  var emptySearch = document.getElementById("empty-search");
  var search = document.getElementById("q");
  var state = { tab: "all", query: "", open: {}, ranges: {} };
  var cache = {};  // "id:range" -> /api/servers/{id}/history payload
  try {
    var saved = JSON.parse(localStorage.getItem("status-view") || "{}");
    if (saved.tab === "fastest" || saved.tab === "stable" || saved.tab === "all") state.tab = saved.tab;
  } catch (e) { /* ignore */ }

  function saveTab() {
    try { localStorage.setItem("status-view", JSON.stringify({ tab: state.tab })); } catch (e) { /* ignore */ }
  }

  function percent(v) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
    return (Math.round(Number(v) * 100) / 100).toFixed(2).replace(/\.?0+$/, "") + "%";
  }

  function ms(v) { return v === null || v === undefined ? "—" : Math.round(v) + " ms"; }

  function cardOf(id) { return document.querySelector('#servers > li[data-id="' + id + '"]'); }

  // ------------------------------------------------------------ sort + search
  function applyView() {
    document.querySelectorAll("[data-tab]").forEach(function (el) {
      var on = el.getAttribute("data-tab") === state.tab;
      el.classList.toggle("is-active", on);
      el.setAttribute("aria-selected", on ? "true" : "false");
    });
    var ul = document.getElementById("servers");
    if (!ul) { if (emptySearch) emptySearch.hidden = true; return; }
    var key = state.tab === "fastest" ? "data-rank-fastest" : state.tab === "stable" ? "data-rank-stable" : "data-order";
    var rows = Array.prototype.slice.call(ul.children);
    rows.sort(function (a, b) {
      var ca = a.getAttribute("data-collecting") === "1" ? 1 : 0;
      var cb = b.getAttribute("data-collecting") === "1" ? 1 : 0;
      if (ca !== cb) return ca - cb;
      var ra = parseInt(a.getAttribute(key), 10), rb = parseInt(b.getAttribute(key), 10);
      return (isNaN(ra) ? 1e9 : ra) - (isNaN(rb) ? 1e9 : rb);
    });
    var shown = 0;
    rows.forEach(function (row) {
      ul.appendChild(row);
      var text = row.getAttribute("data-search") || "";
      var match = !state.query || text.indexOf(state.query) !== -1;
      row.hidden = !match;
      if (match) shown++;
    });
    if (emptySearch) emptySearch.hidden = shown > 0 || rows.length === 0;
  }

  // ------------------------------------------------------- open card + range
  function showOpen(card, open) {
    card.classList.toggle("is-open", open);
    card.querySelector(".card-main").setAttribute("aria-expanded", open ? "true" : "false");
    card.querySelector(".card-detail").hidden = !open;
  }

  function setOpen(card, open) {
    var id = card.getAttribute("data-id");
    hideTip();
    showOpen(card, open);
    state.open[id] = open;
    if (open) showRange(card, state.ranges[id] || DEFAULT_RANGE);
  }

  function markRange(card, range) {
    card.querySelectorAll(".range-seg [data-range]").forEach(function (b) {
      var on = b.getAttribute("data-range") === range;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setStat(card, name, text) {
    var el = card.querySelector('[data-stat="' + name + '"]');
    if (el) el.textContent = text;
  }

  function showLoading(card) {
    ["uptime", "ping", "incidents"].forEach(function (name) { setStat(card, name, "…"); });
    var timeline = card.querySelector(".timeline");
    timeline.classList.add("is-loading");
    timeline._data = null;
    timeline.querySelector(".range-bars").innerHTML = "";
  }

  function render(card, range, data) {
    var collecting = card.getAttribute("data-collecting") === "1";
    var avg = data.summary.avg_latency;
    setStat(card, "uptime", collecting && range !== "1h" && range !== "24h" ? "—" : percent(data.summary.uptime));
    setStat(card, "ping", ms(avg));
    setStat(card, "incidents", String(data.incident_stats.count));
    var timeline = card.querySelector(".timeline");
    timeline.classList.remove("is-loading");
    timeline._data = data;
    timeline._range = range;
    var bars = timeline.querySelector(".range-bars");
    bars.innerHTML = data.bars.map(function (b) { return '<i class="b lv-' + b.level + '"></i>'; }).join("");
    var rangeWord = T.word("range_long_" + range);
    bars.setAttribute("aria-label", fill("aria_status", { range: rangeWord }));
    var canvas = timeline.querySelector("canvas");
    canvas.setAttribute("aria-label", avg === null || avg === undefined
      ? fill("aria_ping_empty", { range: rangeWord })
      : fill("aria_ping", { range: rangeWord, ms: String(Math.round(avg)) }));
    if (window.MiniChart) window.MiniChart.timeline(canvas, { points: data.points, bars: data.bars, range: range });
  }

  /* One request per server and range fills the numbers, the bar and the chart; results are cached. */
  function showRange(card, range, quiet) {
    var id = card.getAttribute("data-id"), key = id + ":" + range;
    state.ranges[id] = range;
    markRange(card, range);
    hideTip();
    if (cache[key]) { render(card, range, cache[key]); return; }
    if (!quiet) showLoading(card);
    fetch("/api/servers/" + encodeURIComponent(id) + "/history?range=" + encodeURIComponent(range),
      { headers: { "Accept": "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        cache[key] = data;
        var current = cardOf(id);
        if (current && state.open[id] && state.ranges[id] === range) render(current, range, data);
      })
      .catch(function () {
        var current = cardOf(id);
        if (!current || quiet) return;
        ["uptime", "ping", "incidents"].forEach(function (name) { setStat(current, name, "—"); });
        current.querySelector(".timeline").classList.remove("is-loading");
      });
  }

  /* After a refresh: reopen cards, keep each range, show the previous data at once, then fetch fresh data. */
  function restore() {
    Object.keys(state.open).forEach(function (id) {
      if (!state.open[id]) return;
      var card = cardOf(id);
      if (!card) return;
      var range = state.ranges[id] || DEFAULT_RANGE, stale = cache[id + ":" + range];
      RANGES.forEach(function (r) { delete cache[id + ":" + r]; });
      showOpen(card, true);
      markRange(card, range);
      if (stale) render(card, range, stale);
      showRange(card, range, !!stale);
    });
  }

  // --------------------------------------------- tooltip + cursor (bar and chart)
  var tipFor = null;  // the bar or timeline the tooltip belongs to
  var dragging = null;  // touch/pen: the region the finger is on

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function indexAt(el, clientX, n) {
    var r = el.getBoundingClientRect();
    return clamp(Math.floor((clientX - r.left) / r.width * n), 0, n - 1);
  }

  function highlight(barsEl, index) {
    document.querySelectorAll(".b.is-hover").forEach(function (b) { b.classList.remove("is-hover"); });
    if (barsEl && barsEl.children[index]) barsEl.children[index].classList.add("is-hover");
  }

  function fill(key, map) {
    var text = T.word(key);
    Object.keys(map).forEach(function (name) { text = text.split("{" + name + "}").join(map[name]); });
    return text;
  }

  function iso(text) { return ISO_L + text + ISO_R; }

  function spanText(start, end, range) {
    if (range === "30d") return T.formatMonthDay(start);
    var clock = T.formatTime(start) + "–" + T.formatTime(end);
    return range === "7d" ? T.formatMonthDay(start) + " " + clock : clock;
  }

  function uptimeText(u) {
    if (u === null || u === undefined || u === "") return T.word("tip_nodata");
    return fill("tip_uptime", { value: iso(percent(u)) });
  }

  function showTip(text) {
    tooltip.textContent = text;
    tooltip.hidden = false;
    return tooltip.getBoundingClientRect();
  }

  function hideTip() {
    tooltip.hidden = true;
    tipFor = null;
    highlight(null);
    document.querySelectorAll(".timeline .cursor").forEach(function (c) { c.hidden = true; });
  }

  /* Closed card: one cell per recent check. An empty cell has no data-t. */
  function pointBar(barsEl, clientX) {
    var cells = barsEl.children, n = cells.length;
    if (!n || !T) return;
    var i = indexAt(barsEl, clientX, n), cell = cells[i];
    var at = cell.getAttribute("data-t");
    var text;
    if (!at) {
      text = T.word("tip_nodata");
    } else {
      var clock = iso(T.formatTime(at));
      var status = cell.getAttribute("data-s");
      var ping = cell.getAttribute("data-ms");
      if (status === "fail") text = clock + " · " + T.word("tip_fail");
      else if (status === "ok" && ping !== null && ping !== "") text = clock + " · " + iso(Math.round(Number(ping)) + " ms");
      else if (status === "ok") text = clock + " · " + T.word("tip_noping");
      else text = clock + " · " + T.word("tip_nodata");
    }
    highlight(barsEl, i);
    var t = showTip(text), box = cells[i].getBoundingClientRect(), top = barsEl.getBoundingClientRect().top;
    tooltip.style.left = clamp(box.left + box.width / 2 - t.width / 2, 8, window.innerWidth - t.width - 8) + "px";
    tooltip.style.top = Math.max(8, top - t.height - 8) + "px";
    tipFor = barsEl;
  }

  /* The slot that contains t. endOf(i) is that slot's end in ms. */
  function slotAt(items, t, endOf) {
    var i, start, end, first = Infinity;
    for (i = 0; i < items.length; i++) {
      start = Date.parse(items[i].t || items[i].start);
      if (i === 0) first = start;
      end = endOf(i);
      if (t >= start && t < end) return i;
    }
    if (!items.length) return -1;
    return t < first ? 0 : items.length - 1;
  }

  /* Open card: one chart point per status-bar cell, on the same slot. The tooltip is that cell.
     The cursor sits on the point and the cell under it is highlighted.
     The tooltip goes above the bar beside the numbers when there is room, else into the chart. */
  function pointTimeline(timeline, clientX) {
    var data = timeline._data;
    if (!data || !data.bars.length || !data.points.length || !T) return;
    var box = timeline.getBoundingClientRect();
    var axisStart = Date.parse(data.bars[0].start);
    var axisEnd = Date.parse(data.bars[data.bars.length - 1].end);
    var span = axisEnd - axisStart;
    if (!(span > 0)) return;
    var at = axisStart + clamp((clientX - box.left) / box.width, 0, 0.999999) * span;
    var step = data.points.length > 1 ? Date.parse(data.points[1].t) - Date.parse(data.points[0].t) : span;
    var pi = slotAt(data.points, at, function (i) { return Date.parse(data.points[i].t) + step; });
    var point = data.points[pi];
    var pStart = Date.parse(point.t);
    var pEnd = pStart + step;
    var ping = point.latency;
    var pingText = ping === null || ping === undefined ? T.word("tip_noping") : iso(Math.round(ping) + " ms");
    var text = iso(spanText(point.t, new Date(pEnd).toISOString(), timeline._range)) + " · " +
      uptimeText(point.uptime) + " · " + pingText;
    var x = clamp(((pStart + pEnd) / 2 - axisStart) / span * box.width, 0, box.width);
    var cursor = timeline.querySelector(".cursor");
    cursor.style.left = x + "px";
    cursor.hidden = false;
    var barsEl = timeline.querySelector(".range-bars");
    var bi = slotAt(data.bars, at, function (i) { return Date.parse(data.bars[i].end); });
    highlight(barsEl, bi);

    var t = showTip(text), lineX = box.left + x, left, top;
    var card = timeline.closest(".card").getBoundingClientRect();
    var stats = timeline.parentNode.querySelector(".card-stats");
    var statsRight = Math.max.apply(null, Array.prototype.map.call(stats.children, function (c) {
      return c.getBoundingClientRect().right;
    }));
    var minLeft = statsRight + 16, maxLeft = card.right - 14 - t.width;
    if (maxLeft >= minLeft) {
      left = clamp(lineX - t.width / 2, minLeft, maxLeft);
      top = barsEl.getBoundingClientRect().top - t.height - 6;
    } else {
      left = lineX + 10;
      if (left + t.width > window.innerWidth - 8) left = lineX - 10 - t.width;
      top = timeline.querySelector(".chart").getBoundingClientRect().top + 6;
    }
    tooltip.style.left = clamp(left, 8, window.innerWidth - t.width - 8) + "px";
    tooltip.style.top = Math.max(8, top) + "px";
    tipFor = timeline;
  }

  function region(target) {
    return target && target.closest ? (target.closest(".card.is-open .timeline") || target.closest(".card-bars")) : null;
  }

  function point(el, clientX) {
    if (el.classList.contains("timeline")) pointTimeline(el, clientX); else pointBar(el, clientX);
  }

  list.addEventListener("pointermove", function (e) {
    if (e.pointerType === "mouse") {
      var el = region(e.target);
      if (el) point(el, e.clientX); else if (tipFor) hideTip();
    } else if (dragging) {
      point(dragging, e.clientX);
    }
  });
  var lastPointer = "mouse";
  list.addEventListener("pointerdown", function (e) {
    lastPointer = e.pointerType || "mouse";
    var el = region(e.target);
    if (el && e.pointerType !== "mouse") { dragging = el; point(el, e.clientX); }
  });
  list.addEventListener("pointerup", function () { dragging = null; });
  list.addEventListener("pointercancel", function () { dragging = null; hideTip(); });  // the page scrolled
  list.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse") hideTip(); });
  document.addEventListener("pointerdown", function (e) { if (tipFor && !region(e.target)) hideTip(); });
  window.addEventListener("scroll", function () { if (!dragging) hideTip(); }, { passive: true });
  window.addEventListener("resize", hideTip);

  // ------------------------------------------------------------------ clicks
  list.addEventListener("click", function (e) {
    var rangeBtn = e.target.closest(".range-seg [data-range]");
    if (rangeBtn) { showRange(rangeBtn.closest(".card"), rangeBtn.getAttribute("data-range")); return; }
    var card = e.target.closest("#servers > li");
    // The open section (its bar and chart) keeps its own pointer handling and does not toggle the card.
    if (!card || e.target.closest(".card-detail")) return;
    // A mouse click on the closed card's bar opens it. Touch only moves the tooltip.
    if (e.target.closest(".card-bars")) {
      if (lastPointer === "mouse" && !card.classList.contains("is-open")) setOpen(card, true);
      return;
    }
    setOpen(card, !card.classList.contains("is-open"));
  });

  // ----------------------------------------------------------------- refresh
  /* Replace a summary line only when what it says changed, so the live region is not re-read. */
  function swapSummary(fresh) {
    var current = document.getElementById("live-summary");
    if (!current || !fresh) return;
    var now = current.children, next = fresh.children;
    if (now.length !== next.length) { current.innerHTML = fresh.innerHTML; return; }
    for (var i = 0; i < next.length; i++) {
      if (now[i].textContent !== next[i].textContent || now[i].className !== next[i].className) {
        current.replaceChild(document.importNode(next[i], true), now[i]);
      }
    }
  }

  var refreshing = false;
  function refresh() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    var y = window.scrollY, focus = null, active = document.activeElement;
    var activeCard = active && active.closest ? active.closest("#servers > li") : null;
    if (activeCard) {
      focus = { id: activeCard.getAttribute("data-id"),
                range: active.closest(".range-seg") ? active.getAttribute("data-range") : null };
    }
    fetch("/partials/live?range=24h", { headers: { "Accept": "text/html" } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var freshList = doc.getElementById("live-list");
        if (!freshList) throw new Error("unexpected fragment");
        hideTip();
        dragging = null;
        swapSummary(doc.getElementById("live-summary"));
        list.innerHTML = freshList.innerHTML;
        applyView();
        restore();
        window.scrollTo(0, y);
        if (focus) {
          var card = cardOf(focus.id);
          var el = card && (focus.range ? card.querySelector('.range-seg [data-range="' + focus.range + '"]')
            : card.querySelector(".card-main"));
          if (el) el.focus({ preventScroll: true });
        }
      })
      .catch(function () { /* keep the current list */ })
      .then(function () { refreshing = false; });
  }

  document.querySelectorAll("[data-tab]").forEach(function (el) {
    el.addEventListener("click", function () { state.tab = el.getAttribute("data-tab"); saveTab(); applyView(); });
  });
  if (search) {
    var timer;
    search.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () { state.query = search.value.trim().toLowerCase(); applyView(); }, 120);
    });
  }

  var pulse = setInterval(refresh, REFRESH_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { clearInterval(pulse); pulse = null; }
    else if (!pulse) { refresh(); pulse = setInterval(refresh, REFRESH_MS); }
  });
  applyView();
})();

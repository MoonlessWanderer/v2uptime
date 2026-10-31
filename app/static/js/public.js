/* Public status page: sort, search, accordion, one ping chart, 60s refresh. */
(function () {
  "use strict";

  var REFRESH_MS = 60000;
  var live = document.getElementById("live");
  var tooltip = document.getElementById("tooltip");
  var emptySearch = document.getElementById("empty-search");
  var search = document.getElementById("q");
  var state = { tab: "all", query: "", open: {}, ranges: {} };
  var cache = {};
  try {
    var saved = JSON.parse(localStorage.getItem("status-view") || "{}");
    if (saved.tab === "fastest" || saved.tab === "stable" || saved.tab === "all") state.tab = saved.tab;
  } catch (e) { /* ignore */ }

  function saveTab() {
    try { localStorage.setItem("status-view", JSON.stringify({ tab: state.tab })); } catch (e) { /* ignore */ }
  }

  function applyView() {
    document.body.classList.toggle("view-fastest", state.tab === "fastest");
    document.body.classList.toggle("view-stable", state.tab === "stable");
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

  function setRange(detail, range) {
    detail.querySelectorAll("[data-range]").forEach(function (el) {
      el.classList.toggle("is-active", el.getAttribute("data-range") === range);
    });
  }

  function drawChart(canvas, data) {
    if (!canvas || !canvas.isConnected || !window.MiniChart) return;
    window.MiniChart.render(canvas, {
      type: "line",
      values: data.points.map(function (p) { return p.latency; }),
      times: data.points.map(function (p) { return p.t; }),
      range: data.range,
      unit: "ms"
    });
  }

  function loadChart(id, range, canvas) {
    if (!canvas) return;
    var key = id + ":" + range;
    var box = canvas.closest(".chart");
    if (cache[key]) { drawChart(canvas, cache[key]); if (box) box.classList.remove("is-loading"); return; }
    if (box) box.classList.add("is-loading");
    fetch("/api/servers/" + encodeURIComponent(id) + "/history?range=" + encodeURIComponent(range))
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) { cache[key] = data; drawChart(canvas, data); })
      .catch(function () { /* keep the skeleton */ })
      .then(function () { if (box) box.classList.remove("is-loading"); });
  }

  function restore() {
    Object.keys(state.open).forEach(function (id) {
      if (!state.open[id]) return;
      var row = document.querySelector('#servers > li[data-id="' + id + '"]');
      if (!row) return;
      var btn = row.querySelector(".row-main");
      var detail = document.getElementById("detail-" + id);
      if (!btn || !detail) return;
      detail.hidden = false;
      btn.setAttribute("aria-expanded", "true");
      var range = state.ranges[id] || "24h";
      setRange(detail, range);
      loadChart(id, range, detail.querySelector("canvas"));
    });
  }

  var refreshing = false;
  function refresh() {
    if (refreshing || document.hidden) return;
    refreshing = true;
    var y = window.scrollY;
    var focusId = "";
    var active = document.activeElement;
    if (active && active.closest) {
      var focused = active.closest("#servers > li");
      if (focused) focusId = focused.getAttribute("data-id") || "";
    }
    fetch("/partials/live?range=24h", { headers: { "Accept": "text/html" } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        live.innerHTML = html;
        applyView();
        restore();
        window.scrollTo(0, y);
        if (focusId) {
          var btn = document.querySelector('#servers > li[data-id="' + focusId + '"] .row-main');
          if (btn) btn.focus();
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

  live.addEventListener("click", function (e) {
    var rangeBtn = e.target.closest(".chart-range [data-range]");
    if (rangeBtn) {
      var row = rangeBtn.closest("#servers > li");
      var detail = rangeBtn.closest(".row-detail");
      var id = row.getAttribute("data-id");
      var range = rangeBtn.getAttribute("data-range");
      state.ranges[id] = range;
      setRange(detail, range);
      loadChart(id, range, detail.querySelector("canvas"));
      return;
    }
    var btn = e.target.closest(".row-main");
    if (!btn) return;
    var item = btn.closest("#servers > li");
    var open = btn.getAttribute("aria-expanded") !== "true";
    var panel = document.getElementById(btn.getAttribute("aria-controls"));
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    panel.hidden = !open;
    state.open[item.getAttribute("data-id")] = open;
    if (open) loadChart(item.getAttribute("data-id"), state.ranges[item.getAttribute("data-id")] || "24h", panel.querySelector("canvas"));
  });

  function buckets() {
    var el = document.getElementById("bucket-meta");
    try { return el ? JSON.parse(el.textContent) : []; } catch (e) { return []; }
  }
  function showTip(bar) {
    var item = buckets()[+bar.getAttribute("data-i")] || {};
    var start = window.UptimeTime ? window.UptimeTime.formatTime(item.start) : "";
    var end = window.UptimeTime ? window.UptimeTime.formatTime(item.end) : "";
    var u = bar.getAttribute("data-u");
    tooltip.textContent = start + "–" + end + " · " + (u === "" ? "No data" : u + "%");
    tooltip.hidden = false;
    var r = bar.getBoundingClientRect();
    var t = tooltip.getBoundingClientRect();
    tooltip.style.left = Math.min(Math.max(8, r.left + r.width / 2 - t.width / 2), window.innerWidth - t.width - 8) + "px";
    tooltip.style.top = Math.max(8, r.top - t.height - 8) + "px";
  }
  function hideTip() { tooltip.hidden = true; }
  live.addEventListener("pointerover", function (e) {
    var bar = e.target.closest(".b");
    if (bar && e.pointerType === "mouse") showTip(bar);
  });
  live.addEventListener("pointerout", function (e) {
    if (e.pointerType === "mouse" && e.target.closest(".b")) hideTip();
  });
  window.addEventListener("scroll", hideTip, { passive: true });

  var pulse = setInterval(refresh, REFRESH_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { clearInterval(pulse); pulse = null; }
    else if (!pulse) { refresh(); pulse = setInterval(refresh, REFRESH_MS); }
  });
  applyView();
})();

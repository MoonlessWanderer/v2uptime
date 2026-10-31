/* Public status page: tabs, filters, search, live refresh, bar tooltips, detail modal. */
(function () {
  "use strict";

  var REFRESH_MS = 60000;
  var live = document.getElementById("live");
  var tooltip = document.getElementById("tooltip");
  var emptyFilter = document.getElementById("empty-filter");
  var search = document.getElementById("q");
  var modal = document.getElementById("modal");

  var state = { tab: "all", range: live.getAttribute("data-range") || "24h", status: "all", query: "", modalId: null, modalRange: "24h" };
  try {
    var saved = JSON.parse(localStorage.getItem("status-view") || "{}");
    if (saved.tab) state.tab = saved.tab;
  } catch (e) { /* ignore */ }

  function save() {
    try { localStorage.setItem("status-view", JSON.stringify({ tab: state.tab })); } catch (e) { /* ignore */ }
  }

  function setActive(selector, attr, value) {
    document.querySelectorAll(selector).forEach(function (el) {
      var on = el.getAttribute(attr) === value;
      el.classList.toggle("is-active", on);
      if (el.getAttribute("role") === "tab") el.setAttribute("aria-selected", on ? "true" : "false");
      if (el.hasAttribute("aria-pressed")) el.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  /* ---------------------------------------------------------- view */
  function applyView() {
    var container = document.getElementById("cards");
    document.body.classList.toggle("view-fastest", state.tab === "fastest");
    document.body.classList.toggle("view-stable", state.tab === "stable");
    setActive(".tab", "data-tab", state.tab);
    setActive(".filter", "data-filter", state.status);
    if (!container) { emptyFilter.hidden = true; return; }
    var cards = Array.prototype.slice.call(container.querySelectorAll(".card"));
    var key = state.tab === "fastest" ? "data-rank-fastest" : state.tab === "stable" ? "data-rank-stable" : "data-order";
    cards.sort(function (a, b) {
      var ra = parseInt(a.getAttribute(key), 10), rb = parseInt(b.getAttribute(key), 10);
      return (isNaN(ra) ? 1e9 : ra) - (isNaN(rb) ? 1e9 : rb);
    });
    var visible = 0;
    cards.forEach(function (card) {
      container.appendChild(card);
      var ranked = state.tab === "all" || card.getAttribute(key) !== "";
      var statusOk = state.status === "all" || card.getAttribute("data-status") === state.status;
      var queryOk = !state.query || card.getAttribute("data-search").indexOf(state.query) !== -1;
      var show = ranked && statusOk && queryOk;
      card.hidden = !show;
      if (show) visible++;
    });
    emptyFilter.hidden = visible > 0 || cards.length === 0;
  }

  /* ---------------------------------------------------------- refresh */
  var refreshing = false;
  function refresh() {
    if (refreshing) return;
    refreshing = true;
    live.classList.add("loading");
    fetch("/partials/live?range=" + encodeURIComponent(state.range), { headers: { "Accept": "text/html" } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) {
        live.innerHTML = html;
        live.setAttribute("data-range", state.range);
        applyView();
      })
      .catch(function () { /* keep the old content */ })
      .then(function () { refreshing = false; live.classList.remove("loading"); });
  }

  var timer = setInterval(refresh, REFRESH_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { clearInterval(timer); timer = null; }
    else if (!timer) { refresh(); timer = setInterval(refresh, REFRESH_MS); }
  });

  /* ---------------------------------------------------------- controls */
  document.querySelectorAll(".tab").forEach(function (el) {
    el.addEventListener("click", function () { state.tab = el.getAttribute("data-tab"); save(); applyView(); });
  });
  document.querySelectorAll(".filter").forEach(function (el) {
    el.addEventListener("click", function () { state.status = el.getAttribute("data-filter"); applyView(); });
  });
  document.querySelectorAll("#range-switch .seg").forEach(function (el) {
    el.addEventListener("click", function (e) {
      e.preventDefault();
      state.range = el.getAttribute("data-range");
      setActive("#range-switch .seg", "data-range", state.range);
      if (history.replaceState) history.replaceState(null, "", "?range=" + state.range);
      refresh();
    });
  });
  var searchTimer;
  search.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { state.query = search.value.trim().toLowerCase(); applyView(); }, 120);
  });

  /* ---------------------------------------------------------- bar tooltips */
  function bucketLabels() {
    var el = document.getElementById("bucket-labels");
    try { return el ? JSON.parse(el.textContent) : []; } catch (e) { return []; }
  }
  var hovered = null;
  function showTip(bar) {
    if (hovered) hovered.classList.remove("is-hover");
    hovered = bar;
    bar.classList.add("is-hover");
    var labels = bucketLabels();
    var u = bar.getAttribute("data-u"), n = parseInt(bar.getAttribute("data-n") || "0", 10);
    var lines = "<strong>" + (u === "" ? "بدون داده" : u + "% آپتایم") + "</strong>";
    lines += esc(labels[+bar.getAttribute("data-i")] || "");
    if (n > 0) lines += "<br>" + n + " قطعی";
    tooltip.innerHTML = lines;
    tooltip.hidden = false;
    var r = bar.getBoundingClientRect(), t = tooltip.getBoundingClientRect();
    var left = Math.min(Math.max(8, r.left + r.width / 2 - t.width / 2), window.innerWidth - t.width - 8);
    var top = r.top - t.height - 8;
    if (top < 8) top = r.bottom + 8;
    tooltip.style.left = left + "px";
    tooltip.style.top = top + "px";
  }
  function hideTip() {
    tooltip.hidden = true;
    if (hovered) { hovered.classList.remove("is-hover"); hovered = null; }
  }
  live.addEventListener("pointerover", function (e) {
    var bar = e.target.closest(".b");
    if (bar && e.pointerType === "mouse") showTip(bar);
  });
  live.addEventListener("pointerout", function (e) {
    if (e.pointerType === "mouse" && e.target.closest(".b")) hideTip();
  });
  window.addEventListener("scroll", hideTip, { passive: true });

  /* ---------------------------------------------------------- card clicks */
  live.addEventListener("click", function (e) {
    var bar = e.target.closest(".b");
    if (bar && e.pointerType !== "mouse" && (!hovered || hovered !== bar)) {
      // On touch, the first tap on a bar shows its tooltip instead of opening the card.
      showTip(bar);
      return;
    }
    hideTip();
    var card = e.target.closest(".card");
    if (card) openModal(card.getAttribute("data-id"));
  });
  live.addEventListener("keydown", function (e) {
    var card = e.target.closest(".card");
    if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openModal(card.getAttribute("data-id")); }
  });

  /* ---------------------------------------------------------- modal */
  var lastFocus = null;
  function openModal(id) {
    state.modalId = id;
    state.modalRange = state.range;
    lastFocus = document.activeElement;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    loadHistory();
    modal.querySelector(".modal-dialog [data-close]").focus();
  }
  function closeModal() {
    modal.hidden = true;
    document.body.style.overflow = "";
    state.modalId = null;
    if (lastFocus) lastFocus.focus();
  }
  modal.addEventListener("click", function (e) { if (e.target.closest("[data-close]")) closeModal(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !modal.hidden) closeModal(); });
  document.querySelectorAll("#m-range .seg").forEach(function (el) {
    el.addEventListener("click", function () { state.modalRange = el.getAttribute("data-range"); loadHistory(); });
  });

  function esc(text) {
    return String(text === null || text === undefined ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pct(v) { return v === null || v === undefined ? "—" : String(v) + "%"; }
  function ms(v) { return v === null || v === undefined ? "—" : Math.round(v) + " ms"; }

  function loadHistory() {
    var id = state.modalId, range = state.modalRange;
    setActive("#m-range .seg", "data-range", range);
    document.getElementById("m-body").classList.add("loading");
    fetch("/api/servers/" + encodeURIComponent(id) + "/history?range=" + range)
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        if (state.modalId !== id) return;
        var s = data.server;
        document.getElementById("m-title").textContent = (s.flag ? s.flag + " " : "") + s.name;
        document.getElementById("m-sub").textContent = s.protocol.toUpperCase() + " · " + s.transport + " · " + s.status_label;
        var sum = data.summary, inc = data.incident_stats;
        var stats = [
          ["آپتایم " + data.range_label, pct(sum.uptime)],
          ["میانگین پینگ", ms(sum.avg_latency)],
          ["کمترین / بیشترین", (sum.min_latency === null ? "—" : sum.min_latency + " / " + sum.max_latency) + " ms"],
          ["نوسان (jitter)", ms(sum.jitter)],
          ["تعداد قطعی", String(inc.count)],
          ["مجموع قطعی", inc.downtime_label],
          ["میانگین زمان برگشت", inc.mttr_label],
          ["امتیاز پایداری", s.stability === null ? "—" : String(s.stability)]
        ];
        document.getElementById("m-stats").innerHTML = stats.map(function (p) {
          return '<div class="m-stat"><span>' + esc(p[0]) + "</span><b>" + esc(p[1]) + "</b></div>";
        }).join("");
        var times = data.points.map(function (p) { return p.t; });
        window.MiniChart.render(document.getElementById("m-latency"), {
          type: "line", values: data.points.map(function (p) { return p.latency; }), times: times, range: data.range, unit: "ms"
        });
        window.MiniChart.render(document.getElementById("m-uptime"), {
          type: "bar", values: data.points.map(function (p) { return p.uptime; }), times: times, range: data.range, unit: "%", yMax: 100
        });
        var list = document.getElementById("m-incidents");
        list.innerHTML = data.incidents.length ? data.incidents.map(function (i) {
          return "<li><span>" + esc(i.start_label) + (i.end_label ? " تا " + esc(i.end_label) : "") + "</span>" +
            '<span class="' + (i.ongoing ? "ongoing" : "") + '">' + (i.ongoing ? "ادامه دارد · " : "") + esc(i.duration_label) +
            " · " + esc(i.error_label) + "</span></li>";
        }).join("") : '<li><span class="muted">در این بازه قطعی ثبت نشده است.</span></li>';
      })
      .catch(function () {
        document.getElementById("m-stats").innerHTML = '<p class="muted">دریافت اطلاعات ممکن نشد.</p>';
      })
      .then(function () { document.getElementById("m-body").classList.remove("loading"); });
  }

  applyView();
})();

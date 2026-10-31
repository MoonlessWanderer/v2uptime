/*
 * MiniChart — tiny dependency-free canvas charts (line + bar) with hover/tap tooltips.
 * MiniChart.render(canvas, {type: "line"|"bar", values: [n|null], labels: [str], yMax?, unit?, color?})
 */
(function () {
  "use strict";

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function uiFamily() { return getComputedStyle(document.body).fontFamily; }

  /* Axis labels and the canvas tooltip use the page font at --fs-xs. */
  function applyFont(chart, ctx) {
    chart.fontFamily = uiFamily();
    var size = cssVar("--fs-xs") || "12px";
    ctx.font = size + " " + chart.fontFamily;
  }

  function whenFontsReady(redraw) {
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(redraw);
  }

  function axisText(cfg, index) {
    var iso = cfg.times && cfg.times[index];
    if (iso && window.UptimeTime) {
      return (cfg.range === "7d" || cfg.range === "30d")
        ? window.UptimeTime.formatMonthDay(iso)
        : window.UptimeTime.formatTime(iso);
    }
    return (cfg.labels && cfg.labels[index]) || "";
  }

  function tipTime(cfg, index) {
    var iso = cfg.times && cfg.times[index];
    if (iso && window.UptimeTime) return window.UptimeTime.formatDateTime(iso);
    return (cfg.labels && cfg.labels[index]) || "";
  }

  function niceMax(value) {
    if (!(value > 0)) return 1;
    var power = Math.pow(10, Math.floor(Math.log10(value)));
    var n = value / power;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * power;
  }

  function Chart(canvas, cfg) {
    this.canvas = canvas;
    this.cfg = cfg;
    this.hover = -1;
    var self = this;
    this._move = function (e) { self.onPointer(e); };
    this._leave = function () { self.hover = -1; self.draw(); };
    canvas.addEventListener("pointermove", this._move);
    canvas.addEventListener("pointerdown", this._move);
    canvas.addEventListener("pointerleave", this._leave);
    this._resize = function () { self.draw(); };
    window.addEventListener("resize", this._resize);
    document.addEventListener("themechange", this._resize);
    this.draw();
    whenFontsReady(function () { if (self.canvas.isConnected) self.draw(); });
  }

  Chart.prototype.destroy = function () {
    this.canvas.removeEventListener("pointermove", this._move);
    this.canvas.removeEventListener("pointerdown", this._move);
    this.canvas.removeEventListener("pointerleave", this._leave);
    window.removeEventListener("resize", this._resize);
    document.removeEventListener("themechange", this._resize);
  };

  Chart.prototype.layout = function () {
    var rect = this.canvas.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    var ctx = this.canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = rect.width;
    this.h = rect.height;
    this.pad = { l: 42, r: 10, t: 10, b: 24 };
    this.plotW = Math.max(1, this.w - this.pad.l - this.pad.r);
    this.plotH = Math.max(1, this.h - this.pad.t - this.pad.b);
    return ctx;
  };

  Chart.prototype.xAt = function (i) {
    var n = this.cfg.values.length;
    if (this.cfg.type === "bar") return this.pad.l + (i + 0.5) * (this.plotW / n);
    return this.pad.l + (n <= 1 ? this.plotW / 2 : i * (this.plotW / (n - 1)));
  };

  Chart.prototype.yAt = function (v) {
    return this.pad.t + this.plotH * (1 - v / this.yMax);
  };

  Chart.prototype.onPointer = function (e) {
    var rect = this.canvas.getBoundingClientRect();
    var x = e.clientX - rect.left;
    var n = this.cfg.values.length;
    if (!n) return;
    var rel = (x - this.pad.l) / this.plotW;
    var idx = this.cfg.type === "bar" ? Math.floor(rel * n) : Math.round(rel * (n - 1));
    idx = Math.max(0, Math.min(n - 1, idx));
    if (idx !== this.hover) { this.hover = idx; this.draw(); }
  };

  Chart.prototype.draw = function () {
    var cfg = this.cfg;
    var ctx = this.layout();
    var values = cfg.values || [];
    var present = values.filter(function (v) { return v !== null && v !== undefined; });
    this.yMax = cfg.yMax || niceMax(Math.max.apply(null, present.concat([1])) * 1.1);
    var text = cssVar("--muted"), grid = cssVar("--border"), color = cfg.color || cssVar("--accent");
    ctx.clearRect(0, 0, this.w, this.h);
    applyFont(this, ctx);
    ctx.fillStyle = text;
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;

    // grid + y labels
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (var g = 0; g <= 4; g++) {
      var gv = this.yMax * g / 4, gy = this.yAt(gv);
      ctx.beginPath(); ctx.moveTo(this.pad.l, gy); ctx.lineTo(this.w - this.pad.r, gy); ctx.stroke();
      ctx.fillText(String(Math.round(gv)), this.pad.l - 6, gy);
    }
    // x labels (first / middle / last): 24h is HH:mm, longer ranges are MMM DD
    ctx.textBaseline = "top";
    [0, Math.floor((values.length - 1) / 2), values.length - 1].forEach(function (i, k) {
      if (i < 0) return;
      var text = axisText(cfg, i);
      if (!text) return;
      ctx.textAlign = k === 0 ? "left" : k === 2 ? "right" : "center";
      ctx.fillText(text, this.xAt(i), this.h - this.pad.b + 6);
    }, this);

    if (!present.length) {
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText("No data", this.pad.l + this.plotW / 2, this.pad.t + this.plotH / 2);
      return;
    }

    if (cfg.type === "bar") {
      var bw = Math.max(1, this.plotW / values.length - 1);
      for (var b = 0; b < values.length; b++) {
        var bv = values[b];
        if (bv === null || bv === undefined) continue;
        ctx.fillStyle = bv >= 99 ? cssVar("--lv-up") : bv >= 95 ? cssVar("--lv-minor") : bv >= 80 ? cssVar("--lv-major") : cssVar("--lv-down");
        var top = this.yAt(bv);
        ctx.globalAlpha = this.hover === -1 || this.hover === b ? 1 : 0.55;
        ctx.fillRect(this.xAt(b) - bw / 2, top, bw, this.pad.t + this.plotH - top);
      }
      ctx.globalAlpha = 1;
    } else {
      ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.lineJoin = "round";
      var started = false;
      ctx.beginPath();
      for (var i = 0; i < values.length; i++) {
        var v = values[i];
        if (v === null || v === undefined) { started = false; continue; }
        var px = this.xAt(i), py = this.yAt(v);
        if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
      }
      ctx.stroke();
      // soft area fill
      ctx.globalAlpha = 0.08; ctx.fillStyle = color;
      for (var j = 0; j < values.length; j++) {
        if (values[j] === null || values[j] === undefined) continue;
        var x0 = this.xAt(j), w0 = values.length > 1 ? this.plotW / (values.length - 1) : 2;
        ctx.fillRect(x0 - w0 / 2, this.yAt(values[j]), w0, this.pad.t + this.plotH - this.yAt(values[j]));
      }
      ctx.globalAlpha = 1;
    }

    // hover tooltip
    if (this.hover >= 0) {
      var hv = values[this.hover], hx = this.xAt(this.hover);
      ctx.strokeStyle = text; ctx.globalAlpha = 0.4;
      ctx.beginPath(); ctx.moveTo(hx, this.pad.t); ctx.lineTo(hx, this.pad.t + this.plotH); ctx.stroke();
      ctx.globalAlpha = 1;
      if (cfg.type !== "bar" && hv !== null && hv !== undefined) {
        ctx.fillStyle = color; ctx.beginPath(); ctx.arc(hx, this.yAt(hv), 3.5, 0, Math.PI * 2); ctx.fill();
      }
      var line1 = tipTime(cfg, this.hover);
      var line2 = hv === null || hv === undefined ? "No data" : (cfg.unit === "%" ? hv.toFixed(2).replace(/\.?0+$/, "") : Math.round(hv)) + " " + (cfg.unit || "");
      applyFont(this, ctx);
      var tw = Math.max(ctx.measureText(line1).width, ctx.measureText(line2).width) + 16;
      var tx = Math.min(Math.max(hx - tw / 2, 2), this.w - tw - 2);
      ctx.fillStyle = cssVar("--text");
      ctx.globalAlpha = 0.92;
      ctx.fillRect(tx, this.pad.t, tw, 40);
      ctx.globalAlpha = 1;
      ctx.fillStyle = cssVar("--bg");
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      ctx.fillText(line1, tx + tw / 2, this.pad.t + 4);
      ctx.fillText(line2, tx + tw / 2, this.pad.t + 21);
    }
  };

  /*
   * Timeline: the public card's ping chart. x is time, from bars[0].start to the end of the last
   * bar, with no horizontal padding, so it lines up with the status bar drawn above it (same
   * width, same time window). y labels sit inside the plot; time labels go underneath.
   * Pointer handling (cursor line, tooltip) is done by public.js for the bar and chart together.
   */
  var LABEL_H = 18;

  function niceStep(max) {
    var raw = max / 3, power = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / power;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * power;
  }

  function timeTicks(bars, range, start, end) {
    var T = window.UptimeTime, ticks = [];
    if (!T || !bars.length) return ticks;
    if (range === "1h") {
      ticks.push({ t: start, text: T.axisLabel("axis_1h"), align: "left" });
      var mid = bars[Math.floor((bars.length - 1) / 2)];
      if (mid) ticks.push({ t: Date.parse(mid.start), text: T.formatTime(mid.start), align: "center" });
      ticks.push({ t: end, text: T.axisLabel("axis_now"), align: "right" });
    } else if (range === "24h") {
      ticks.push({ t: start, text: T.axisLabel("axis_24h"), align: "left" });
      bars.forEach(function (b) {
        var hm = T.formatTime(b.start);
        if (/^(00|06|12|18):00$/.test(hm)) ticks.push({ t: Date.parse(b.start), text: hm, align: "center" });
      });
      ticks.push({ t: end, text: T.axisLabel("axis_now"), align: "right" });
    } else if (range === "7d") {
      var midnights = bars.filter(function (b) { return T.formatTime(b.start) === "00:00"; });
      for (var k = midnights.length - 1; k >= 0; k -= 2) {
        ticks.push({ t: Date.parse(midnights[k].start), text: T.formatMonthDay(midnights[k].start), align: "center" });
      }
    } else {
      ticks.push({ t: end, text: T.axisLabel("axis_today"), align: "right" });
      for (var d = bars.length - 9; d >= 0; d -= 8) {
        var mid = (Date.parse(bars[d].start) + Date.parse(bars[d].end)) / 2;
        ticks.push({ t: mid, text: T.formatMonthDay(bars[d].start), align: "center" });
      }
    }
    // edge labels ("24h ago", "now", "today") win over a middle label that would collide
    return ticks.sort(function (a, b) { return (a.align === "center") - (b.align === "center"); });
  }

  function Timeline(canvas, cfg) {
    this.canvas = canvas;
    this.cfg = cfg;
    var self = this;
    this._redraw = function () { if (canvas.isConnected) self.draw(); else self.destroy(); };
    window.addEventListener("resize", this._redraw);
    document.addEventListener("themechange", this._redraw);
    this.draw();
    whenFontsReady(this._redraw);
  }

  Timeline.prototype.destroy = function () {
    window.removeEventListener("resize", this._redraw);
    document.removeEventListener("themechange", this._redraw);
  };

  Timeline.prototype.draw = function () {
    var cfg = this.cfg, canvas = this.canvas, bars = cfg.bars || [];
    var rect = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var w = rect.width, h = rect.height, plotH = Math.max(1, h - LABEL_H), top = 14;  // room for the top y label
    ctx.clearRect(0, 0, w, h);
    if (!bars.length) return;
    var start = Date.parse(bars[0].start), end = Date.parse(bars[bars.length - 1].end), span = end - start;
    var xAt = function (t) { return (t - start) / span * w; };

    var pts = (cfg.points || []).map(function (p) { return { t: Date.parse(p.t), v: p.latency }; });
    var half = pts.length > 1 ? (pts[1].t - pts[0].t) / 2 : 0;
    pts = pts.map(function (p) { return { t: p.t + half, v: p.v }; })  // plot each bucket at its middle
      .filter(function (p) { return p.t >= start && p.t <= end; });
    var values = pts.filter(function (p) { return p.v !== null && p.v !== undefined; }).map(function (p) { return p.v; });
    var max = values.length ? Math.max.apply(null, values) : 100;
    var step = niceStep(max * 1.15 || 1), yMax = Math.ceil(max * 1.15 / step) * step || step;
    var yAt = function (v) { return top + (plotH - top) * (1 - v / yMax); };

    var muted = cssVar("--muted"), grid = cssVar("--border"), color = cfg.color || cssVar("--accent");
    applyFont(this, ctx);
    ctx.lineWidth = 1;
    for (var g = step; g <= yMax + 0.001; g += step) {  // grid lines with their label inside, above the line
      var gy = Math.round(yAt(g)) + 0.5;
      ctx.strokeStyle = grid; ctx.globalAlpha = 0.7;
      ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke();
      ctx.globalAlpha = 0.6; ctx.fillStyle = muted; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
      ctx.fillText(String(Math.round(g)), 2, gy - 2);
    }
    ctx.globalAlpha = 1;
    ctx.strokeStyle = grid;
    ctx.beginPath(); ctx.moveTo(0, plotH + 0.5); ctx.lineTo(w, plotH + 0.5); ctx.stroke();

    // line and soft area, broken where there is no ping (outages)
    var runs = [], run = [];
    pts.forEach(function (p) {
      if (p.v === null || p.v === undefined) { if (run.length) runs.push(run); run = []; }
      else run.push({ x: xAt(p.t), y: yAt(p.v) });
    });
    if (run.length) runs.push(run);
    runs.forEach(function (r) {
      ctx.beginPath();
      r.forEach(function (p, i) { if (i) ctx.lineTo(p.x, p.y); else ctx.moveTo(p.x, p.y); });
      ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.lineJoin = "round"; ctx.stroke();
      ctx.lineTo(r[r.length - 1].x, plotH); ctx.lineTo(r[0].x, plotH); ctx.closePath();
      ctx.globalAlpha = 0.14; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
    });
    ctx.fillStyle = color;
    runs.forEach(function (r) {
      r.forEach(function (p) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 2.25, 0, Math.PI * 2);
        ctx.fill();
      });
    });
    if (!values.length) {
      ctx.fillStyle = muted; ctx.textAlign = "center"; ctx.textBaseline = "middle"; applyFont(this, ctx);
      ctx.fillText("No ping data", w / 2, plotH / 2);
    }

    // time labels under the plot; drop the ones that would collide
    applyFont(this, ctx); ctx.fillStyle = muted; ctx.textBaseline = "top";
    var placed = [];
    timeTicks(bars, cfg.range, start, end).forEach(function (tick) {
      var tw = ctx.measureText(tick.text).width, x = xAt(tick.t);
      var left = tick.align === "left" ? x : tick.align === "right" ? x - tw : Math.min(Math.max(x - tw / 2, 0), w - tw);
      var clash = placed.some(function (p) { return left < p.right + 24 && left + tw + 24 > p.left; });
      if (clash) return;
      placed.push({ left: left, right: left + tw });
      ctx.textAlign = "left";
      ctx.fillText(tick.text, left, plotH + 4);
    });
  };

  window.MiniChart = {
    render: function (canvas, cfg) {
      if (canvas._chart) canvas._chart.destroy();
      canvas._chart = new Chart(canvas, cfg);
      return canvas._chart;
    },
    timeline: function (canvas, cfg) {
      if (canvas._chart) canvas._chart.destroy();
      canvas._chart = new Timeline(canvas, cfg);
      return canvas._chart;
    }
  };
})();

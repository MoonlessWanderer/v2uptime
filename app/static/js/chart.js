/*
 * MiniChart — tiny dependency-free canvas charts (line + bar) with hover/tap tooltips.
 * MiniChart.render(canvas, {type: "line"|"bar", values: [n|null], labels: [str], yMax?, unit?, color?})
 */
(function () {
  "use strict";

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

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
    ctx.font = "11px system-ui, Segoe UI, sans-serif";
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
      ctx.font = "12px system-ui, Segoe UI, sans-serif";
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

  window.MiniChart = {
    render: function (canvas, cfg) {
      if (canvas._chart) canvas._chart.destroy();
      canvas._chart = new Chart(canvas, cfg);
      return canvas._chart;
    }
  };
})();

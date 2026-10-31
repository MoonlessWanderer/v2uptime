/*
 * Sole client-side time formatter. Reads data-tz / data-tz-label from <body>
 * and builds strings from Intl parts so the result does not follow the browser locale.
 */
(function () {
  "use strict";

  var EMPTY = "—";

  function zone() {
    return (document.body && document.body.getAttribute("data-tz")) || "UTC";
  }

  function toDate(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number") return new Date(value * 1000);
    if (typeof value === "string" && /^-?\d+$/.test(value)) return new Date(Number(value) * 1000);
    var date = new Date(value);
    return isNaN(date.getTime()) ? null : date;
  }

  function parts(date, options) {
    var fmt = new Intl.DateTimeFormat("en-US", Object.assign({
      timeZone: zone(),
      hourCycle: "h23",
      numberingSystem: "latn"
    }, options));
    var out = {};
    fmt.formatToParts(date).forEach(function (part) {
      if (part.type !== "literal") out[part.type] = part.value;
    });
    return out;
  }

  function pad(value) {
    var text = String(value);
    return text.length >= 2 ? text : ("0" + text);
  }

  function hourOf(part) {
    return part.hour === "24" ? "00" : pad(part.hour);
  }

  function formatTime(value) {
    var date = toDate(value);
    if (!date) return EMPTY;
    var part = parts(date, { hour: "2-digit", minute: "2-digit" });
    return hourOf(part) + ":" + pad(part.minute);
  }

  function formatDateTime(value, withSeconds) {
    var date = toDate(value);
    if (!date) return EMPTY;
    var options = {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit"
    };
    if (withSeconds) options.second = "2-digit";
    var part = parts(date, options);
    var clock = hourOf(part) + ":" + pad(part.minute);
    if (withSeconds) clock += ":" + pad(part.second || "0");
    return part.year + "-" + pad(part.month) + "-" + pad(part.day) + " " + clock;
  }

  function formatMonthDay(value) {
    var date = toDate(value);
    if (!date) return EMPTY;
    var part = parts(date, { month: "short", day: "2-digit" });
    return part.month + " " + pad(part.day);
  }

  /* English words. Persian (and any override) comes from #i18n, built from the same dictionary. */
  var EN = {
    dur_d: "{n}d", dur_h: "{n}h", dur_m: "{n}m", dur_s: "{n}s", dur_join: " ",
    ago_just: "just now", ago_m: "{n}m ago", ago_h: "{n}h ago", ago_d: "{n}d ago",
    axis_24h: "24h ago", axis_now: "now", axis_today: "today"
  };

  function pageLang(explicit) {
    if (explicit === "fa" || explicit === "en") return explicit;
    var root = document.documentElement;
    return root && root.lang === "fa" ? "fa" : "en";
  }

  function pack() {
    if (pack.cache) return pack.cache;
    var el = document.getElementById ? document.getElementById("i18n") : null;
    try { pack.cache = el ? JSON.parse(el.textContent) : {}; }
    catch (e) { pack.cache = {}; }
    return pack.cache;
  }

  function word(key, lang) {
    var packed = pack()[key];
    if (pageLang(lang) === "fa" && packed) return packed;
    if (EN[key]) return EN[key];
    return packed || key;
  }

  function piece(count, suffix, lang) {
    return word("dur_" + suffix, lang).split("{n}").join(String(count));
  }

  /* Largest non-zero unit, plus the next smaller one when it is not zero (same as fmt_duration).
     coarse drops seconds once the span is at least a minute ("47m 2s" -> "47m").
     lang changes the unit words only. */
  var UNITS = [["d", 86400], ["h", 3600], ["m", 60], ["s", 1]];
  function formatDuration(seconds, coarse, lang) {
    if (seconds === null || seconds === undefined || seconds === "") return EMPTY;
    var rem = Math.floor(Number(seconds));
    if (!isFinite(rem)) return EMPTY;
    if (rem < 0) rem = 0;
    var chosen = pageLang(lang);
    var counts = UNITS.map(function (unit) {
      var count = Math.floor(rem / unit[1]);
      rem -= count * unit[1];
      return count;
    });
    for (var i = 0; i < counts.length; i++) {
      if (!counts[i]) continue;
      var text = piece(counts[i], UNITS[i][0], chosen);
      if (i + 1 < counts.length && counts[i + 1] && !(coarse && UNITS[i + 1][0] === "s")) {
        text += word("dur_join", chosen) + piece(counts[i + 1], UNITS[i + 1][0], chosen);
      }
      return text;
    }
    return piece(0, "s", chosen);
  }

  function formatAgo(value, nowMs, lang) {
    var date = toDate(value);
    if (!date) return EMPTY;
    var chosen = pageLang(lang);
    var delta = Math.floor(((nowMs || Date.now()) - date.getTime()) / 1000);
    if (delta < 60) return word("ago_just", chosen);
    if (delta < 3600) return word("ago_m", chosen).split("{n}").join(String(Math.floor(delta / 60)));
    if (delta < 86400) return word("ago_h", chosen).split("{n}").join(String(Math.floor(delta / 3600)));
    return word("ago_d", chosen).split("{n}").join(String(Math.floor(delta / 86400)));
  }

  function axisLabel(kind, lang) {
    return word(kind, lang);
  }

  function tick() {
    document.querySelectorAll("time[data-ago]").forEach(function (el) {
      var text = formatAgo(el.getAttribute("datetime"));
      if (el.textContent !== text) el.textContent = text;
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", tick);
  else tick();
  setInterval(tick, 60000);

  window.UptimeTime = {
    formatTime: formatTime,
    formatDateTime: formatDateTime,
    formatMonthDay: formatMonthDay,
    formatDuration: formatDuration,
    formatAgo: formatAgo,
    axisLabel: axisLabel,
    word: word
  };
})();

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

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || seconds === "") return EMPTY;
    var total = Math.floor(Number(seconds));
    if (!isFinite(total)) return EMPTY;
    if (total < 0) total = 0;
    var days = Math.floor(total / 86400);
    var rem = total % 86400;
    var hours = Math.floor(rem / 3600);
    rem %= 3600;
    var minutes = Math.floor(rem / 60);
    var secs = rem % 60;
    var units = [];
    if (days) units.push(days + "d");
    if (hours) units.push(hours + "h");
    if (minutes) units.push(minutes + "m");
    if (secs || !units.length) units.push(secs + "s");
    return units.slice(0, 2).join(" ");
  }

  function formatAgo(value, nowMs) {
    var date = toDate(value);
    if (!date) return EMPTY;
    var delta = Math.floor(((nowMs || Date.now()) - date.getTime()) / 1000);
    if (delta < 60) return "just now";
    if (delta < 3600) return Math.floor(delta / 60) + "m ago";
    if (delta < 86400) return Math.floor(delta / 3600) + "h ago";
    return Math.floor(delta / 86400) + "d ago";
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
    formatAgo: formatAgo
  };
})();

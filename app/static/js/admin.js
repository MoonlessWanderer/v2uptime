/* Admin panel helpers: CSRF fetch, confirm, toggles, test-now, ordering, Telegram test, charts. */
(function () {
  "use strict";

  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var base = (document.querySelector('meta[name="admin-base"]') || {}).content || "";
  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {}),
      credentials: "same-origin"
    }).then(function (r) {
      if (r.status === 401) { location.reload(); throw new Error("auth"); }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function toast(text) {
    var el = document.createElement("div");
    el.className = "toast";
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 3000);
  }

  function esc(text) {
    return String(text === null || text === undefined ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* navigation (mobile) */
  var nav = document.getElementById("sidenav");
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-nav-toggle]")) { nav.classList.toggle("is-open"); return; }
    if (nav && nav.classList.contains("is-open") && !e.target.closest("#sidenav")) nav.classList.remove("is-open");
  });

  /* confirmation for destructive forms */
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  /* toggles (active / public) */
  document.querySelectorAll("[data-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.disabled = true;
      post(base + "/servers/" + btn.getAttribute("data-id") + "/toggle", { flag: btn.getAttribute("data-toggle") })
        .then(function () { location.reload(); })
        .catch(function () { btn.disabled = false; toast("Could not save"); });
    });
  });

  /* test now */
  document.querySelectorAll("[data-test]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.getAttribute("data-test");
      var out = document.querySelector('[data-result-for="' + id + '"]');
      btn.disabled = true;
      out.textContent = "Testing…";
      out.className = "test-result muted";
      post(base + "/servers/" + id + "/test")
        .then(function (r) {
          out.className = "test-result pill " + (r.ok ? "pill-ok" : "pill-bad");
          out.textContent = r.ok
            ? "OK · " + r.latency_ms + " ms" + (r.connect_ms !== null && r.connect_ms !== undefined ? " (connect " + r.connect_ms + " ms)" : "")
            : "Failed · " + r.error;
          if (!r.ok && r.detail) out.title = r.detail;
        })
        .catch(function () { out.textContent = "Error"; })
        .then(function () { btn.disabled = false; });
    });
  });

  /* ordering: drag & drop + up/down buttons */
  var table = document.getElementById("server-table");
  if (table) {
    var tbody = table.querySelector("tbody");
    var dragged = null;
    var saveOrder = function () {
      var ids = Array.prototype.map.call(tbody.querySelectorAll("tr[data-id]"), function (tr) { return tr.getAttribute("data-id"); });
      post(base + "/servers/reorder", { ids: ids }).then(function () { toast("Order saved"); })
        .catch(function () { toast("Could not save the order"); });
    };
    tbody.addEventListener("dragstart", function (e) {
      if (e.target.closest("input, button, a")) { e.preventDefault(); return; }
      dragged = e.target.closest("tr[data-id]");
      if (!dragged) return;
      dragged.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    tbody.addEventListener("dragover", function (e) {
      var target = e.target.closest("tr[data-id]");
      if (!dragged || !target || target === dragged) return;
      e.preventDefault();
      tbody.querySelectorAll(".drop-target").forEach(function (el) { el.classList.remove("drop-target"); });
      target.classList.add("drop-target");
    });
    tbody.addEventListener("drop", function (e) {
      var target = e.target.closest("tr[data-id]");
      if (!dragged || !target || target === dragged) return;
      e.preventDefault();
      var rect = target.getBoundingClientRect();
      tbody.insertBefore(dragged, e.clientY > rect.top + rect.height / 2 ? target.nextSibling : target);
      saveOrder();
    });
    tbody.addEventListener("dragend", function () {
      if (dragged) dragged.classList.remove("is-dragging");
      tbody.querySelectorAll(".drop-target").forEach(function (el) { el.classList.remove("drop-target"); });
      dragged = null;
    });
    tbody.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-move]");
      if (!btn) return;
      var row = btn.closest("tr");
      if (btn.getAttribute("data-move") === "up" && row.previousElementSibling) tbody.insertBefore(row, row.previousElementSibling);
      else if (btn.getAttribute("data-move") === "down" && row.nextElementSibling) tbody.insertBefore(row.nextElementSibling, row);
      else return;
      saveOrder();
    });

    var bar = document.getElementById("bulk-bar");
    var count = document.getElementById("bulk-count");
    var checkAll = document.getElementById("check-all");
    var selected = function () {
      return Array.prototype.map.call(tbody.querySelectorAll("tr[data-id]:not([hidden]) .row-check:checked"), function (box) {
        return Number(box.getAttribute("data-id"));
      });
    };
    var refreshBar = function () {
      var ids = selected();
      if (bar) bar.hidden = ids.length === 0;
      if (count) count.textContent = ids.length + " selected";
      if (checkAll) {
        var boxes = tbody.querySelectorAll("tr[data-id]:not([hidden]) .row-check");
        checkAll.checked = boxes.length > 0 && ids.length === boxes.length;
        checkAll.indeterminate = ids.length > 0 && ids.length < boxes.length;
      }
    };
    tbody.addEventListener("change", function (e) {
      if (e.target.classList.contains("row-check")) refreshBar();
    });
    if (checkAll) {
      checkAll.addEventListener("change", function () {
        tbody.querySelectorAll("tr[data-id]:not([hidden]) .row-check").forEach(function (box) {
          box.checked = checkAll.checked;
        });
        refreshBar();
      });
    }
    if (bar) {
      bar.addEventListener("click", function (e) {
        var action = e.target.closest("[data-bulk]");
        if (!action) return;
        var kind = action.getAttribute("data-bulk");
        if (kind === "clear") {
          tbody.querySelectorAll(".row-check").forEach(function (box) { box.checked = false; });
          refreshBar();
          return;
        }
        var ids = selected();
        if (!ids.length) return;
        if (kind === "delete") {
          if (ids.length > 20) {
            var typed = window.prompt("Delete " + ids.length + " servers and their history? This cannot be undone. Type " + ids.length + " to confirm.");
            if (typed !== String(ids.length)) return;
          } else if (!window.confirm("Delete " + ids.length + " servers and their history? This cannot be undone.")) {
            return;
          }
        }
        action.disabled = true;
        post(base + "/servers/bulk", { action: kind, ids: ids })
          .then(function () { location.reload(); })
          .catch(function () { action.disabled = false; toast("Could not update the servers"); });
      });
    }
  }

  /* telegram test */
  var tgButton = document.getElementById("telegram-test");
  if (tgButton) {
    tgButton.addEventListener("click", function () {
      var out = document.getElementById("telegram-test-result");
      tgButton.disabled = true;
      out.innerHTML = '<p class="muted">Sending…</p>';
      post(tgButton.getAttribute("data-url"))
        .then(function (r) {
          if (r.error) { out.innerHTML = '<div class="alert alert-error">' + esc(r.error) + "</div>"; return; }
          out.innerHTML = r.results.map(function (x) {
            return x.ok
              ? '<div class="alert alert-success">✅ ' + esc(x.chat_id) + ": sent — " + esc(x.via) + "</div>"
              : '<div class="alert alert-error">❌ ' + esc(x.chat_id) + ": " + esc(x.error) +
                (x.tried && x.tried.length ? "<br><small>" + esc(x.tried.join(" | ")) + "</small>" : "") + "</div>";
          }).join("");
        })
        .catch(function () { out.innerHTML = '<div class="alert alert-error">Could not reach the panel</div>'; })
        .then(function () { tgButton.disabled = false; });
    });
  }

  /* add-server preview */
  var linkBox = document.getElementById("links");
  var preview = document.getElementById("link-preview");
  if (linkBox && preview) {
    var previewTimer;
    linkBox.addEventListener("input", function () {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(function () {
        var text = linkBox.value.trim();
        if (!text) { preview.hidden = true; preview.innerHTML = ""; return; }
        post(base + "/servers/preview", { links: linkBox.value }).then(function (r) {
          var html = "";
          if (r.error) html += "<p class=\"alert alert-error\">" + esc(r.error) + "</p>";
          if (r.subscriptions && r.subscriptions.length) {
            html += "<ul>";
            r.subscriptions.forEach(function (item) {
              if (item.error) {
                html += "<li>" + esc(item.host) + ": could not fetch: " + esc(item.error) + "</li>";
              } else {
                html += "<li>" + esc(item.url) + " → " + item.configs + " configs (" + item.new +
                  " new, " + item.duplicates + " duplicates, " + item.invalid + " invalid)</li>";
              }
            });
            html += "</ul>";
          }
          if (r.valid && r.valid.length) {
            html += "<p><strong>Valid (" + r.valid.length + ")</strong></p><ul>";
            r.valid.forEach(function (item) {
              html += "<li><bdi>" + esc(item.name) + "</bdi> · " + esc(item.protocol) + "</li>";
            });
            html += "</ul>";
          }
          if (r.invalid && r.invalid.length) {
            html += "<p><strong>Invalid (" + r.invalid.length + ")</strong></p><ul>";
            r.invalid.forEach(function (item) {
              html += "<li>Line " + esc(item.line) + ": " + esc(item.message) + "</li>";
            });
            html += "</ul>";
          }
          if (!html) html = "<p class=\"muted\">Nothing to preview yet.</p>";
          preview.innerHTML = html;
          preview.hidden = false;
        }).catch(function () { preview.hidden = true; });
      }, 250);
    });
  }

  /* maintenance: update and restart the panel, then follow it until it is back */
  var maint = document.getElementById("maintenance");
  if (maint) {
    var statusUrl = maint.getAttribute("data-status-url");
    var bootedAt = maint.getAttribute("data-started");
    var buttons = maint.querySelectorAll("[data-maintenance]");
    buttons.forEach(function (b) { b.setAttribute("data-off", b.disabled ? "1" : ""); });
    var lock = function () { buttons.forEach(function (b) { b.disabled = true; }); };
    var unlock = function () { buttons.forEach(function (b) { b.disabled = b.getAttribute("data-off") === "1"; }); };
    var note = function (id, text, kind) {
      var el = document.getElementById(id);
      el.textContent = text;
      el.className = "status-note" + (kind ? " is-" + kind : "");
    };
    var send = function (url) {
      return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrf },
        body: "{}",
        credentials: "same-origin"
      }).then(function (r) {
        if (r.status === 401) { location.reload(); throw new Error("auth"); }
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
          return data;
        });
      });
    };
    var status = function () {
      return fetch(statusUrl, { headers: { "Accept": "application/json" }, credentials: "same-origin", cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
    };
    /* step(elapsedMs) resolves true when done; a failed request means the panel is down for now */
    var follow = function (step, onDown) {
      var started = Date.now();
      var tick = function () {
        var elapsed = Date.now() - started;
        status().then(function (s) { return step(s, elapsed); }, function () { return onDown(elapsed); })
          .then(function (done) { if (!done) setTimeout(tick, 2000); });
      };
      setTimeout(tick, 1500);
    };

    var watchUpdate = function (since) {
      lock();
      note("update-note", "Waiting for the updater to start…");
      follow(function (s, elapsed) {
        var u = s.update;
        var current = u.started_at !== null && u.started_at >= since - 5;
        if (current && u.log) {
          var logBox = document.getElementById("update-log");
          logBox.textContent = u.log;
          logBox.closest("details").open = true;
        }
        if (current && u.state === "running") {
          note("update-note", "Updating… The panel restarts near the end.");
          return false;
        }
        if (current && u.state) {
          note("update-note", u.message, u.state === "ok" ? "ok" : "bad");
          if (u.state === "ok") setTimeout(function () { location.reload(); }, 2500);
          else unlock();
          return true;
        }
        if (elapsed > 60000) {
          note("update-note", "The updater has not started. On the server, check: systemctl status v2uptime-control.path", "bad");
        }
        return elapsed > 45 * 60000;
      }, function (elapsed) {
        note("update-note", "The panel is restarting with the new version…");
        return elapsed > 45 * 60000;
      });
    };

    var watchRestart = function () {
      lock();
      note("restart-note", "Restarting…");
      var failed = function () {
        note("restart-note", "The panel has not come back yet. On the server: sudo v2uptime, then 8 (Service status).", "bad");
        return true;
      };
      follow(function (s, elapsed) {
        if (String(s.process_started_at) !== bootedAt) {
          note("restart-note", "The panel restarted.", "ok");
          setTimeout(function () { location.reload(); }, 1200);
          return true;
        }
        return elapsed > 120000 && failed();
      }, function (elapsed) { return elapsed > 120000 && failed(); });
    };

    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!window.confirm(btn.getAttribute("data-confirm"))) return;
        var kind = btn.getAttribute("data-maintenance");
        lock();
        send(btn.getAttribute("data-url")).then(function (r) {
          if (kind === "update") watchUpdate(r.requested_at); else watchRestart();
        }).catch(function (err) {
          if (err.message === "auth") return;
          note(kind + "-note", err.message, "bad");
          unlock();
        });
      });
    });
    if (maint.getAttribute("data-busy") === "1") watchUpdate(parseInt(maint.getAttribute("data-since"), 10));
  }

  /* server detail charts */
  var history = document.querySelector("[data-history]");
  if (history && window.MiniChart) {
    var load = function (range) {
      history.querySelectorAll(".seg").forEach(function (s) { s.classList.toggle("is-active", s.getAttribute("data-range") === range); });
      fetch(history.getAttribute("data-history") + "?range=" + range, { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var times = data.points.map(function (p) { return p.t; });
          MiniChart.render(history.querySelector('[data-chart="latency"]'), {
            type: "line", values: data.points.map(function (p) { return p.latency; }), times: times, range: range, unit: "ms"
          });
          MiniChart.render(history.querySelector('[data-chart="uptime"]'), {
            type: "bar", values: data.points.map(function (p) { return p.uptime; }), times: times, range: range, unit: "%", yMax: 100
          });
        });
    };
    history.querySelectorAll(".seg").forEach(function (s) {
      s.addEventListener("click", function () { load(s.getAttribute("data-range")); });
    });
    load("24h");
  }
})();

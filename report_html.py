"""Shared HTML report generator.

Both quick_scan.py and deep_audit.py already write a plain-text
{site_name}.txt report - kept as-is because it's easy to paste into an
email. This module writes a companion {site_name}.html next to it: the
same findings, as a single self-contained sortable/filterable table, for
digging through a large site's results without scrolling a text file.

No build step, no external assets - the page is one file with embedded
CSS/JS so it opens directly from disk (file://) or off a plain static
file server.
"""

import json
from datetime import datetime
from pathlib import Path

# Category -> severity, used only to color-code rows/chips. Matches the
# same "broken/security first, legacy/sensitive second" priority used in
# the text reports.
CRITICAL_CATEGORIES = {
    "Broken Link", "Broken Resource", "Security Watch",
    "Directory Listing", "Possible Secret", "Exposed Path",
}
WARNING_CATEGORIES = {
    "Legacy File", "Sensitive File", "Missing Security Header",
    "Risky Parameter", "Flagged File", "Mixed Content", "Cookie Flag",
}
# Everything else (Rate Limited, Public/Low-Confidence Key, Version
# Disclosure, Third-Party Script, Tabnabbing Link) falls through to the
# muted "none" severity - informational, present but not alarming.


def severity_for(category):
    base = category.split(" (", 1)[0]  # "Broken Resource (image)" -> "Broken Resource"
    if base in CRITICAL_CATEGORIES:
        return "critical"
    if base in WARNING_CATEGORIES:
        return "warning"
    return "none"


def make_row(category, detail, url, found_on=""):
    return {
        "category": category,
        "severity": severity_for(category),
        "detail": "" if detail is None else str(detail),
        "url": url or "",
        "found_on": found_on or "",
    }


def write_html_report(rows, meta, outdir, site_name):
    """meta: dict with at least title, subtitle, generated, partial (bool),
    and a list of (label, value) summary_lines to show under the header."""
    path = Path(outdir) / f"{site_name}.html"
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {"meta": meta, "rows": rows}
    # Escape "</" so a URL/finding value can never break out of the
    # <script type="application/json"> block it's embedded in. Rendering
    # itself uses textContent everywhere (never innerHTML), so this is
    # belt-and-suspenders against a crawled page's own content (which is
    # adversarial/untrusted by definition) smuggling markup into the report.
    data_json = json.dumps(payload).replace("</", "<\\/")

    html = _PAGE_TEMPLATE.replace("__TITLE__", _escape(meta.get("title", site_name)))
    html = html.replace("__DATA_JSON__", data_json)
    path.write_text(html, encoding="utf-8")
    return path


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --surface-1:      #fcfcfb;
    --page-plane:     #f9f9f7;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --gridline:       #e1e0d9;
    --border:         rgba(11,11,11,0.10);
    --accent:         #2a78d6;
    --status-critical:#d03b3b;
    --status-warning: #a86a00;
    --row-hover:      #f0efec;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface-1:      #1a1a19;
      --page-plane:     #0d0d0d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --gridline:       #2c2c2a;
      --border:         rgba(255,255,255,0.10);
      --accent:         #3987e5;
      --status-critical:#e66767;
      --status-warning: #fab219;
      --row-hover:      #232322;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page-plane);
    color: var(--text-primary);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 24px 20px 60px; }
  header h1 { font-size: 19px; margin: 0 0 4px; }
  header .subtitle { color: var(--text-secondary); font-size: 13px; margin: 0 0 14px; }
  .partial-banner {
    background: var(--status-warning);
    color: #1a1a19;
    font-weight: 600;
    padding: 8px 12px;
    border-radius: 6px;
    margin-bottom: 14px;
    font-size: 13px;
  }
  .summary-lines {
    display: flex; flex-wrap: wrap; gap: 4px 18px;
    color: var(--text-secondary); font-size: 12.5px;
    margin-bottom: 16px;
  }
  .summary-lines b { color: var(--text-primary); font-variant-numeric: tabular-nums; }

  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
  .chip {
    border: 1px solid var(--border);
    background: var(--surface-1);
    color: var(--text-secondary);
    border-radius: 999px;
    padding: 4px 10px;
    font-size: 12px;
    cursor: pointer;
    user-select: none;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .chip .n { font-variant-numeric: tabular-nums; color: var(--text-muted); }
  .chip[data-severity="critical"] { border-color: color-mix(in srgb, var(--status-critical) 45%, var(--border)); }
  .chip[data-severity="critical"] .dot { background: var(--status-critical); }
  .chip[data-severity="warning"] { border-color: color-mix(in srgb, var(--status-warning) 45%, var(--border)); }
  .chip[data-severity="warning"] .dot { background: var(--status-warning); }
  .chip .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); flex: none; }
  .chip.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .chip.active .n, .chip.active .dot { color: #fff; }

  .controls {
    display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
    margin-bottom: 12px;
  }
  .controls input[type="search"] {
    flex: 1 1 260px;
    background: var(--surface-1);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 6px;
    padding: 7px 10px;
    font-size: 13px;
  }
  .controls button {
    background: var(--surface-1);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 6px;
    padding: 7px 12px;
    font-size: 13px;
    cursor: pointer;
  }
  .controls button:hover { background: var(--row-hover); }
  .controls button.copied { color: var(--accent); border-color: var(--accent); }
  .count { color: var(--text-muted); font-size: 12.5px; margin-bottom: 8px; }

  .table-scroll { overflow-x: auto; border-radius: 8px; }
  table { width: 100%; min-width: 640px; table-layout: fixed; border-collapse: collapse; background: var(--surface-1); }
  col.col-category { width: 17%; }
  col.col-detail { width: 14%; }
  col.col-url { width: 38%; }
  col.col-found-on { width: 31%; }
  thead th {
    position: sticky; top: 0;
    background: var(--surface-1);
    text-align: left;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .03em;
    color: var(--text-muted);
    padding: 9px 12px;
    border-bottom: 1px solid var(--gridline);
    cursor: pointer;
    white-space: nowrap;
  }
  thead th:hover { color: var(--text-primary); }
  thead th .arrow { opacity: .5; margin-left: 3px; }
  thead th.sorted { color: var(--text-primary); }
  thead th.sorted .arrow { opacity: 1; }
  tbody td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--gridline);
    vertical-align: top;
    overflow-wrap: anywhere;
  }
  tbody tr:hover td { background: var(--row-hover); }
  tbody tr:last-child td { border-bottom: none; }
  td.detail { font-variant-numeric: tabular-nums; }
  td.url, td.found_on { font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12.5px; }
  td.url a, td.found_on a { color: var(--accent); text-decoration: none; }
  td.url a:hover, td.found_on a:hover { text-decoration: underline; }
  .sev-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 7px; }
  .sev-dot[data-severity="critical"] { background: var(--status-critical); }
  .sev-dot[data-severity="warning"] { background: var(--status-warning); }
  .sev-dot[data-severity="none"] { background: var(--text-muted); opacity: .35; }
  .empty { padding: 30px 12px; text-align: center; color: var(--text-muted); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1 id="report-title"></h1>
    <p class="subtitle" id="report-subtitle"></p>
  </header>
  <div id="partial-banner"></div>
  <div class="summary-lines" id="summary-lines"></div>
  <div class="chips" id="chips"></div>
  <div class="controls">
    <input type="search" id="search" placeholder="Filter (matches category, detail, URL, found on)...">
    <button id="copy-btn" type="button">Copy visible rows</button>
  </div>
  <div class="count" id="count"></div>
  <div class="table-scroll">
    <table>
      <colgroup>
        <col class="col-category"><col class="col-detail"><col class="col-url"><col class="col-found-on">
      </colgroup>
      <thead>
        <tr>
          <th data-key="category">Category<span class="arrow">↕</span></th>
          <th data-key="detail">Detail<span class="arrow">↕</span></th>
          <th data-key="url">URL<span class="arrow">↕</span></th>
          <th data-key="found_on">Found On<span class="arrow">↕</span></th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
  <div class="empty" id="empty-state" style="display:none">No rows match the current filter.</div>
</div>

<script type="application/json" id="report-data">__DATA_JSON__</script>
<script>
(function () {
  "use strict";
  var payload = JSON.parse(document.getElementById("report-data").textContent);
  var meta = payload.meta;
  var rows = payload.rows;

  document.title = meta.title || document.title;
  document.getElementById("report-title").textContent = meta.title || "";
  document.getElementById("report-subtitle").textContent = meta.subtitle || "";

  if (meta.partial) {
    var banner = document.createElement("div");
    banner.className = "partial-banner";
    banner.textContent = "PARTIAL / IN-PROGRESS CHECKPOINT - crawl was still running when this was written";
    document.getElementById("partial-banner").appendChild(banner);
  }

  var summaryEl = document.getElementById("summary-lines");
  (meta.summary_lines || []).forEach(function (pair) {
    var span = document.createElement("span");
    var b = document.createElement("b");
    b.textContent = pair[1];
    span.appendChild(document.createTextNode(pair[0] + ": "));
    span.appendChild(b);
    summaryEl.appendChild(span);
  });

  // ---- category chips (multi-select filter) ----
  var categoryOrder = [];
  var categoryCounts = {};
  var categorySeverity = {};
  rows.forEach(function (r) {
    if (!(r.category in categoryCounts)) {
      categoryOrder.push(r.category);
      categoryCounts[r.category] = 0;
      categorySeverity[r.category] = r.severity;
    }
    categoryCounts[r.category]++;
  });

  var activeCategories = new Set(); // empty set == "all"
  var chipsEl = document.getElementById("chips");

  function renderChips() {
    chipsEl.innerHTML = "";
    categoryOrder.forEach(function (cat) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip" + (activeCategories.has(cat) ? " active" : "");
      chip.dataset.severity = categorySeverity[cat];
      var dot = document.createElement("span");
      dot.className = "dot";
      var label = document.createElement("span");
      label.textContent = cat;
      var n = document.createElement("span");
      n.className = "n";
      n.textContent = categoryCounts[cat];
      chip.appendChild(dot);
      chip.appendChild(label);
      chip.appendChild(n);
      chip.addEventListener("click", function () {
        if (activeCategories.has(cat)) { activeCategories.delete(cat); }
        else { activeCategories.add(cat); }
        renderChips();
        render();
      });
      chipsEl.appendChild(chip);
    });
  }

  // ---- sort state ----
  var sortKey = "category";
  var sortDir = 1;

  function isNumeric(v) {
    return v !== "" && v !== null && v !== undefined && !isNaN(v);
  }

  // Compares two raw cell values. When both look numeric (e.g. two
  // status codes) it compares numerically; otherwise it falls back to a
  // string compare of both sides. Comparing per-pair rather than
  // pre-converting each row in isolation matters because the "Detail"
  // column mixes types across categories (status codes, secret labels,
  // header names, param lists) - converting a number to a JS Number and
  // a string to a JS String independently means < and > silently coerce
  // via NaN and produce a nonsense order once both types are present.
  function compareValues(a, b) {
    if (isNumeric(a) && isNumeric(b)) {
      return parseFloat(a) - parseFloat(b);
    }
    var as = (a || "").toString().toLowerCase();
    var bs = (b || "").toString().toLowerCase();
    if (as < bs) return -1;
    if (as > bs) return 1;
    return 0;
  }

  document.querySelectorAll("thead th").forEach(function (th) {
    th.addEventListener("click", function () {
      var key = th.dataset.key;
      if (sortKey === key) { sortDir *= -1; }
      else { sortKey = key; sortDir = 1; }
      document.querySelectorAll("thead th").forEach(function (h) {
        h.classList.toggle("sorted", h === th);
        h.querySelector(".arrow").textContent = h === th ? (sortDir === 1 ? "↑" : "↓") : "↕";
      });
      render();
    });
  });

  // ---- search + render ----
  var searchEl = document.getElementById("search");
  var tbody = document.getElementById("tbody");
  var countEl = document.getElementById("count");
  var emptyEl = document.getElementById("empty-state");
  var visibleRows = [];

  function matchesSearch(row, needle) {
    if (!needle) return true;
    return (row.category + " " + row.detail + " " + row.url + " " + row.found_on)
      .toLowerCase().indexOf(needle) !== -1;
  }

  function linkCell(td, value) {
    if (/^https?:\\/\\//i.test(value)) {
      var a = document.createElement("a");
      a.href = value;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = value;
      td.appendChild(a);
    } else {
      td.textContent = value;
    }
  }

  function render() {
    var needle = searchEl.value.trim().toLowerCase();
    visibleRows = rows.filter(function (r) {
      if (activeCategories.size && !activeCategories.has(r.category)) return false;
      return matchesSearch(r, needle);
    });
    visibleRows.sort(function (a, b) {
      return compareValues(a[sortKey], b[sortKey]) * sortDir;
    });

    tbody.innerHTML = "";
    visibleRows.forEach(function (r) {
      var tr = document.createElement("tr");

      var tdCat = document.createElement("td");
      var dot = document.createElement("span");
      dot.className = "sev-dot";
      dot.dataset.severity = r.severity;
      tdCat.appendChild(dot);
      tdCat.appendChild(document.createTextNode(r.category));
      tr.appendChild(tdCat);

      var tdDetail = document.createElement("td");
      tdDetail.className = "detail";
      tdDetail.textContent = r.detail;
      tr.appendChild(tdDetail);

      var tdUrl = document.createElement("td");
      tdUrl.className = "url";
      linkCell(tdUrl, r.url);
      tr.appendChild(tdUrl);

      var tdFound = document.createElement("td");
      tdFound.className = "found_on";
      linkCell(tdFound, r.found_on);
      tr.appendChild(tdFound);

      tbody.appendChild(tr);
    });

    countEl.textContent = "Showing " + visibleRows.length + " of " + rows.length + " rows";
    emptyEl.style.display = visibleRows.length ? "none" : "block";
  }

  searchEl.addEventListener("input", render);

  // ---- copy visible rows (TSV - pastes cleanly into email or a sheet) ----
  document.getElementById("copy-btn").addEventListener("click", function () {
    var lines = ["Category\\tDetail\\tURL\\tFound On"];
    visibleRows.forEach(function (r) {
      lines.push([r.category, r.detail, r.url, r.found_on].join("\\t"));
    });
    var text = lines.join("\\n");
    var btn = document.getElementById("copy-btn");
    function flash() {
      var original = "Copy visible rows";
      btn.textContent = "Copied " + visibleRows.length + " rows";
      btn.classList.add("copied");
      setTimeout(function () { btn.textContent = original; btn.classList.remove("copied"); }, 1500);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(flash, function () { fallbackCopy(text, flash); });
    } else {
      fallbackCopy(text, flash);
    }
  });

  function fallbackCopy(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore */ }
    document.body.removeChild(ta);
    done();
  }

  renderChips();
  render();
})();
</script>
</body>
</html>
"""

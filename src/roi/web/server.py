"""A tiny read-only web dashboard.

Goals
-----
- **Zero extra dependencies** (stdlib only).
- **Read-only**: this is intended for *observability* (status + diagnostics),
  not remote control.
- Safe to run alongside the existing Rich TUI and/or headless mode.

This module intentionally avoids touching hardware (no instrument I/O). All
data is obtained from existing in-process state snapshots.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class WebServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    # Optional token. If set, clients must provide it as either:
    #   - Authorization: Bearer <token>
    #   - ?token=<token>
    token: str = ""


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>ROI Dashboard</title>
  <style>
    :root { --fg:#111; --muted:#666; --bg:#fafafa; --card:#fff; --border:#ddd; --ok:#0a7; --warn:#d70; --bad:#c22; }
    body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; background:var(--bg); color:var(--fg); margin:0; }
    header { padding: 14px 18px; border-bottom: 1px solid var(--border); background:#fff; position: sticky; top: 0; z-index: 2; }
    header .title { font-weight: 700; }
    header .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
    main { padding: 16px 18px 40px; max-width: 1200px; margin: 0 auto; }

    .row { display:flex; flex-wrap:wrap; gap: 10px; align-items:center; }
    .row .hint { color: var(--muted); font-size: 12px; }
    .btn { background:#fff; border:1px solid var(--border); border-radius: 8px; padding: 6px 10px; cursor:pointer; font-size: 12px; }
    .btn:hover { background:#f2f2f2; }

    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 12px 10px; box-shadow: 0 1px 0 rgba(0,0,0,.03); }
    .card h2 { font-size: 14px; margin: 0 0 8px; display:flex; align-items:center; gap:8px; }
    .card.pat { grid-column: 1 / -1; }

    .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
    .pill.ok { color: #fff; background: var(--ok); border-color: transparent; }
    .pill.warn { color: #fff; background: var(--warn); border-color: transparent; }
    .pill.bad { color: #fff; background: var(--bad); border-color: transparent; }

    table { width: 100%; border-collapse: collapse; }
    td { padding: 4px 0; vertical-align: top; font-size: 13px; }
    td.k { color: var(--muted); width: 42%; }
    td.v { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }

    .badge { font-weight: 700; }
    .badge.on { color: var(--ok); }
    .badge.off { color: var(--bad); }
    .badge.dim { color: var(--muted); font-weight: 500; }

    .wd.ok { color: var(--ok); font-weight: 700; }
    .wd.warn { color: var(--warn); font-weight: 700; }
    .wd.bad { color: var(--bad); font-weight: 700; }
    .wd.dim { color: var(--muted); font-weight: 500; }

    .patgrid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .patcell { border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px; }
    .pathead { display:flex; justify-content:space-between; align-items:baseline; gap:8px; margin-bottom: 6px; }
    .pathead .j { font-weight: 700; }
    .pathead .age { color: var(--muted); font-size: 11px; }
    .patbody { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; line-height: 1.3; }
    .patpin { display:inline-block; margin-right: 6px; }
    .patpin.v1 { color: var(--ok); }
    .patpin.v2 { color: var(--warn); }
    .patpin.v3 { color: var(--bad); }
    .patdim { color: var(--muted); }

    details.err summary { cursor:pointer; color: var(--bad); }
    details.err pre { background:#0b1020; color:#e7e7e7; padding: 10px; border-radius: 10px; overflow:auto; font-size: 11px; line-height: 1.35; margin-top: 8px; }

    .log { margin-top: 14px; }
    .log pre { background:#0b1020; color:#e7e7e7; padding: 10px; border-radius: 10px; overflow:auto; font-size: 12px; line-height: 1.35; }
    .tiny { font-size: 11px; color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <div class="title">ROI — Web Dashboard</div>
    <div class="meta" id="meta">Loading...</div>
  </header>

  <main>
    <div class="row">
      <button class="btn" id="pauseBtn">Pause</button>
      <button class="btn" id="copyBtn">Copy JSON</button>
      <span class="hint" id="hint"></span>
    </div>
    <div style="height:10px"></div>

    <div class="grid" id="cards"></div>

    <div class="log">
      <h2 style="font-size:14px;margin:14px 0 8px">Recent events</h2>
      <div class="tiny">This is an in-memory ring buffer (restarts clear it). Repeated identical errors are throttled.</div>
      <pre id="events">Loading...</pre>
    </div>

    <div class="log">
      <h2 style="font-size:14px;margin:14px 0 8px">Raw snapshot</h2>
      <pre id="raw">Loading...</pre>
    </div>
  </main>

<script>
(function () {
  var paused = false;
  var inFlight = false;

  var pauseBtn = document.getElementById('pauseBtn');
  var copyBtn = document.getElementById('copyBtn');
  var meta = document.getElementById('meta');
  var hint = document.getElementById('hint');
  var cards = document.getElementById('cards');
  var eventsPre = document.getElementById('events');
  var rawPre = document.getElementById('raw');

  function parseQuery(search) {
    var out = {};
    var s = (search || '').replace(/^\?/, '');
    if (!s) return out;
    var parts = s.split('&');
    for (var i = 0; i < parts.length; i++) {
      var kv = parts[i].split('=');
      if (!kv[0]) continue;
      var k = decodeURIComponent(kv[0]);
      var v = decodeURIComponent(kv.slice(1).join('=') || '');
      out[k] = v;
    }
    return out;
  }

  var q = parseQuery(window.location.search);
  var token = q.token || '';
  var statusUrl = token ? ('/api/status?token=' + encodeURIComponent(token)) : '/api/status';

  pauseBtn.onclick = function () {
    paused = !paused;
    pauseBtn.textContent = paused ? 'Resume' : 'Pause';
  };

  copyBtn.onclick = function () {
    try {
      var txt = rawPre.textContent || '';
      var ta = document.createElement('textarea');
      ta.value = txt;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      var ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (e) {
        ok = false;
      }
      document.body.removeChild(ta);
      hint.textContent = ok ? 'Copied JSON to clipboard.' : 'Copy failed (browser permissions).';
      setTimeout(function () { hint.textContent = ''; }, ok ? 1200 : 1500);
    } catch (e2) {
      hint.textContent = 'Copy failed.';
      setTimeout(function () { hint.textContent = ''; }, 1500);
    }
  };

  function pill(text, cls) {
    var s = document.createElement('span');
    s.className = 'pill ' + cls;
    s.textContent = text;
    return s;
  }

  function badge(on, trueLabel, falseLabel) {
    var s = document.createElement('span');
    if (on === null || on === undefined) {
      s.className = 'badge dim';
      s.textContent = '--';
      return s;
    }
    s.className = 'badge ' + (on ? 'on' : 'off');
    s.textContent = on ? (trueLabel || 'ON') : (falseLabel || 'OFF');
    return s;
  }

  function wdSpan(wd, key) {
    var span = document.createElement('span');
    var ages = (wd && wd.ages) ? wd.ages : {};
    var states = (wd && wd.states) ? wd.states : {};
    var timed = (wd && wd.timed_out) ? wd.timed_out : {};

    var age = ages ? ages[key] : null;
    var st = states ? (states[key] || '') : '';
    st = String(st || '').toLowerCase();
    var to = timed ? (timed[key] === true) : false;

    // If we don't have an age, still surface timeout state when known.
    if (age === null || age === undefined) {
      if (st === 'warn') {
        span.className = 'wd warn';
        span.textContent = 'LAG';
        return span;
      }
      if (st === 'to' || st === 'timeout' || to) {
        span.className = 'wd bad';
        span.textContent = 'TO';
        return span;
      }
      if (st) {
        span.className = 'wd warn';
        span.textContent = st.toUpperCase();
        return span;
      }
      span.className = 'wd dim';
      span.textContent = '--';
      return span;
    }

    var a = 0.0;
    try { a = Number(age) || 0.0; } catch (e) { a = 0.0; }
    if (st === 'warn') {
      span.className = 'wd warn';
      span.textContent = 'LAG ' + a.toFixed(1) + 's';
      return span;
    }
    if (st === 'to' || st === 'timeout' || to) {
      span.className = 'wd bad';
      span.textContent = 'TO ' + a.toFixed(1) + 's';
      return span;
    }
    span.className = 'wd ok';
    span.textContent = a.toFixed(1) + 's';
    return span;
  }

  function addRow(tbl, k, v) {
    var tr = document.createElement('tr');
    var tdK = document.createElement('td');
    tdK.className = 'k';
    tdK.textContent = k;
    var tdV = document.createElement('td');
    tdV.className = 'v';

    if (v === null || v === undefined) {
      tdV.textContent = '--';
    } else if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      tdV.textContent = String(v);
    } else {
      tdV.appendChild(v);
    }

    tr.appendChild(tdK);
    tr.appendChild(tdV);
    tbl.appendChild(tr);
  }

  function fmt4g(x) {
    var n = Number(x);
    if (!isFinite(n)) return '';
    // Similar to Python's format ".4g"
    var abs = Math.abs(n);
    if (abs === 0) return '0';
    if (abs >= 1000 || abs < 0.01) return n.toExponential(3).replace('e', 'E');
    // Remove trailing zeros
    var s = n.toPrecision(4);
    // toPrecision can yield scientific for some values; keep if so.
    if (s.indexOf('e') !== -1 || s.indexOf('E') !== -1) return s.replace('e', 'E');
    // Trim trailing zeros and dot
    s = s.replace(/0+$/, '').replace(/\.$/, '');
    return s;
  }

  function deviceHealthPill(present, health) {
    if (!present) return pill('NOT DETECTED', 'bad');
    if (health && health.last_error_age_s !== undefined && health.last_error_age_s !== null) {
      return pill('ERROR', 'bad');
    }
    if (health && health.last_ok_age_s !== undefined && health.last_ok_age_s !== null) {
      var age = Number(health.last_ok_age_s) || 0;
      if (age < 2.5) return pill('OK', 'ok');
      if (age < 8.0) return pill('STALE', 'warn');
      return pill('STUCK', 'bad');
    }
    return pill('OK', 'ok');
  }

  function errorDetails(h) {
    if (!h || !h.last_error) return null;
    var det = document.createElement('details');
    det.className = 'err';
    var sum = document.createElement('summary');
    var n = (h.error_count !== undefined && h.error_count !== null) ? String(h.error_count) : '';
    sum.textContent = (n ? ('x' + n + ' ') : '') + String(h.last_error);
    det.appendChild(sum);
    if (h.last_error_trace) {
      var pre = document.createElement('pre');
      pre.textContent = String(h.last_error_trace);
      det.appendChild(pre);
    }
    return det;
  }

  function makeCard(title, key, cls, buildFn, data) {
    var devices = data.devices || {};
    var healthAll = (data.diagnostics && data.diagnostics.health) ? data.diagnostics.health : {};
    var pres = !!(devices[key] && devices[key].present);

    var card = document.createElement('div');
    card.className = 'card' + (cls ? (' ' + cls) : '');
    var h2 = document.createElement('h2');
    h2.textContent = title;
    h2.appendChild(deviceHealthPill(pres, healthAll ? healthAll[key] : null));
    card.appendChild(h2);

    buildFn(card, devices, healthAll, data);
    cards.appendChild(card);
  }

  function render(data) {
    // Header meta
    var build = data.build_tag || 'unknown';
    var host = data.host || '--';
    var up = Number(data.uptime_s) || 0;
    meta.textContent = 'host=' + host + ' | build=' + build + ' | uptime=' + up.toFixed(1) + 's | updated=' + (new Date()).toLocaleTimeString();

    var devices = data.devices || {};
    var telem = data.telemetry || {};
    var wd = data.watchdog || {};
    var cfg = data.config || {};

    // Cards (ordered to match the Rich TUI)
    cards.innerHTML = '';

    makeCard('E-Load', 'eload', '', function (card, devices, healthAll, data) {
      var telem = data.telemetry || {};
      var wd = data.watchdog || {};
      var h = healthAll ? healthAll.eload : null;
      var tbl = document.createElement('table');

      addRow(tbl, 'WD', wdSpan(wd, 'eload'));
      // Rich dash shows VISA resource_name as ID
      addRow(tbl, 'ID', (devices.eload && devices.eload.resource) ? devices.eload.resource : '--');

      var imp = String(telem.load_stat_imp || '').trim();
      var en;
      if (imp !== '') {
        var u = imp.toUpperCase();
        en = (u === 'ON' || imp === '1');
      } else {
        en = !!(devices.eload && devices.eload.cmd_enabled);
      }
      addRow(tbl, 'Enable', badge(en, 'ON', 'OFF'));

      var modePolled = String(telem.load_stat_func || '').trim();
      var modeStr = modePolled;
      if (!modeStr) {
        modeStr = (devices.eload && devices.eload.cmd_mode) ? 'RES' : 'CURR';
      }
      addRow(tbl, 'Mode', modeStr || '--');

      var modeU = String(modeStr || '').toUpperCase();
      var setLabel = 'Set';
      var setVal = '';
      if (modeU.indexOf('CURR') === 0) {
        setLabel = 'Set (I)';
        setVal = String(telem.load_stat_curr || '').trim();
        if (!setVal) {
          var mA = devices.eload ? devices.eload.cmd_csetting_mA : null;
          if (mA !== null && mA !== undefined) setVal = fmt4g(Number(mA) / 1000.0);
        }
      } else if (modeU.indexOf('RES') === 0) {
        setLabel = 'Set (R)';
        setVal = String(telem.load_stat_res || '').trim();
        if (!setVal) {
          var mOhm = devices.eload ? devices.eload.cmd_rsetting_mOhm : null;
          if (mOhm !== null && mOhm !== undefined) setVal = fmt4g(Number(mOhm) / 1000.0);
        }
      } else {
        setVal = String(telem.load_stat_curr || '').trim() || String(telem.load_stat_res || '').trim();
      }
      addRow(tbl, setLabel, setVal || '--');

      var v = (telem.load_volts_mV !== null && telem.load_volts_mV !== undefined) ? (Number(telem.load_volts_mV) / 1000.0) : null;
      var i = (telem.load_current_mA !== null && telem.load_current_mA !== undefined) ? (Number(telem.load_current_mA) / 1000.0) : null;
      addRow(tbl, 'Meas V', (v !== null && isFinite(v)) ? (v.toFixed(3) + ' V') : '--');
      addRow(tbl, 'Meas I', (i !== null && isFinite(i)) ? (i.toFixed(3) + ' A') : '--');

      var shortS = String(telem.load_stat_short || '').trim();
      if (shortS !== '') {
        var su = shortS.toUpperCase();
        addRow(tbl, 'Short', badge((su === 'ON' || shortS === '1'), 'ON', 'OFF'));
      } else {
        addRow(tbl, 'Short', badge(!!(devices.eload && devices.eload.cmd_short), 'ON', 'OFF'));
      }

      var err = errorDetails(h);
      if (err) addRow(tbl, 'Error', err);

      card.appendChild(tbl);
    }, data);

    makeCard('AFG-2125', 'afg', '', function (card, devices, healthAll, data) {
      var telem = data.telemetry || {};
      var wd = data.watchdog || {};
      var h = healthAll ? healthAll.afg : null;
      var tbl = document.createElement('table');

      addRow(tbl, 'WD', wdSpan(wd, 'afg'));
      addRow(tbl, 'ID', (devices.afg && devices.afg.id) ? devices.afg.id : '--');

      var outPolled = String(telem.afg_out_str || '').trim();
      var outOn;
      if (outPolled) {
        outOn = (outPolled.toUpperCase() === 'ON' || outPolled === '1');
      } else {
        outOn = !!(devices.afg && devices.afg.cmd_output);
      }
      addRow(tbl, 'Output', badge(outOn, 'ON', 'OFF'));

      var freq = String(telem.afg_freq_str || '').trim();
      if (!freq && devices.afg && devices.afg.cmd_freq_hz !== undefined) freq = String(devices.afg.cmd_freq_hz);
      addRow(tbl, 'Freq', (freq ? (freq + ' Hz') : '--'));

      var ampl = String(telem.afg_ampl_str || '').trim();
      if (!ampl && devices.afg && devices.afg.cmd_ampl_mVpp !== undefined) ampl = fmt4g(Number(devices.afg.cmd_ampl_mVpp) / 1000.0);
      addRow(tbl, 'Ampl', (ampl ? (ampl + ' Vpp') : '--'));

      var offs = String(telem.afg_offset_str || '').trim();
      if (!offs && devices.afg && devices.afg.cmd_offset_mV !== undefined) offs = fmt4g(Number(devices.afg.cmd_offset_mV) / 1000.0);
      addRow(tbl, 'Offset', (offs ? (offs + ' V') : '--'));

      var duty = String(telem.afg_duty_str || '').trim();
      if (!duty && devices.afg && devices.afg.cmd_duty !== undefined) duty = String(devices.afg.cmd_duty);
      addRow(tbl, 'Duty', (duty ? (duty + ' %') : '--'));

      var shape = String(telem.afg_shape_str || '').trim();
      if (!shape && devices.afg && devices.afg.cmd_shape !== undefined) {
        var si = Number(devices.afg.cmd_shape) || 0;
        shape = (si === 1) ? 'SQU' : ((si === 2) ? 'RAMP' : 'SIN');
      }
      addRow(tbl, 'Shape', shape || '--');

      var err = errorDetails(h);
      if (err) addRow(tbl, 'Error', err);

      card.appendChild(tbl);
    }, data);

    makeCard('Multimeter', 'mmeter', '', function (card, devices, healthAll, data) {
      var telem = data.telemetry || {};
      var wd = data.watchdog || {};
      var h = healthAll ? healthAll.mmeter : null;
      var mm = devices.mmeter || {};

      var tbl = document.createElement('table');
      addRow(tbl, 'WD', wdSpan(wd, 'mmeter'));
      addRow(tbl, 'ID', mm.id || '--');

      addRow(tbl, 'Func', mm.func || '--');
      var range = '--';
      if (mm.autorange) {
        range = 'AUTO';
      } else if (mm.range_value !== undefined && mm.range_value !== null && Number(mm.range_value) !== 0) {
        range = String(mm.range_value);
      }
      addRow(tbl, 'Range', range);
      addRow(tbl, 'NPLC', (mm.nplc !== undefined && mm.nplc !== null) ? String(mm.nplc) : '--');
      addRow(tbl, 'Rel', badge(!!mm.rel, 'ON', 'OFF'));
      var trig = mm.trig;
      var trigName = '--';
      if (trig !== undefined && trig !== null) {
        trigName = (trig === 0 || trig === '0') ? 'IMM' : ((trig === 1 || trig === '1') ? 'BUS' : ((trig === 2 || trig === '2') ? 'MAN' : String(trig)));
      }
      addRow(tbl, 'Trig', trigName);
      addRow(tbl, 'Func2', (mm.func2_enabled ? (mm.func2 || '--') : 'OFF'));

      var primary = String(telem.mmeter_primary_str || '').trim();
      var secondary = String(telem.mmeter_secondary_str || '').trim();
      var measFunc = String(telem.mmeter_func_str || '').trim();

      if (primary) {
        if (measFunc) addRow(tbl, 'Meas', measFunc);
        addRow(tbl, 'Val', primary);
        if (secondary) addRow(tbl, 'Val2', secondary);
      } else {
        var cur = (telem.meter_current_mA !== null && telem.meter_current_mA !== undefined) ? (Number(telem.meter_current_mA) / 1000.0) : null;
        addRow(tbl, 'Val', (cur !== null && isFinite(cur)) ? (cur.toFixed(3) + ' A') : '--');
      }

      var err = errorDetails(h);
      if (err) addRow(tbl, 'Error', err);

      card.appendChild(tbl);
    }, data);

    makeCard('MrSignal', 'mrsignal', '', function (card, devices, healthAll, data) {
      var telem = data.telemetry || {};
      var wd = data.watchdog || {};
      var h = healthAll ? healthAll.mrsignal : null;
      var mrs = devices.mrsignal || {};

      var tbl = document.createElement('table');
      addRow(tbl, 'WD', wdSpan(wd, 'mrsignal'));

      var idPolled = String(telem.mrs_id_str || '').trim();
      addRow(tbl, 'ID', idPolled || (mrs.id !== undefined ? String(mrs.id) : '--'));

      var outPolled = String(telem.mrs_out_str || '').trim();
      var outOn;
      if (outPolled) {
        var ou = outPolled.toUpperCase();
        outOn = (ou === 'ON' || ou === '1' || ou === 'TRUE');
      } else {
        outOn = !!mrs.cmd_output_on;
      }
      addRow(tbl, 'Output', badge(outOn, 'ON', 'OFF'));

      var mode = String(telem.mrs_mode_str || '').trim();
      if (!mode) {
        var sel = Number(mrs.cmd_output_select) || 0;
        mode = (sel === 0) ? 'mA' : ((sel === 1) ? 'V' : ((sel === 2) ? 'XMT' : ((sel === 3) ? 'PULSE' : ((sel === 4) ? 'mV' : ((sel === 5) ? 'R' : ((sel === 6) ? '24V' : '—'))))));
      }
      addRow(tbl, 'Mode', mode || '—');

      var setS = String(telem.mrs_set_str || '').trim();
      if (!setS) {
        var sel2 = Number(mrs.cmd_output_select) || 0;
        var v2 = Number(mrs.cmd_output_value);
        if (!isFinite(v2)) v2 = 0.0;
        if (sel2 === 0) setS = fmt4g(v2) + ' mA';
        else if (sel2 === 4) setS = fmt4g(v2) + ' mV';
        else setS = fmt4g(v2) + ' V';
      }
      addRow(tbl, 'Set', setS || '—');

      var inS = String(telem.mrs_in_str || '').trim();
      if (!inS) {
        var sel3 = Number(mrs.cmd_output_select) || 0;
        var v3 = Number(mrs.cmd_input_value);
        if (!isFinite(v3)) v3 = 0.0;
        if (sel3 === 0) inS = fmt4g(v3) + ' mA';
        else if (sel3 === 4) inS = fmt4g(v3) + ' mV';
        else inS = fmt4g(v3) + ' V';
      }
      addRow(tbl, 'Input', inS || '—');

      var bo = String(telem.mrs_bo_str || '').trim() || String(mrs.cmd_float_byteorder || '').trim();
      if (bo) addRow(tbl, 'Float', bo);

      var err = errorDetails(h);
      if (err) addRow(tbl, 'Error', err);

      card.appendChild(tbl);
    }, data);

    makeCard('K Relays', 'k1', '', function (card, devices, healthAll, data) {
      var wd = data.watchdog || {};
      var k1 = devices.k1 || {};
      var kr = devices.k_relays || {};
      var chans = Array.isArray(kr.channels) ? kr.channels : (Array.isArray(k1.channels) ? k1.channels : []);
      var tbl = document.createElement('table');
      addRow(tbl, 'WD', wdSpan(wd, 'k1'));
      addRow(tbl, 'Backend', kr.backend || k1.backend || '--');
      addRow(tbl, 'Channels', Number(kr.channel_count || k1.channel_count || (chans.length || 1)));

      if (chans.length > 0) {
        for (var i = 0; i < chans.length; i++) {
          var ch = chans[i] || {};
          var idx = Number(ch.index);
          if (!isFinite(idx) || idx <= 0) idx = (i + 1);
          var name = String(ch.name || ('K' + String(idx)));

          var wrap = document.createElement('span');
          wrap.appendChild(badge(!!ch.drive, 'ON', 'OFF'));
          wrap.appendChild(document.createTextNode('  '));
          if (ch.pin_level === null || ch.pin_level === undefined) {
            wrap.appendChild(document.createTextNode('--'));
          } else {
            wrap.appendChild(badge(!!ch.pin_level, 'HIGH', 'LOW'));
          }
          addRow(tbl, name, wrap);
        }
      } else {
        addRow(tbl, 'K1', badge(!!k1.drive, 'ON', 'OFF'));
      }
      card.appendChild(tbl);
    }, data);

    makeCard('CAN', 'can', '', function (card, devices, healthAll, data) {
      var wd = data.watchdog || {};
      var can = devices.can || {};
      var cfg = data.config || {};
      var tbl = document.createElement('table');

      addRow(tbl, 'WD', wdSpan(wd, 'can'));
      addRow(tbl, 'IF', can.interface || cfg.can_interface || '--');
      addRow(tbl, 'Chan', can.channel_short || can.channel || cfg.can_channel || '--');

      var br = can.bitrate || cfg.can_bitrate;
      var brStr = '--';
      if (br !== null && br !== undefined) {
        var kb = Math.round(Number(br) / 1000);
        if (isFinite(kb) && kb) brStr = String(kb) + ' kbps';
      }
      addRow(tbl, 'Bitrate', brStr);

      var load = can.bus_load_pct;
      addRow(tbl, 'Load', (load !== null && load !== undefined) ? (Number(load).toFixed(1) + ' %') : '--');
      addRow(tbl, 'Poll', (cfg.status_poll_period_s !== undefined && cfg.status_poll_period_s !== null) ? (Number(cfg.status_poll_period_s).toFixed(2) + ' s') : '--');
      addRow(tbl, 'RX', (can.rx_fps !== null && can.rx_fps !== undefined) ? (Number(can.rx_fps).toFixed(0) + ' fps') : '--');
      addRow(tbl, 'TX', (can.tx_fps !== null && can.tx_fps !== undefined) ? (Number(can.tx_fps).toFixed(0) + ' fps') : '--');

      card.appendChild(tbl);
    }, data);

    // PAT switching matrix footer (spans width)
    makeCard('PAT switching matrix', 'pat', 'pat', function (card, devices, healthAll, data) {
      var pm = data.pat_matrix || {};
      var meta = data.pat_meta || {};
      var timeoutS = (meta.timeout_s !== undefined && meta.timeout_s !== null) ? Number(meta.timeout_s) : null;
      var j0Names = meta.j0_pin_names || {};

      function cell(j) {
        var k = 'J' + String(j);
        var entry = pm[k] || {};
        var age = entry.age;
        var vals = entry.vals;

        var box = document.createElement('div');
        box.className = 'patcell';

        var head = document.createElement('div');
        head.className = 'pathead';
        var jEl = document.createElement('div');
        jEl.className = 'j';
        jEl.textContent = k;
        var ageEl = document.createElement('div');
        ageEl.className = 'age';

        if (age === null || age === undefined) {
          ageEl.textContent = 'n/a';
        } else {
          var a = Number(age) || 0.0;
          if (timeoutS !== null && isFinite(timeoutS) && timeoutS > 0 && a > timeoutS) {
            ageEl.textContent = 'TO ' + a.toFixed(1) + 's';
          } else {
            ageEl.textContent = a.toFixed(1) + 's';
          }
        }
        head.appendChild(jEl);
        head.appendChild(ageEl);
        box.appendChild(head);

        var body = document.createElement('div');
        body.className = 'patbody';

        // If stale, don't show last captured pins.
        var showPins = true;
        if (age !== null && age !== undefined && timeoutS !== null && isFinite(timeoutS) && timeoutS > 0) {
          var a2 = Number(age) || 0.0;
          if (a2 > timeoutS) showPins = false;
        }

        if (!showPins) {
          var dim = document.createElement('span');
          dim.className = 'patdim';
          dim.textContent = '--';
          body.appendChild(dim);
          box.appendChild(body);
          return box;
        }

        if (!vals || !vals.length) {
          var dim2 = document.createElement('span');
          dim2.className = 'patdim';
          dim2.textContent = 'n/a';
          body.appendChild(dim2);
          box.appendChild(body);
          return box;
        }

        var any = false;
        for (var i = 0; i < 12 && i < vals.length; i++) {
          var vv = (Number(vals[i]) || 0) & 3;
          if (!vv) continue;
          any = true;
          var pin = i + 1;
          var name = '';
          if (j === 0) {
            name = j0Names[String(pin)] || '';
          }
          var label = String(pin) + (name ? (':' + name) : '');
          var sp = document.createElement('span');
          sp.className = 'patpin v' + String(vv);
          sp.textContent = label;
          body.appendChild(sp);
        }
        if (!any) {
          var dim3 = document.createElement('span');
          dim3.className = 'patdim';
          dim3.textContent = '--';
          body.appendChild(dim3);
        }
        box.appendChild(body);
        return box;
      }

      var grid = document.createElement('div');
      grid.className = 'patgrid';
      grid.appendChild(cell(0));
      grid.appendChild(cell(1));
      grid.appendChild(cell(2));
      grid.appendChild(cell(3));
      grid.appendChild(cell(4));
      grid.appendChild(cell(5));
      card.appendChild(grid);
    }, data);

    // Events
    var ev = (data.diagnostics && data.diagnostics.events) ? data.diagnostics.events : [];
    var lines = [];
    var start = Math.max(0, ev.length - 80);
    for (var i = start; i < ev.length; i++) {
      var e = ev[i] || {};
      var ts = Number(e.ts_unix) || 0;
      var t = new Date(ts * 1000).toLocaleTimeString();
      var lvl = String(e.level || 'info').toUpperCase();
      var src = String(e.source || '');
      var msg = String(e.message || '');
      lines.push('[' + t + '] ' + lvl + ' ' + src + ' ' + msg);
    }
    eventsPre.textContent = lines.join('\n') || '(no events yet)';

    rawPre.textContent = JSON.stringify(data, null, 2);
  }

  function showError(err) {
    var msg = String(err);
    meta.textContent = 'Disconnected (' + msg + ')';
    eventsPre.textContent = 'Disconnected: ' + msg;
    if (!rawPre.textContent || rawPre.textContent === 'Loading...' || rawPre.textContent === 'Loading…') {
      rawPre.textContent = 'Disconnected: ' + msg;
    }
  }

  function httpGetJson(url, timeoutMs, onOk, onErr) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url, true);
    xhr.timeout = timeoutMs;
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          var data = JSON.parse(xhr.responseText || '{}');
          onOk(data);
        } catch (e) {
          onErr(e);
        }
      } else {
        var body = '';
        try { body = xhr.responseText || ''; } catch (e2) { body = ''; }
        body = body ? (' ' + body.slice(0, 200)) : '';
        onErr(new Error('HTTP ' + xhr.status + body));
      }
    };
    xhr.ontimeout = function () { onErr(new Error('timeout')); };
    xhr.onerror = function () { onErr(new Error('network error')); };
    try {
      xhr.send(null);
    } catch (e3) {
      onErr(e3);
    }
  }

  function pollOnce() {
    if (paused || inFlight) return;
    inFlight = true;
    httpGetJson(statusUrl, 1500, function (data) {
      try { render(data); } catch (e) { showError(e); }
      inFlight = false;
    }, function (err) {
      showError(err);
      inFlight = false;
    });
  }

  pollOnce();
  setInterval(pollOnce, 1000);
})();
</script>
</body>
</html>
"""


class _ServerWithContext(ThreadingHTTPServer):
    """ThreadingHTTPServer with an attached context."""

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, *, context: "_Context"):
        super().__init__(server_address, RequestHandlerClass)
        self.context = context


class _Context:
    def __init__(
        self,
        *,
        cfg: WebServerConfig,
        get_snapshot: Callable[[], Dict[str, Any]],
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.get_snapshot = get_snapshot
        self.log = log_fn or (lambda _m: None)


class _Handler(BaseHTTPRequestHandler):
    server: _ServerWithContext  # type: ignore[assignment]

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Silence the default per-request logging. ROI already has its own logs.
        return

    def _send(self, status: int, *, content_type: str, body: bytes) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        body = b"Unauthorized"
        self.send_response(int(HTTPStatus.UNAUTHORIZED))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("WWW-Authenticate", 'Bearer realm="ROI"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_authorized(self) -> bool:
        token = str(getattr(self.server.context.cfg, "token", "") or "")
        if not token:
            return True

        # 1) Authorization header
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth.split(" ", 1)[1].strip() == token:
            return True

        # 2) Query parameter
        try:
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            if params.get("token", [""])[0] == token:
                return True
        except Exception:
            pass
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._is_authorized():
            self._unauthorized()
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"

        if path in ("/", "/index.html"):
            self._send(
                int(HTTPStatus.OK),
                content_type="text/html; charset=utf-8",
                body=_INDEX_HTML.encode("utf-8"),
            )
            return

        if path == "/api/status":
            try:
                snap = self.server.context.get_snapshot()
                body = json.dumps(snap, sort_keys=False).encode("utf-8")
                self._send(int(HTTPStatus.OK), content_type="application/json", body=body)
            except Exception as e:
                self._send(
                    int(HTTPStatus.INTERNAL_SERVER_ERROR),
                    content_type="application/json",
                    body=json.dumps({"error": str(e)}).encode("utf-8"),
                )
            return

        if path == "/api/ping":
            self._send(int(HTTPStatus.OK), content_type="text/plain; charset=utf-8", body=b"pong")
            return

        self._send(
            int(HTTPStatus.NOT_FOUND),
            content_type="text/plain; charset=utf-8",
            body=b"Not found",
        )


class WebDashboardServer:
    """Background thread that serves a read-only HTML dashboard + JSON API."""

    def __init__(
        self,
        *,
        cfg: WebServerConfig,
        get_snapshot: Callable[[], Dict[str, Any]],
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._context = _Context(cfg=cfg, get_snapshot=get_snapshot, log_fn=log_fn)
        self._server: Optional[_ServerWithContext] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.is_running:
            return

        # Bind early so we fail fast with a useful error message.
        addr = (str(self.cfg.host), int(self.cfg.port))
        self._server = _ServerWithContext(addr, _Handler, context=self._context)

        def _run() -> None:
            assert self._server is not None
            host, port = self._server.server_address[:2]
            self._context.log(f"[web] dashboard: http://{host}:{port}")
            try:
                self._server.serve_forever(poll_interval=0.5)
            finally:
                try:
                    self._server.server_close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, name="roi-web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        srv = self._server
        if srv is None:
            return
        try:
            srv.shutdown()
        except Exception:
            pass

    @staticmethod
    def default_host() -> str:
        # Prefer a stable hostname if possible, but fall back to 0.0.0.0.
        try:
            _ = socket.gethostname()
        except Exception:
            return "0.0.0.0"
        return "0.0.0.0"


import os
import csv
import time
import platform
import subprocess
import threading
import socket
from datetime import datetime
from collections import deque

from flask import Flask, render_template_string, jsonify

# ---------------------- CONFIG ----------------------
HOSTS = [
    {"name": "Router", "host": "192.168.1.1"},
    {"name": "Google DNS", "host": "8.8.8.8"},
    {"name": "Cloudflare DNS", "host": "1.1.1.1"},
    {"name": "Cloudflare DNS", "host": "88.99.1.5"},
]

PING_INTERVAL_SEC = 5
PING_COUNT = 1
TIMEOUT_SEC = 2
HISTORY_LEN = 120

# TCP fallback ports (if ICMP blocked)
TCP_FALLBACK_PORTS = [53, 80, 443]  # DNS/HTTP/HTTPS

# Log path: put it next to this script (avoids CWD weirdness)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "ping_log.csv")

# ---------------------- STATE ----------------------
state_lock = threading.Lock()
state = {
    h["host"]: {
        "name": h["name"],
        "host": h["host"],
        "up": None,             # None = unknown, True = up, False = down
        "latency_ms": None,
        "last_seen": None,
        "loss_pct": 0.0,
        "history": deque(maxlen=HISTORY_LEN),
        "sent": 0,
        "received": 0,
        "method": "ICMP",
    }
    for h in HOSTS
}

# ---------------------- PROBES ----------------------
def tcp_probe(host, ports, timeout=2):
    """Try TCP connect on common ports. True if any port connects."""
    for p in ports:
        try:
            with socket.create_connection((host, p), timeout=timeout):
                return True
        except Exception:
            continue
    return False


def ping_host(host: str, count=1, timeout=2):
    """
    Returns (up: bool, latency_ms: float|None, method: "ICMP"|"TCP")
    - Forces IPv4
    - Windows-safe ICMP detection (TTL/Reply tokens)
    - Falls back to TCP if ICMP is blocked
    """
    system = platform.system().lower()

    if system == "windows":
        cmd = ["ping", "-4", "-n", str(count), "-w", str(timeout * 1000), host]
    else:
        cmd = ["ping", "-4", "-c", str(count), "-W", str(timeout), host]

    try:
        start = time.time()
        proc = subprocess.run(cmd, capture_output=True)
        duration = (time.time() - start) * 1000.0

        stdout = proc.stdout.decode(errors="ignore")
        stderr = proc.stderr.decode(errors="ignore")
        out = (stdout + "\n" + stderr).strip()
        low = out.lower()

        icmp_tokens = [
            "ttl=", "reply from", "bytes=",     # EN
            "απάντηση από", "ttl=", "byte="     # GR
        ]
        icmp_up = any(tok in low for tok in icmp_tokens)

        latency_ms = None
        if icmp_up:
            import re
            m = re.search(r"time[=<]\s*([\d\.]+)\s*ms", out, re.IGNORECASE)
            if m:
                latency_ms = float(m.group(1))
            else:
                latency_ms = duration
            return True, latency_ms, "ICMP"

        tcp_up = tcp_probe(host, TCP_FALLBACK_PORTS, timeout=timeout)
        if tcp_up:
            return True, None, "TCP"

        return False, None, "ICMP"

    except Exception:
        return False, None, "ICMP"


# ---------------------- LOGGING ----------------------
_log_path_ready = False
_log_lock = threading.Lock()

def _ensure_log_path():
    """
    Make sure we can write to the log file.
    If ping_log.csv is locked (e.g., open in Excel), fall back to a new file.
    """
    global LOG_FILE, _log_path_ready
    if _log_path_ready:
        return

    with _log_lock:
        if _log_path_ready:
            return

        # If file doesn't exist, try create header
        if not os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["timestamp", "name", "host", "up", "latency_ms", "method"])
                _log_path_ready = True
                return
            except PermissionError:
                pass

        # If exists (or create failed), test append
        try:
            with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
                pass
            _log_path_ready = True
            return
        except PermissionError:
            # Fallback to timestamped log in same dir
            ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            LOG_FILE = os.path.join(SCRIPT_DIR, f"ping_log_{ts}.csv")
            with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "name", "host", "up", "latency_ms", "method"])
            _log_path_ready = True


def log_result(ts_iso, host_state):
    _ensure_log_path()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            ts_iso,
            host_state["name"],
            host_state["host"],
            int(host_state["up"]) if host_state["up"] is not None else "",
            host_state["latency_ms"] if host_state["latency_ms"] is not None else "",
            host_state.get("method", "ICMP")
        ])


# ---------------------- MONITOR LOOP ----------------------
def monitor_loop():
    while True:
        ts_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        for h in HOSTS:
            host = h["host"]
            up, latency_ms, method = ping_host(
                host, count=PING_COUNT, timeout=TIMEOUT_SEC
            )

            with state_lock:
                s = state[host]
                s["sent"] += 1
                if up:
                    s["received"] += 1
                s["up"] = up
                s["latency_ms"] = latency_ms
                s["last_seen"] = ts_iso
                s["method"] = method
                s["loss_pct"] = round(100.0 * (1 - s["received"] / s["sent"]), 1)
                s["history"].append((ts_iso, latency_ms if up else None))

            # log outside lock
            try:
                log_result(ts_iso, s)
            except PermissionError:
                # If log suddenly locked mid-run, just skip that tick
                pass

        time.sleep(PING_INTERVAL_SEC)


def start_monitor_thread_once():
    """
    Start monitor thread exactly once (handles flask reloader spawning).
    """
    if getattr(start_monitor_thread_once, "_started", False):
        return
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    start_monitor_thread_once._started = True


# ---------------------- FLASK APP ----------------------
app = Flask(__name__)
start_monitor_thread_once()

HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Network Monitor</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root{
      --bg:#0a0b0d;
      --panel:#121417;
      --panel-2:#171a1f;
      --line:#2a2f36;
      --text:#e6e8eb;
      --muted:#9aa3ad;
      --good:#2ecc71;
      --bad:#e74c3c;
      --warn:#f1c40f;
      --accent:#f1c40f;
      --radius:6px;
      --shadow: 0 4px 12px rgba(0,0,0,.45);
    }
    *{box-sizing:border-box}
    body{
      margin:0; padding:14px;
      background:var(--bg); color:var(--text);
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial;
      letter-spacing:.2px;
    }

    header{
      background:linear-gradient(180deg, #0f1114, #0c0d10);
      border:1px solid var(--line);
      border-left:6px solid var(--accent);
      padding:12px 12px;
      display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:space-between;
      border-radius:var(--radius);
      box-shadow:var(--shadow);
    }
    .title h1{
      margin:0; font-size:18px; font-weight:800; text-transform:uppercase; letter-spacing:1px;
    }
    .title .sub{ color:var(--muted); font-size:12px; }

    .controls{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
    input[type="search"], select{
      background:var(--panel); border:1px solid var(--line); color:var(--text);
      padding:7px 9px; border-radius:4px; outline:none; font-size:13px;
      min-height:34px;
    }
    input[type="search"]{min-width:210px;}
    button{
      background:var(--panel-2); border:1px solid var(--line); color:var(--text);
      padding:7px 10px; border-radius:4px; cursor:pointer; font-weight:700; font-size:13px;
      min-height:34px; transition:.12s ease;
    }
    button:hover{ border-color:#3a4049; }
    button.active{
      border-color:var(--accent);
      box-shadow: inset 0 0 0 1px var(--accent);
    }

    .kpis{
      margin-top:10px;
      display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:8px;
    }
    .kpi{
      background:var(--panel);
      border:1px solid var(--line);
      padding:10px 12px;
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      display:flex; justify-content:space-between; align-items:center;
    }
    .kpi .label{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.6px;}
    .kpi .value{ font-size:17px; font-weight:900; font-variant-numeric: tabular-nums; }
    .tag{font-size:11px; color:var(--muted);}

    /* Alarm banner */
    #alarm{
      display:none; margin-top:10px;
      border:1px solid var(--line);
      border-left:6px solid var(--bad);
      background:#120b0c;
      padding:10px 12px;
      border-radius:var(--radius);
      box-shadow:var(--shadow);
    }

    .table-wrap{
      margin-top:10px;
      background:var(--panel);
      border:1px solid var(--line);
      border-radius:var(--radius);
      overflow:hidden;
      box-shadow:var(--shadow);
    }
    table{
      width:100%; border-collapse:collapse; font-size:13px;
      font-variant-numeric: tabular-nums;
    }
    thead{ background:#0e1013; }
    th, td{
      padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle;
    }
    th{
      color:#c9ced6; font-weight:800; font-size:11px;
      text-transform:uppercase; letter-spacing:.7px; cursor:pointer; user-select:none;
    }
    tbody tr:hover{ background:#0f1216; }

    .pill{
      display:inline-flex; align-items:center; gap:6px;
      font-weight:900; font-size:11px; padding:3px 7px;
      border-radius:3px; border:1px solid transparent; letter-spacing:.5px;
    }
    .pill.up{
      color:#bff5d4; background:rgba(46,204,113,.12); border-color:rgba(46,204,113,.35);
    }
    .pill.down{
      color:#ffd2cf; background:rgba(231,76,60,.12); border-color:rgba(231,76,60,.35);
    }
    .dot{ width:7px; height:7px; border-radius:1px; display:inline-block; }
    .dot.good{ background:var(--good); }
    .dot.bad{ background:var(--bad); }

    .muted{ color:var(--muted); font-size:12px; }
    .loss.bad{ color:var(--bad); font-weight:800;}
    .loss.warn{ color:var(--warn); font-weight:800;}
    .loss.good{ color:var(--good); font-weight:800;}

    canvas{
      width:150px; height:32px; display:block;
      background:#0b0d11; border-radius:3px; border:1px solid #1e232a;
    }

    footer{ margin-top:8px; color:var(--muted); font-size:11px; letter-spacing:.4px; }

    @media (max-width:680px){
      canvas{width:105px;}
      input[type="search"]{min-width:150px;}
      td:nth-child(3), th:nth-child(3){display:none;}
    }
  </style>
</head>
<body>

<header>
  <div class="title">
    <h1>Network Monitor</h1>
    <div class="sub">Interval {{interval}}s • Log file: <b>{{logfile}}</b></div>
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder="Search host / IP" />
    <select id="sort">
      <option value="name">Sort: Name</option>
      <option value="status">Sort: Status</option>
      <option value="method">Sort: Method</option>
      <option value="latency">Sort: Latency</option>
      <option value="loss">Sort: Loss</option>
    </select>
    <button id="autoBtn" class="active">AUTO REFRESH: ON</button>
    <button id="refreshBtn">REFRESH NOW</button>
  </div>
</header>

<!-- ALARM BANNER -->
<div id="alarm">
  <div style="display:flex; gap:10px; align-items:center; justify-content:space-between; flex-wrap:wrap;">
    <div>
      <div style="font-weight:900; letter-spacing:.6px; text-transform:uppercase; font-size:12px; color:#ffd2cf;">
        Critical Alert
      </div>
      <div id="alarmText" style="font-size:14px; font-weight:800; margin-top:2px;"></div>
      <div id="alarmList" class="muted" style="margin-top:4px;"></div>
    </div>
    <div style="display:flex; gap:6px;">
      <button id="ackBtn" style="border-color:var(--bad);">ACKNOWLEDGE</button>
    </div>
  </div>
</div>

<section class="kpis" id="kpis"></section>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th data-key="name">Name</th>
        <th data-key="status">Status</th>
        <th data-key="host">Host</th>
        <th data-key="method">Method</th>
        <th data-key="latency">Latency</th>
        <th data-key="loss">Loss</th>
        <th>Trend</th>
        <th data-key="last_seen">Last Seen</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
</div>

<footer id="foot"></footer>

<script>
const intervalMs = {{interval}} * 1000;
let autoRefresh = true;
let lastData = {};
let sortKey = "name";
let sortDir = 1;

const qEl = document.getElementById("q");
const sortEl = document.getElementById("sort");
const rowsEl = document.getElementById("rows");
const kpisEl = document.getElementById("kpis");
const footEl = document.getElementById("foot");
const autoBtn = document.getElementById("autoBtn");
const refreshBtn = document.getElementById("refreshBtn");

// alarm refs + state
const alarmEl = document.getElementById("alarm");
const alarmTextEl = document.getElementById("alarmText");
const alarmListEl = document.getElementById("alarmList");
const ackBtn = document.getElementById("ackBtn");
let acknowledged = false;
let lastDownSignature = "";

// ---------- Sparkline ----------
function sparkline(canvas, points){
  const ctx = canvas.getContext("2d");
  const w = canvas.width = canvas.clientWidth * devicePixelRatio;
  const h = canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0,0,w,h);

  const vals = points.map(p=>p[1]).filter(v=>v!==null);
  if(vals.length === 0){
    ctx.fillStyle = "rgba(230,232,235,0.6)";
    ctx.fillText("-", 8, h/2);
    return;
  }
  const min = Math.min(...vals), max = Math.max(...vals);
  const pad = 4 * devicePixelRatio;
  const step = w / Math.max(points.length-1,1);
  const y = v => (max===min) ? h/2 : h-pad - ((v-min)/(max-min))*(h-2*pad);

  ctx.lineWidth = 2 * devicePixelRatio;
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent');
  ctx.beginPath();
  points.forEach((p,i)=>{
    const v = p[1];
    const x = i*step;
    const yy = (v===null)? h-pad : y(v);
    if(i===0) ctx.moveTo(x,yy); else ctx.lineTo(x,yy);
  });
  ctx.stroke();
}

function lossClass(loss){
  if(loss >= 20) return "bad";
  if(loss >= 5) return "warn";
  return "good";
}

function computeKPIs(list){
  const total = list.length;
  const up = list.filter(x => x.up === true).length;
  const down = list.filter(x => x.up === false).length;

  const latVals = list.map(x=>x.latency_ms).filter(v=>v!==null);
  const avgLat = latVals.length ? (latVals.reduce((a,b)=>a+b,0)/latVals.length) : null;

  const avgLoss = total ? (list.reduce((a,b)=>a+b.loss_pct,0)/total) : 0;
  return {total, up, down, avgLat, avgLoss};
}

function renderKPIs(k){
  kpisEl.innerHTML = `
    <div class="kpi"><div><div class="label">Hosts</div><div class="value">${k.total}</div></div><div class="tag">inventory</div></div>
    <div class="kpi"><div><div class="label">Up</div><div class="value" style="color:var(--good)">${k.up}</div></div><div class="tag">operational</div></div>
    <div class="kpi"><div><div class="label">Down</div><div class="value" style="color:var(--bad)">${k.down}</div></div><div class="tag">attention</div></div>
    <div class="kpi"><div><div class="label">Avg Latency</div><div class="value">${k.avgLat===null?"-":k.avgLat.toFixed(1)+" ms"}</div></div><div class="tag">fleet</div></div>
    <div class="kpi"><div><div class="label">Avg Loss</div><div class="value">${k.avgLoss.toFixed(1)}%</div></div><div class="tag">overall</div></div>
  `;
}

// ---------- Alarm ----------
function renderAlarm(list){
  const down = list.filter(x => x.up === false);

  if(down.length === 0){
    alarmEl.style.display = "none";
    acknowledged = false;
    lastDownSignature = "";
    return;
  }

  const sig = down.map(d => d.host).sort().join("|");
  if(sig !== lastDownSignature){
    acknowledged = false;
    lastDownSignature = sig;
  }

  if(acknowledged){
    alarmEl.style.display = "none";
    return;
  }

  alarmEl.style.display = "block";
  alarmTextEl.textContent = `${down.length} host(s) DOWN — immediate attention required.`;
  alarmListEl.textContent = down.map(d => `${d.name} (${d.host})`).join(", ");
}

function sortList(list){
  const k = sortKey, dir = sortDir;
  return list.sort((a,b)=>{
    let va, vb;
    if(k==="status"){ va=(a.up===true)?1:0; vb=(b.up===true)?1:0; }
    else if(k==="latency"){ va=a.latency_ms ?? 999999; vb=b.latency_ms ?? 999999; }
    else if(k==="loss"){ va=a.loss_pct; vb=b.loss_pct; }
    else { va=(a[k]??"").toString().toLowerCase(); vb=(b[k]??"").toString().toLowerCase(); }
    if(va<vb) return -1*dir; if(va>vb) return 1*dir; return 0;
  });
}

function renderTable(data){
  const list = Object.values(data);

  renderAlarm(list);

  const q = qEl.value.trim().toLowerCase();
  const filtered = q
    ? list.filter(x=>x.name.toLowerCase().includes(q) || x.host.toLowerCase().includes(q))
    : list;

  sortList(filtered);
  renderKPIs(computeKPIs(filtered));

  rowsEl.innerHTML = "";
  filtered.forEach(h=>{
    const tr = document.createElement("tr");

    const isUp = (h.up === true);
    const isDown = (h.up === false);
    const statusText = isUp ? "UP" : (isDown ? "DOWN" : "UNKNOWN");
    const statusClass = isUp ? "up" : "down";

    const latency = (h.latency_ms!==null) ? h.latency_ms.toFixed(1)+" ms" : "-";

    tr.innerHTML = `
      <td><div style="font-weight:800;">${h.name}</div></td>
      <td><span class="pill ${statusClass}"><span class="dot ${isUp?"good":"bad"}"></span>${statusText}</span></td>
      <td class="muted">${h.host}</td>
      <td class="muted">${h.method || "ICMP"}</td>
      <td>${latency}</td>
      <td class="loss ${lossClass(h.loss_pct)}">${h.loss_pct.toFixed(1)}%</td>
      <td><canvas></canvas></td>
      <td class="muted">${h.last_seen || "-"}</td>
    `;
    sparkline(tr.querySelector("canvas"), h.history);
    rowsEl.appendChild(tr);
  });

  footEl.textContent = `Showing ${filtered.length}/${list.length} hosts • last update ${new Date().toLocaleTimeString()}`;
}

async function fetchStatus(){
  const r = await fetch("/api/status");
  return r.json();
}

async function tick(){
  try{
    lastData = await fetchStatus();
    renderTable(lastData);
  }catch(e){ console.error(e); }
}

setInterval(()=>{ if(autoRefresh) tick(); }, intervalMs);

autoBtn.onclick = ()=>{
  autoRefresh = !autoRefresh;
  autoBtn.classList.toggle("active", autoRefresh);
  autoBtn.textContent = autoRefresh ? "AUTO REFRESH: ON" : "AUTO REFRESH: OFF";
};
refreshBtn.onclick = tick;
sortEl.onchange = ()=>{ sortKey = sortEl.value; renderTable(lastData); };

document.querySelectorAll("th[data-key]").forEach(th=>{
  th.onclick = ()=>{
    const k = th.dataset.key;
    if(sortKey===k) sortDir*=-1; else { sortKey=k; sortDir=1; sortEl.value=k; }
    renderTable(lastData);
  };
});
qEl.oninput = ()=>renderTable(lastData);

ackBtn.onclick = ()=>{
  acknowledged = true;
  alarmEl.style.display = "none";
};

tick();
</script>
</body>
</html>
"""

@app.route("/")
def dashboard():
    return render_template_string(HTML, interval=PING_INTERVAL_SEC, logfile=os.path.basename(LOG_FILE))

@app.route("/api/status")
def api_status():
    with state_lock:
        payload = {}
        for host, s in state.items():
            payload[host] = {
                "name": s["name"],
                "host": s["host"],
                "up": s["up"],
                "latency_ms": s["latency_ms"],
                "last_seen": s["last_seen"],
                "loss_pct": s["loss_pct"],
                "history": list(s["history"]),
                "method": s.get("method", "ICMP"),
            }
        return jsonify(payload)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

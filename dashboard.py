#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nado Bot Dashboard — Real-time monitoring + Bot Control
Run  : python dashboard.py
Open : http://localhost:5000
"""

import re, os, sys, subprocess, threading, time
from flask import Flask, jsonify, render_template_string, request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app  = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
LOG  = os.path.join(BASE, "scanner.log")
LOCK = os.path.join(BASE, ".bot.lock")

# ── Bot Process Management ────────────────────────────────────────────────────

_bot_proc  = None
_proc_lock = threading.Lock()

def get_lock_pid():
    try:
        if os.path.exists(LOCK):
            with open(LOCK) as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None

def pid_alive(pid: int) -> bool:
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=3
        )
        return str(pid) in r.stdout
    except Exception:
        return False

def bot_running_state():
    global _bot_proc
    with _proc_lock:
        if _bot_proc and _bot_proc.poll() is None:
            return True, _bot_proc.pid
    pid = get_lock_pid()
    if pid and pid_alive(pid):
        return True, pid
    return False, None

# ── Log Reader ────────────────────────────────────────────────────────────────

def read_lines(n: int = 600):
    if not os.path.exists(LOG):
        return []
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []

# ── PnL History Parser ────────────────────────────────────────────────────────

def parse_pnl_history():
    lines = read_lines(2000)
    per_ts   = {}   # ts_str -> {symbol: pnl_value}
    running,_ = bot_running_state()

    for line in lines:
        ts = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        pm = re.search(
            r"([A-Z0-9]+-PERP)\s+(?:LONG|SHORT).*?PnL=([+-]?[\d.]+)\s*\([+-]?[\d.]+%\)",
            line
        )
        if ts and pm:
            t   = ts.group(1)
            sym = pm.group(1)
            pnl = float(pm.group(2))
            if t not in per_ts:
                per_ts[t] = {}
            per_ts[t][sym] = pnl

    points = []
    for ts_str in sorted(per_ts.keys()):
        total = sum(per_ts[ts_str].values())
        points.append({"t": ts_str[11:], "v": round(total, 4)})  # HH:MM:SS

    # Deduplicate — keep last per minute
    seen, deduped = set(), []
    for p in reversed(points):
        key = p["t"][:5]   # HH:MM
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    deduped.reverse()
    return deduped[-60:]  # max 60 data points

# ── State Parser ──────────────────────────────────────────────────────────────

def parse_state():
    lines = read_lines(600)
    running, pid = bot_running_state()
    state = {
        "balance"       : None,
        "positions"     : [],
        "cycle"         : 0,
        "sentiment"     : "neutral",
        "sentiment_score": 0.0,
        "signals"       : [],
        "last_update"   : "—",
        "bot_running"   : running,
        "bot_pid"       : pid,
        "recent_orders" : [],
        "top_picks"     : [],
        "wallet"        : "—",
        "max_positions" : 2,
        "active_count"  : 0,
        "total_pnl"     : 0.0,
    }

    pos_map    = {}   # global fallback
    cycle_pos  = {}   # cycle_num -> {symbol: data}
    cycle_sigs = {}
    orders     = []
    cur_cycle  = 0

    for line in lines:
        t = line.strip()

        ts = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", t)
        ts_str = ts.group(1) if ts else ""
        if ts_str:
            state["last_update"] = ts_str

        w = re.search(r"Wallet\s*:\s*(0x[0-9a-fA-F]{40,})", t)
        if w:
            a = w.group(1)
            state["wallet"] = a[:8] + "..." + a[-6:]

        c = re.search(r"Cycle #(\d+)", t)
        if c:
            cur_cycle = int(c.group(1))
            state["cycle"] = cur_cycle
            cycle_sigs = {}

        b = re.search(r"Balance:\s*\$([\d.]+)", t)
        if b:
            state["balance"] = float(b.group(1))

        ac = re.search(r"Posisi aktif:\s*(\d+)/(\d+)", t)
        if ac:
            state["active_count"]  = int(ac.group(1))
            state["max_positions"] = int(ac.group(2))

        sn = re.search(r"News sentiment:\s*(\w+)\s*\(score=([+-]?[\d.]+)\)", t)
        if sn:
            state["sentiment"]       = sn.group(1).lower()
            state["sentiment_score"] = float(sn.group(2))

        # Position monitoring
        pm = re.search(
            r"([A-Z0-9]+-PERP)\s+(LONG|SHORT)(.*?)\|"
            r"\s*entry=([\d.]+)\s*\S+\s*([\d.]+)\s*\|"
            r"\s*PnL=([+-]?[\d.]+)\s*\(([+-]?[\d.]+)%\)\s*\|"
            r"\s*SL=([\d.]+)\s+TP=([\d.]+)",
            t
        )
        if pm:
            sym = pm.group(1)
            flags = re.sub(r"[^\w✓ ]", "", pm.group(3).strip()).strip()
            _pdata = {
                "symbol" : sym, "side"    : pm.group(2), "flags": flags,
                "entry"  : float(pm.group(4)), "current": float(pm.group(5)),
                "pnl"    : float(pm.group(6)), "pnl_pct": float(pm.group(7)),
                "sl"     : float(pm.group(8)), "tp"     : float(pm.group(9)),
                "time"   : ts_str,
            }
            pos_map[sym] = _pdata
            if cur_cycle not in cycle_pos:
                cycle_pos[cur_cycle] = {}
            cycle_pos[cur_cycle][sym] = _pdata

        # Scanner signal
        sg = re.search(
            r"^\s*\S+\s+(\w+)\s+\|\s+(LONG|SHORT|NEUTRAL)\s+score=([+-]?\d+)/7\s+\|\s+RSI=([\d.]+)\s+\|\s+(\w+)",
            t
        )
        if sg and "entry=" not in t and "PnL=" not in t:
            coin  = sg.group(1)
            side  = sg.group(2)
            score = int(sg.group(3))
            color = "green" if "🟢" in t else ("red" if "🔴" in t else
                    ("green" if (side=="LONG" and score>=2) else
                     "red"   if (side=="SHORT" and score>=2) else "gray"))
            cycle_sigs[coin] = {
                "coin": coin, "side": side, "score": score,
                "rsi" : float(sg.group(4)), "trend": sg.group(5), "color": color,
            }

        tp = re.search(r"Top picks:\s*\[(.+?)\]", t)
        if tp:
            state["top_picks"] = [p.strip().strip("'\"") for p in tp.group(1).split(",")]

        os_ = re.search(
            r">>>\s+SIGNAL:\s+(.+?)\s+(LONG|SHORT)\s+\|\s+score=(\d+)/7\s+\|\s+RSI=([\d.]+)\s+\|\s+lev=([\d.]+)x",
            t
        )
        if os_ and ts_str:
            orders.append({
                "time": ts_str[-8:], "symbol": os_.group(1), "side": os_.group(2),
                "score": int(os_.group(3)), "rsi": float(os_.group(4)),
                "lev": float(os_.group(5)), "status": "pending",
            })

        if "ORDER PLACED" in t and orders:
            orders[-1]["status"] = "placed"
        if "Order failed" in t and orders:
            orders[-1]["status"] = "failed"

    # Gunakan posisi dari cycle TERBARU saja (bukan akumulasi semua history)
    if cycle_pos:
        latest = max(cycle_pos.keys())
        final_pos = list(cycle_pos[latest].values())
    else:
        final_pos = list(pos_map.values())
    state["positions"]     = final_pos
    state["signals"]       = sorted(cycle_sigs.values(), key=lambda x: abs(x["score"]), reverse=True)
    state["recent_orders"] = orders[-15:]
    state["total_pnl"]     = sum(p["pnl"] for p in state["positions"])
    return state

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template_string(HTML)

@app.route("/api/status")
def api_status():
    return jsonify(parse_state())

@app.route("/api/logs")
def api_logs():
    lines = read_lines(150)
    return jsonify({"lines": [l.rstrip() for l in lines]})

@app.route("/api/pnl_history")
def api_pnl_history():
    return jsonify({"points": parse_pnl_history()})

@app.route("/api/candles/<symbol>")
def api_candles(symbol):
    """Fetch OHLC candles from Binance public API — no auth needed."""
    import urllib.request as urlreq
    # "SOL-PERP" -> "SOLUSDT", "XRP-PERP" -> "XRPUSDT"
    coin     = symbol.upper().replace("-PERP", "").replace("-", "") + "USDT"
    interval = request.args.get("interval", "5m")
    limit    = min(int(request.args.get("limit", "100")), 200)
    url = (f"https://api.binance.com/api/v3/klines"
           f"?symbol={coin}&interval={interval}&limit={limit}")
    try:
        with urlreq.urlopen(url, timeout=6) as resp:
            raw = json.loads(resp.read())
        candles = [{"time": int(k[0])//1000, "open": float(k[1]),
                    "high": float(k[2]),  "low":  float(k[3]),
                    "close": float(k[4]), "vol":  float(k[5])} for k in raw]
        return jsonify({"ok": True, "candles": candles, "binance_sym": coin})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "candles": []})


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    global _bot_proc
    running, pid = bot_running_state()
    if running:
        return jsonify({"ok": False, "msg": f"Bot sudah jalan (PID {pid})"})
    # Remove stale lockfile
    if os.path.exists(LOCK):
        try: os.remove(LOCK)
        except: pass
    try:
        with _proc_lock:
            _bot_proc = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=BASE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0,
            )
        time.sleep(1.0)
        return jsonify({"ok": True, "msg": f"Bot started (PID {_bot_proc.pid})", "pid": _bot_proc.pid})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    global _bot_proc
    stopped = False
    with _proc_lock:
        if _bot_proc and _bot_proc.poll() is None:
            try:
                _bot_proc.terminate()
                _bot_proc.wait(timeout=5)
            except Exception:
                _bot_proc.kill()
            _bot_proc = None
            stopped = True

    pid = get_lock_pid()
    if pid and pid_alive(pid):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
            stopped = True
        except Exception:
            pass

    if os.path.exists(LOCK):
        try: os.remove(LOCK)
        except: pass

    return jsonify({"ok": True, "msg": "Bot stopped" if stopped else "No bot process found"})

@app.route("/api/bot/control_status")
def api_bot_control_status():
    running, pid = bot_running_state()
    return jsonify({"running": running, "pid": pid})

# ── HTML Template ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Nado Bot Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#060c1c;--bg2:#0c1830;--bg3:#080f22;
  --surface:rgba(10,20,45,0.88);
  --border:rgba(0,195,255,0.14);--border2:rgba(255,255,255,0.06);
  --cyan:#00d4ff;--cyan-d:rgba(0,212,255,0.12);
  --green:#00ff88;--green-d:rgba(0,255,136,0.12);
  --red:#ff3366;--red-d:rgba(255,51,102,0.12);
  --yellow:#ffd700;--purple:#a855f7;--purple-d:rgba(168,85,247,0.12);
  --text:#dde8f5;--muted:#4d6080;
  --font:'Inter',sans-serif;--mono:'JetBrains Mono',monospace;
}
html{scroll-behavior:smooth}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(ellipse 70% 40% at 5% 10%,rgba(0,180,255,.05) 0%,transparent 60%),
    radial-gradient(ellipse 60% 45% at 95% 90%,rgba(168,85,247,.04) 0%,transparent 60%),
    radial-gradient(ellipse 50% 50% at 50% 50%,rgba(0,255,136,.02) 0%,transparent 60%)}
.wrap{position:relative;z-index:1;max-width:1700px;margin:0 auto;padding:0 18px 40px}

/* ── Header ── */
header{display:flex;align-items:center;justify-content:space-between;
  padding:14px 0;border-bottom:1px solid var(--border);margin-bottom:20px;flex-wrap:wrap;gap:10px}
.h-left{display:flex;align-items:center;gap:14px}
.bot-icon{width:46px;height:46px;
  background:linear-gradient(135deg,rgba(0,212,255,.15),rgba(168,85,247,.15));
  border:1px solid var(--border);border-radius:12px;
  display:flex;align-items:center;justify-content:center;font-size:24px}
.bot-name{font-size:18px;font-weight:800;letter-spacing:-.4px}
.bot-name em{color:var(--cyan);font-style:normal}
.bot-sub{font-size:10.5px;color:var(--muted);margin-top:2px;font-family:var(--mono)}
.h-right{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.wallet{font-family:var(--mono);font-size:11px;color:var(--muted);
  background:var(--bg2);border:1px solid var(--border2);border-radius:8px;padding:5px 11px}
.cycle-tag{font-family:var(--mono);font-size:11px;color:var(--cyan);
  background:var(--cyan-d);border:1px solid rgba(0,212,255,.2);border-radius:6px;padding:4px 10px}
.status-row{display:flex;align-items:center;gap:7px;font-size:12px;font-weight:500}
.dot{width:8px;height:8px;border-radius:50%;background:var(--red)}
.dot.on{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 8px var(--green)}50%{box-shadow:0 0 18px var(--green);opacity:.7}}
.upd{font-size:10.5px;color:var(--muted);font-family:var(--mono)}
.rdot{width:6px;height:6px;border-radius:50%;background:var(--cyan);animation:blink 1s infinite;display:none}
.rdot.on{display:block}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

/* ── Stats ── */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
@media(max-width:900px){.stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:480px){.stats{grid-template-columns:1fr}}
.sc{background:var(--surface);border:1px solid var(--border2);border-radius:14px;
  padding:18px 20px;backdrop-filter:blur(14px);
  position:relative;overflow:hidden;transition:border-color .3s,transform .2s}
.sc:hover{border-color:var(--border);transform:translateY(-2px)}
.sc::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--sg,linear-gradient(90deg,var(--cyan),transparent));opacity:.7}
.sc-lbl{font-size:9.5px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.sc-val{font-size:28px;font-weight:800;letter-spacing:-.6px;line-height:1}
.sc-sub{font-size:11px;color:var(--muted);margin-top:6px}
.c-cyan{--sg:linear-gradient(90deg,var(--cyan),transparent)}
.c-green{--sg:linear-gradient(90deg,var(--green),transparent)}
.c-purple{--sg:linear-gradient(90deg,var(--purple),transparent)}
.pos-g{color:var(--green)}.pos-r{color:var(--red)}.pos-n{color:var(--text)}
.slots{display:flex;gap:6px;margin-top:8px}
.sd{width:10px;height:10px;border-radius:50%;background:var(--border2);border:1px solid var(--border);transition:all .3s}
.sd.on{background:var(--cyan);box-shadow:0 0 8px var(--cyan)}
.sent-pill{display:inline-flex;align-items:center;gap:6px;border-radius:20px;
  padding:5px 13px;font-size:12px;font-weight:600;margin-top:4px}
.sent-pill.positive{background:var(--green-d);color:var(--green)}
.sent-pill.negative{background:var(--red-d);color:var(--red)}
.sent-pill.neutral{background:rgba(100,116,128,.15);color:var(--muted)}

/* ── Control Panel ── */
.ctrl-panel{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
@media(max-width:800px){.ctrl-panel{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--border2);
  border-radius:16px;padding:18px 20px;backdrop-filter:blur(14px)}
.card-hd{font-size:10px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;
  color:var(--muted);margin-bottom:15px;display:flex;align-items:center;justify-content:space-between}
.badge{font-size:10px;font-weight:500;background:var(--cyan-d);color:var(--cyan);
  border:1px solid rgba(0,212,255,.2);border-radius:20px;padding:2px 9px}

.ctrl-body{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.ctrl-status{display:flex;align-items:center;gap:10px;flex:1}
.ctrl-status-dot{width:12px;height:12px;border-radius:50%;background:var(--red);flex-shrink:0}
.ctrl-status-dot.on{background:var(--green);box-shadow:0 0 10px var(--green);animation:pulse 2s infinite}
.ctrl-status-text{font-weight:600;font-size:14px}
.ctrl-pid{font-family:var(--mono);font-size:11px;color:var(--muted)}

.btn{display:inline-flex;align-items:center;gap:7px;
  padding:9px 20px;border-radius:10px;font-size:13px;font-weight:600;
  border:none;cursor:pointer;transition:all .2s;white-space:nowrap}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-start{background:linear-gradient(135deg,rgba(0,255,136,.2),rgba(0,255,136,.1));
  color:var(--green);border:1px solid rgba(0,255,136,.35)}
.btn-start:hover:not(:disabled){background:linear-gradient(135deg,rgba(0,255,136,.3),rgba(0,255,136,.15));
  box-shadow:0 0 16px rgba(0,255,136,.2);transform:translateY(-1px)}
.btn-stop{background:linear-gradient(135deg,rgba(255,51,102,.2),rgba(255,51,102,.1));
  color:var(--red);border:1px solid rgba(255,51,102,.35)}
.btn-stop:hover:not(:disabled){background:linear-gradient(135deg,rgba(255,51,102,.3),rgba(255,51,102,.15));
  box-shadow:0 0 16px rgba(255,51,102,.2);transform:translateY(-1px)}
.btn-icon{font-size:16px}

.ctrl-msg{font-size:11.5px;padding:6px 12px;border-radius:7px;margin-top:8px;
  display:none;font-family:var(--mono)}
.ctrl-msg.ok{background:var(--green-d);color:var(--green);border:1px solid rgba(0,255,136,.2);display:block}
.ctrl-msg.err{background:var(--red-d);color:var(--red);border:1px solid rgba(255,51,102,.2);display:block}

/* Config grid */
.cfg-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.cfg-item{background:var(--bg2);border:1px solid var(--border2);border-radius:9px;padding:10px 12px;text-align:center}
.cfg-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px}
.cfg-val{font-size:18px;font-weight:800;font-family:var(--mono);color:var(--cyan)}
.cfg-unit{font-size:10px;color:var(--muted);margin-top:2px}

/* ── Candle Chart ── */
.candle-wrap{margin-bottom:16px}
.candle-toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.sym-select{font-family:var(--mono);font-size:12px;background:var(--bg2);
  border:1px solid var(--border2);color:var(--text);border-radius:8px;
  padding:6px 12px;outline:none;cursor:pointer;transition:border-color .2s}
.sym-select:focus{border-color:var(--cyan)}
.iv-btn{font-family:var(--mono);font-size:11px;background:var(--bg2);
  border:1px solid var(--border2);color:var(--muted);border-radius:6px;
  padding:5px 10px;cursor:pointer;transition:all .2s}
.iv-btn:hover{border-color:var(--border);color:var(--text)}
.iv-btn.active{background:var(--cyan-d);border-color:rgba(0,212,255,.3);color:var(--cyan);font-weight:600}
.candle-body{height:320px;border-radius:10px;overflow:hidden;background:rgba(3,7,16,.5)}
.candle-msg{display:flex;align-items:center;justify-content:center;
  height:320px;color:var(--muted);font-size:13px}

/* ── PnL Chart ── */
.chart-wrap{margin-bottom:16px}
.chart-inner{position:relative;height:180px;padding:4px}
.chart-no-data{display:flex;align-items:center;justify-content:center;
  height:180px;color:var(--muted);font-size:13px}

/* ── Main grid ── */
.main{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:1050px){.main{grid-template-columns:1fr}}
.left-col{display:flex;flex-direction:column;gap:16px}

/* ── Position Cards ── */
.pos-list{display:flex;flex-direction:column;gap:10px}
.pc{background:var(--bg2);border:1px solid var(--border2);border-radius:12px;
  padding:14px 15px;transition:border-color .3s}
.pc:hover{border-color:rgba(0,212,255,.2)}
.pc.long{border-left:3px solid var(--green)}
.pc.short{border-left:3px solid var(--red)}
.pc-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.pc-sym{font-weight:700;font-size:15px;letter-spacing:-.3px}
.pc-flag{font-size:10px;color:var(--cyan);margin-left:6px;font-family:var(--mono)}
.side-b{font-size:10px;font-weight:700;letter-spacing:1px;border-radius:6px;padding:3px 10px}
.side-b.long{background:var(--green-d);color:var(--green);border:1px solid rgba(0,255,136,.3)}
.side-b.short{background:var(--red-d);color:var(--red);border:1px solid rgba(255,51,102,.3)}
.pc-prices{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}
.pi{text-align:center}
.pi-lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.pi-val{font-family:var(--mono);font-size:13px;font-weight:500}
.pnl-wrap{margin-bottom:8px}
.pnl-row{display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px}
.bar-track{height:4px;background:var(--border2);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width .6s ease;min-width:3px}
.bar-fill.p{background:var(--green)}.bar-fill.n{background:var(--red)}
.pc-lvl{display:flex;gap:14px;font-size:11px;color:var(--muted)}
.pc-lvl span{font-family:var(--mono)}
.empty{text-align:center;padding:36px 20px;color:var(--muted);font-size:13px}
.empty-ic{font-size:36px;margin-bottom:10px}

/* ── Signal Grid ── */
.sig-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:7px}
.st{background:var(--bg2);border:1px solid var(--border2);border-radius:10px;
  padding:10px 7px;text-align:center;transition:all .2s;cursor:default}
.st:hover{transform:translateY(-2px);border-color:var(--border)}
.st.green{border-color:rgba(0,255,136,.3)}.st.red{border-color:rgba(255,51,102,.3)}
.st.top{border-color:rgba(0,212,255,.5);box-shadow:0 0 12px rgba(0,212,255,.15)}
.st-coin{font-size:10.5px;font-weight:700;margin-bottom:4px}
.st-side{font-size:9px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;margin-bottom:4px}
.st-side.long{color:var(--green)}.st-side.short{color:var(--red)}.st-side.neutral{color:var(--muted)}
.st-score{font-size:18px;font-weight:800;font-family:var(--mono);line-height:1}
.st-score.green{color:var(--green)}.st-score.red{color:var(--red)}.st-score.gray{color:var(--muted)}
.st-rsi{font-size:9px;color:var(--muted);margin-top:3px;font-family:var(--mono)}

/* ── Orders ── */
.ord-list{display:flex;flex-direction:column;gap:6px}
.or{display:flex;align-items:center;gap:10px;
  background:var(--bg2);border:1px solid var(--border2);border-radius:8px;padding:8px 12px;font-size:12px}
.or-time{font-family:var(--mono);color:var(--muted);font-size:10.5px;min-width:60px}
.or-sym{font-weight:600;flex:1}
.or-side.long{color:var(--green);font-weight:600}.or-side.short{color:var(--red);font-weight:600}
.or-meta{color:var(--muted);font-size:10.5px;font-family:var(--mono)}
.os{font-size:9.5px;padding:2px 8px;border-radius:4px;font-weight:600;letter-spacing:.5px}
.os.placed{background:var(--green-d);color:var(--green)}
.os.failed{background:var(--red-d);color:var(--red)}
.os.pending{background:rgba(255,215,0,.1);color:var(--yellow)}

/* ── Log ── */
.log-wrap{background:var(--surface);border:1px solid var(--border2);border-radius:16px;overflow:hidden}
.log-hd{display:flex;align-items:center;justify-content:space-between;
  padding:13px 20px;border-bottom:1px solid var(--border2)}
.log-title{font-size:10px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted)}
.log-ctrl{display:flex;align-items:center;gap:10px}
.log-inp{font-family:var(--mono);font-size:11px;background:var(--bg2);
  border:1px solid var(--border2);color:var(--text);border-radius:7px;padding:5px 11px;
  outline:none;transition:border-color .2s;width:180px}
.log-inp:focus{border-color:var(--cyan)}
.log-cnt{font-size:10.5px;color:var(--muted);font-family:var(--mono)}
.log-body{height:260px;overflow-y:auto;padding:12px 16px;
  font-family:var(--mono);font-size:11.5px;line-height:1.65;background:rgba(3,7,16,.55)}
.log-body::-webkit-scrollbar{width:4px}
.log-body::-webkit-scrollbar-thumb{background:rgba(0,180,255,.2);border-radius:2px}
.ll{white-space:pre-wrap;word-break:break-all;padding:1px 0}
.ll.er{color:#ff6b8a}.ll.wa{color:var(--yellow)}.ll.cy{color:var(--cyan);font-weight:600}
.ll.od{color:var(--green);font-weight:600}.ll.ps{color:#93c5fd}.ll.sg{color:#c084fc}.ll.in{color:#5a738a}
</style>
</head>
<body>
<div class="wrap">

<!-- HEADER -->
<header>
  <div class="h-left">
    <div class="bot-icon">&#x1F916;</div>
    <div>
      <div class="bot-name">Nado <em>Auto Trader</em></div>
      <div class="bot-sub">Multi-Pair Scanner v2 &middot; Software Isolated Margin &middot; 2 Slots</div>
    </div>
  </div>
  <div class="h-right">
    <div class="wallet" id="wallet">&#x2014;</div>
    <div class="cycle-tag" id="cycle">Cycle #&#x2014;</div>
    <div class="status-row">
      <div class="dot" id="sdot"></div>
      <span id="stxt">Connecting...</span>
    </div>
    <div class="rdot" id="rdot"></div>
    <div class="upd" id="upd">&#x2014;</div>
  </div>
</header>

<!-- STATS -->
<div class="stats">
  <div class="sc c-cyan">
    <div class="sc-lbl">Balance</div>
    <div class="sc-val" id="s-bal">&#x2014;</div>
    <div class="sc-sub">USDT</div>
  </div>
  <div class="sc c-green">
    <div class="sc-lbl">Active Positions</div>
    <div class="sc-val" id="s-pos">&#x2014;/&#x2014;</div>
    <div class="slots" id="s-slots"></div>
  </div>
  <div class="sc" id="pnl-card">
    <div class="sc-lbl">Total PnL</div>
    <div class="sc-val" id="s-pnl">&#x2014;</div>
    <div class="sc-sub">Unrealized</div>
  </div>
  <div class="sc c-purple">
    <div class="sc-lbl">Market Sentiment</div>
    <div id="s-sent"><span class="sent-pill neutral">&#x1F4CA; NEUTRAL</span></div>
    <div class="sc-sub" id="s-sent-score">score: 0.00</div>
  </div>
</div>

<!-- CONTROL PANEL -->
<div class="ctrl-panel">
  <!-- Bot Control -->
  <div class="card">
    <div class="card-hd">Bot Control <span class="badge" id="ctrl-pid-badge">PID: &#x2014;</span></div>
    <div class="ctrl-body">
      <div class="ctrl-status">
        <div class="ctrl-status-dot" id="ctrl-dot"></div>
        <div>
          <div class="ctrl-status-text" id="ctrl-status">Checking...</div>
          <div class="ctrl-pid" id="ctrl-pid">&#x2014;</div>
        </div>
      </div>
      <button class="btn btn-start" id="btn-start" onclick="botControl('start')">
        <span class="btn-icon">&#x25B6;</span> Start Bot
      </button>
      <button class="btn btn-stop" id="btn-stop" onclick="botControl('stop')">
        <span class="btn-icon">&#x25A0;</span> Stop Bot
      </button>
    </div>
    <div class="ctrl-msg" id="ctrl-msg"></div>
  </div>

  <!-- Config / Info -->
  <div class="card">
    <div class="card-hd">Bot Configuration</div>
    <div class="cfg-grid">
      <div class="cfg-item">
        <div class="cfg-lbl">Max Positions</div>
        <div class="cfg-val" id="cfg-maxpos">2</div>
        <div class="cfg-unit">slots</div>
      </div>
      <div class="cfg-item">
        <div class="cfg-lbl">Margin / Pos</div>
        <div class="cfg-val">15%</div>
        <div class="cfg-unit">of balance</div>
      </div>
      <div class="cfg-item">
        <div class="cfg-lbl">Min Score</div>
        <div class="cfg-val">2</div>
        <div class="cfg-unit">out of 7</div>
      </div>
      <div class="cfg-item">
        <div class="cfg-lbl">Min Notional</div>
        <div class="cfg-val">$100</div>
        <div class="cfg-unit">USDT</div>
      </div>
      <div class="cfg-item">
        <div class="cfg-lbl">Scan Interval</div>
        <div class="cfg-val">60</div>
        <div class="cfg-unit">seconds</div>
      </div>
      <div class="cfg-item">
        <div class="cfg-lbl">Max Leverage</div>
        <div class="cfg-val">20x</div>
        <div class="cfg-unit">software cap</div>
      </div>
    </div>
  </div>
</div>

<!-- PnL CHART -->
<div class="card chart-wrap">
  <div class="card-hd">
    PnL History (Unrealized)
    <span class="badge" id="chart-pts">0 points</span>
  </div>
  <div class="chart-inner" id="chart-inner">
    <canvas id="pnl-chart"></canvas>
  </div>
</div>

<!-- CANDLE CHART -->
<div class="card candle-wrap">
  <div class="card-hd">
    Candlestick Chart
    <span style="display:flex;align-items:center;gap:8px">
      <span class="badge" id="candle-sym-badge">No position</span>
      <span id="candle-price" style="font-family:var(--mono);font-size:11px;color:var(--cyan)"></span>
    </span>
  </div>
  <div class="candle-toolbar">
    <select class="sym-select" id="sym-select" onchange="onSymChange()">
      <option value="">-- Select Symbol --</option>
    </select>
    <div style="display:flex;gap:4px">
      <button class="iv-btn active" onclick="setInterval2('1m',this)">1m</button>
      <button class="iv-btn" onclick="setInterval2('5m',this)">5m</button>
      <button class="iv-btn" onclick="setInterval2('15m',this)">15m</button>
      <button class="iv-btn" onclick="setInterval2('1h',this)">1h</button>
      <button class="iv-btn" onclick="setInterval2('4h',this)">4h</button>
    </div>
    <span style="font-size:11px;color:var(--muted)" id="candle-status">—</span>
  </div>
  <div class="candle-body" id="candle-body">
    <div class="candle-msg">Select a symbol to load chart</div>
  </div>
</div>

<div class="main">
  <div class="left-col">
    <!-- Active Positions -->
    <div class="card">
      <div class="card-hd">Active Positions <span class="badge" id="pos-badge">0 / 2</span></div>
      <div class="pos-list" id="pos-list">
        <div class="empty"><div class="empty-ic">&#x1F4ED;</div>No active positions</div>
      </div>
    </div>
    <!-- Recent Signals -->
    <div class="card">
      <div class="card-hd">Recent Signals <span class="badge" id="ord-badge">0</span></div>
      <div class="ord-list" id="ord-list">
        <div class="empty" style="padding:20px 0">No signals yet</div>
      </div>
    </div>
  </div>

  <!-- Scanner -->
  <div class="card">
    <div class="card-hd">Scanner &middot; All Coins <span class="badge" id="sig-badge">0 coins</span></div>
    <div class="sig-grid" id="sig-grid">
      <div style="grid-column:1/-1;color:var(--muted);text-align:center;padding:28px 0;font-size:13px">
        Waiting for scan...
      </div>
    </div>
  </div>
</div>

<!-- LIVE LOG -->
<div class="log-wrap">
  <div class="log-hd">
    <div class="log-title">Live Log &mdash; scanner.log</div>
    <div class="log-ctrl">
      <input class="log-inp" id="log-f" type="text" placeholder="Filter..." oninput="filterLog()">
      <span class="log-cnt" id="log-cnt">0 lines</span>
    </div>
  </div>
  <div class="log-body" id="log-body">
    <div class="ll in">Connecting...</div>
  </div>
</div>

</div><!-- /wrap -->

<script>
let rawLines = [], autoScroll = true, pnlChart = null;

// ── Candlestick Chart State ───────────────────────────────────────────────────
let candleChart = null, candleSeries = null, candleInterval = '5m';
let candleSymbol = '', candlePriceLine = null, candleSLLine = null, candleTPLine = null;
let lastPositions = [];


// ── Log scroll detection ──────────────────────────────────────────────────────
document.getElementById('log-body').addEventListener('scroll', function(){
  autoScroll = (this.scrollHeight - this.scrollTop - this.clientHeight < 50);
});

const f = (v, d=4) => (v==null ? '—' : Number(v).toFixed(d));

// ── Chart init ────────────────────────────────────────────────────────────────
function initChart(){
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets:[{
        label: 'Total PnL (USDT)',
        data: [],
        borderColor: '#00d4ff',
        backgroundColor: (ctx2) => {
          const g = ctx2.chart.ctx.createLinearGradient(0, 0, 0, 180);
          g.addColorStop(0, 'rgba(0,212,255,0.18)');
          g.addColorStop(1, 'rgba(0,212,255,0.01)');
          return g;
        },
        borderWidth: 2,
        pointRadius: 3,
        pointBackgroundColor: '#00d4ff',
        pointHoverRadius: 6,
        fill: true,
        tension: 0.4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 400, easing: 'easeInOutQuart' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(6,12,28,0.95)',
          borderColor: 'rgba(0,212,255,0.3)',
          borderWidth: 1,
          titleColor: '#4d6080',
          bodyColor: '#00d4ff',
          bodyFont: { family: 'JetBrains Mono', size: 12, weight: '600' },
          callbacks: {
            label: ctx3 => {
              const v = ctx3.parsed.y;
              return ` PnL: ${v>=0?'+':''}$${v.toFixed(4)}`;
            }
          }
        }
      },
      scales: {
        x: {
          grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
          ticks: { color: '#4d6080', font: { family: 'JetBrains Mono', size: 10 }, maxTicksLimit: 10 }
        },
        y: {
          grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
          ticks: {
            color: '#4d6080', font: { family: 'JetBrains Mono', size: 10 },
            callback: v => (v>=0?'+':'') + '$' + v.toFixed(3)
          }
        }
      }
    }
  });
}

function updateChart(points){
  if(!pnlChart || !points.length){
    document.getElementById('chart-pts').textContent = '0 points';
    return;
  }
  document.getElementById('chart-pts').textContent = points.length + ' points';
  pnlChart.data.labels   = points.map(p => p.t);
  pnlChart.data.datasets[0].data = points.map(p => p.v);

  // Dynamic color based on last value
  const last = points[points.length-1].v;
  pnlChart.data.datasets[0].borderColor = last >= 0 ? '#00ff88' : '#ff3366';
  pnlChart.data.datasets[0].backgroundColor = (ctx2) => {
    const g = ctx2.chart.ctx.createLinearGradient(0, 0, 0, 180);
    const c = last >= 0 ? '0,255,136' : '255,51,102';
    g.addColorStop(0, `rgba(${c},0.18)`);
    g.addColorStop(1, `rgba(${c},0.01)`);
    return g;
  };
  pnlChart.update();
}

// ── Candlestick Chart ─────────────────────────────────────────────────────────
function initCandleChart(){
  const el = document.getElementById('candle-body');
  el.innerHTML = '';
  candleChart = LightweightCharts.createChart(el, {
    width:  el.clientWidth,
    height: 320,
    layout: { background: { color: '#030710' }, textColor: '#4d6080' },
    grid: {
      vertLines: { color: 'rgba(255,255,255,0.04)' },
      horzLines: { color: 'rgba(255,255,255,0.04)' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: 'rgba(0,195,255,0.1)' },
    timeScale: { borderColor: 'rgba(0,195,255,0.1)', timeVisible: true, secondsVisible: false },
  });
  candleSeries = candleChart.addCandlestickSeries({
    upColor:   '#00ff88', downColor: '#ff3366',
    borderUpColor: '#00ff88', borderDownColor: '#ff3366',
    wickUpColor:   '#00ff88', wickDownColor:   '#ff3366',
  });
  // Resize observer
  new ResizeObserver(()=>{
    if(candleChart) candleChart.applyOptions({ width: el.clientWidth });
  }).observe(el);
}

async function loadCandles(sym){
  if(!sym) return;
  const status = document.getElementById('candle-status');
  status.textContent = 'Loading...';
  document.getElementById('candle-sym-badge').textContent = sym;
  try {
    const r = await fetch(`/api/candles/${encodeURIComponent(sym)}?interval=${candleInterval}&limit=120`).then(r=>r.json());
    if(!r.ok){ status.textContent = r.error || 'Error'; return; }
    if(!candleSeries) initCandleChart();
    candleSeries.setData(r.candles);
    candleChart.timeScale().fitContent();
    status.textContent = `${r.binance_sym} · ${candleInterval} · ${r.candles.length} bars`;
    // Update last price
    if(r.candles.length){
      const lc = r.candles[r.candles.length-1];
      document.getElementById('candle-price').textContent = `$${lc.close.toFixed(4)}`;
    }
    // Draw price lines for active position
    drawPriceLines(sym);
  } catch(e){ status.textContent = 'Fetch error'; }
}

function drawPriceLines(sym){
  if(!candleSeries) return;
  // Remove old lines
  [candlePriceLine, candleSLLine, candleTPLine].forEach(l=>{ try{ if(l) candleSeries.removePriceLine(l); }catch(e){} });
  const pos = lastPositions.find(p => p.symbol === sym);
  if(!pos) return;
  const isLong = pos.side === 'LONG';
  candlePriceLine = candleSeries.createPriceLine({
    price: pos.entry, color: '#00d4ff', lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    title: `Entry ${pos.side}`,
  });
  candleSLLine = candleSeries.createPriceLine({
    price: pos.sl, color: '#ff3366', lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dotted, title: 'SL',
  });
  candleTPLine = candleSeries.createPriceLine({
    price: pos.tp, color: '#00ff88', lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dotted, title: 'TP',
  });
}

function onSymChange(){
  const sel = document.getElementById('sym-select');
  candleSymbol = sel.value;
  if(candleSymbol) loadCandles(candleSymbol);
}

function setInterval2(iv, btn){
  candleInterval = iv;
  document.querySelectorAll('.iv-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  if(candleSymbol) loadCandles(candleSymbol);
}

function updateSymSelector(positions){
  lastPositions = positions;
  const sel = document.getElementById('sym-select');
  const cur = sel.value;
  // Rebuild options
  sel.innerHTML = '<option value="">-- Select Symbol --</option>';
  positions.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.symbol;  opt.textContent = `${p.symbol} (${p.side})`;
    sel.appendChild(opt);
  });
  // Auto-select first active position if nothing selected
  if(!cur && positions.length){
    sel.value = positions[0].symbol;
    candleSymbol = positions[0].symbol;
    if(!candleSeries) initCandleChart();
    loadCandles(candleSymbol);
  } else if(cur) {
    sel.value = cur;
    drawPriceLines(cur);
  }
}


// ── API Calls ─────────────────────────────────────────────────────────────────
async function fetchStatus(){
  const rd = document.getElementById('rdot');
  rd.classList.add('on');
  try {
    const d = await fetch('/api/status').then(r=>r.json());
    render(d);
  } catch(e){
    document.getElementById('sdot').className='dot';
    document.getElementById('stxt').textContent='Offline';
  } finally { rd.classList.remove('on'); }
}

async function fetchLogs(){
  try {
    const d = await fetch('/api/logs').then(r=>r.json());
    rawLines = d.lines; renderLog();
  } catch(e){}
}

async function fetchPnl(){
  try {
    const d = await fetch('/api/pnl_history').then(r=>r.json());
    updateChart(d.points||[]);
  } catch(e){}
}

async function fetchCtrlStatus(){
  try {
    const d = await fetch('/api/bot/control_status').then(r=>r.json());
    updateCtrl(d.running, d.pid);
  } catch(e){}
}

// ── Bot Control ───────────────────────────────────────────────────────────────
async function botControl(action){
  const msg = document.getElementById('ctrl-msg');
  msg.className = 'ctrl-msg';
  msg.textContent = action==='start' ? 'Starting bot...' : 'Stopping bot...';
  msg.style.display = 'block';
  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-stop').disabled  = true;

  try {
    const r = await fetch(`/api/bot/${action}`, {method:'POST'}).then(r=>r.json());
    msg.className = 'ctrl-msg ' + (r.ok ? 'ok' : 'err');
    msg.textContent = r.msg || (r.ok ? 'Done!' : 'Failed');
    setTimeout(()=>{ msg.className='ctrl-msg'; }, 4000);
    await fetchCtrlStatus();
    if(action==='start') setTimeout(fetchStatus, 2000);
  } catch(e){
    msg.className = 'ctrl-msg err';
    msg.textContent = 'Request failed: ' + e.message;
  } finally {
    document.getElementById('btn-start').disabled = false;
    document.getElementById('btn-stop').disabled  = false;
  }
}

function updateCtrl(running, pid){
  document.getElementById('ctrl-dot').className    = 'ctrl-status-dot' + (running?' on':'');
  document.getElementById('ctrl-status').textContent = running ? 'Bot Running' : 'Bot Stopped';
  document.getElementById('ctrl-pid').textContent    = pid ? `PID: ${pid}` : 'Not started';
  document.getElementById('ctrl-pid-badge').textContent = pid ? `PID: ${pid}` : 'PID: —';
  document.getElementById('btn-start').disabled = running;
  document.getElementById('btn-stop').disabled  = !running;
}

// ── Log ───────────────────────────────────────────────────────────────────────
function filterLog(){ renderLog(); }
function renderLog(){
  const fv = document.getElementById('log-f').value.toLowerCase();
  const body = document.getElementById('log-body');
  const lines = fv ? rawLines.filter(l=>l.toLowerCase().includes(fv)) : rawLines;
  document.getElementById('log-cnt').textContent = lines.length + ' lines';
  body.innerHTML = lines.map(l=>{
    const cls = classLog(l);
    return `<div class="ll ${cls}">${l.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
  }).join('');
  if(autoScroll) body.scrollTop = body.scrollHeight;
}
function classLog(l){
  if(l.includes('[ERROR]'))                                    return 'er';
  if(l.includes('[WARNING]'))                                  return 'wa';
  if(l.includes('Cycle #'))                                    return 'cy';
  if(l.includes('ORDER PLACED')||l.includes('>>> SIGNAL'))     return 'od';
  if(l.includes('PnL='))                                      return 'ps';
  if(l.includes('score=')&&l.includes('RSI='))                return 'sg';
  return 'in';
}

// ── Render Data ───────────────────────────────────────────────────────────────
function render(d){
  document.getElementById('sdot').className = 'dot'+(d.bot_running?' on':'');
  document.getElementById('stxt').textContent = d.bot_running?'Running':'Stopped';
  document.getElementById('wallet').textContent = d.wallet||'—';
  document.getElementById('cycle').textContent  = d.cycle?`Cycle #${d.cycle}`:'Cycle #—';
  document.getElementById('upd').textContent    = d.last_update||'—';
  document.getElementById('s-bal').textContent  = d.balance!=null?`$${f(d.balance,4)}`:'—';

  const mx=d.max_positions||2, ac=d.active_count||d.positions.length||0;
  document.getElementById('s-pos').textContent    = `${ac} / ${mx}`;
  document.getElementById('pos-badge').textContent= `${ac} / ${mx}`;
  document.getElementById('cfg-maxpos').textContent = mx;
  const se = document.getElementById('s-slots');
  se.innerHTML='';
  for(let i=0;i<mx;i++){const s=document.createElement('div');s.className='sd'+(i<ac?' on':'');se.appendChild(s);}

  const pnl=d.total_pnl||0;
  const pe=document.getElementById('s-pnl');
  pe.textContent=(pnl>=0?'+':'')+'$'+f(Math.abs(pnl),4);
  pe.className='sc-val '+(pnl>0?'pos-g':pnl<0?'pos-r':'pos-n');
  document.getElementById('pnl-card').style.setProperty('--sg',
    `linear-gradient(90deg,${pnl>=0?'var(--green)':'var(--red)'},transparent)`);

  const st=d.sentiment||'neutral';
  const ic=st==='positive'?'&#x1F4C8;':st==='negative'?'&#x1F4C9;':'&#x1F4CA;';
  document.getElementById('s-sent').innerHTML=`<span class="sent-pill ${st}">${ic} ${st.toUpperCase()}</span>`;
  document.getElementById('s-sent-score').textContent=`score: ${d.sentiment_score>=0?'+':''}${f(d.sentiment_score,2)}`;

  updateCtrl(d.bot_running, d.bot_pid);
  renderPos(d.positions||[]);
  renderSigs(d.signals||[],d.top_picks||[]);
  renderOrd(d.recent_orders||[]);
  updateSymSelector(d.positions||[]);
}

function renderPos(list){
  const el=document.getElementById('pos-list');
  if(!list.length){el.innerHTML='<div class="empty"><div class="empty-ic">&#x1F4ED;</div>No active positions</div>';return;}
  el.innerHTML=list.map(p=>{
    const pCls=p.pnl>0?'pos-g':p.pnl<0?'pos-r':'pos-n';
    const bCls=p.pnl>=0?'p':'n';
    const range=Math.abs(p.tp-p.entry)||1;
    const pct=Math.min(Math.abs(p.current-p.entry)/range*100,100);
    const flags=p.flags?`<span class="pc-flag">[${p.flags}]</span>`:'';
    return `<div class="pc ${p.side.toLowerCase()}">
  <div class="pc-hd"><div><span class="pc-sym">${p.symbol}</span>${flags}</div>
    <span class="side-b ${p.side.toLowerCase()}">${p.side}</span></div>
  <div class="pc-prices">
    <div class="pi"><div class="pi-lbl">Entry</div><div class="pi-val">$${f(p.entry,4)}</div></div>
    <div class="pi"><div class="pi-lbl">Current</div><div class="pi-val" style="color:var(--cyan)">$${f(p.current,4)}</div></div>
    <div class="pi"><div class="pi-lbl">PnL</div><div class="pi-val ${pCls}">${p.pnl>=0?'+':''}$${f(p.pnl,4)}</div></div>
  </div>
  <div class="pnl-wrap">
    <div class="pnl-row"><span class="${pCls}" style="font-weight:600">${p.pnl_pct>=0?'+':''}${f(p.pnl_pct,2)}%</span>
      <span style="color:var(--muted);font-size:10px">${p.time?p.time.slice(-8):'—'}</span></div>
    <div class="bar-track"><div class="bar-fill ${bCls}" style="width:${Math.max(2,pct)}%"></div></div>
  </div>
  <div class="pc-lvl">
    <span>SL: <span style="color:var(--red)">$${f(p.sl,4)}</span></span>
    <span>TP: <span style="color:var(--green)">$${f(p.tp,4)}</span></span>
  </div></div>`;}).join('');
}

function renderSigs(sigs,picks){
  const el=document.getElementById('sig-grid');
  document.getElementById('sig-badge').textContent=sigs.length+' coins';
  if(!sigs.length){el.innerHTML='<div style="grid-column:1/-1;color:var(--muted);text-align:center;padding:28px 0;font-size:13px">Waiting for scan...</div>';return;}
  el.innerHTML=sigs.map(s=>{
    const isTop=picks.some(p=>p.includes(s.coin));
    const sc=s.score>=0?'+':'';
    return `<div class="st ${s.color}${isTop?' top':''}" title="RSI ${s.rsi} | ${s.trend}${isTop?' - TOP PICK':''}">
  <div class="st-coin">${s.coin}${isTop?' &#x2B50;':''}</div>
  <div class="st-side ${s.side.toLowerCase()}">${s.side}</div>
  <div class="st-score ${s.color}">${sc}${s.score}</div>
  <div class="st-rsi">RSI&nbsp;${f(s.rsi,0)}</div></div>`;}).join('');
}

function renderOrd(orders){
  const el=document.getElementById('ord-list');
  document.getElementById('ord-badge').textContent=orders.length;
  if(!orders.length){el.innerHTML='<div class="empty" style="padding:18px 0">No signals yet</div>';return;}
  el.innerHTML=[...orders].reverse().map(o=>`
<div class="or">
  <span class="or-time">${o.time}</span>
  <span class="or-sym">${o.symbol}</span>
  <span class="or-side ${o.side.toLowerCase()}">${o.side}</span>
  <span class="or-meta">s${o.score} R${f(o.rsi,0)} ${f(o.lev,0)}x</span>
  <span class="os ${o.status}">${o.status.toUpperCase()}</span>
</div>`).join('');
}

// ── Init ──────────────────────────────────────────────────────────────────────
(async()=>{
  initChart();
  initCandleChart();
  await fetchStatus();
  await fetchLogs();
  await fetchPnl();
  setInterval(fetchStatus,    3000);
  setInterval(fetchLogs,      5000);
  setInterval(fetchPnl,      10000);
  setInterval(fetchCtrlStatus, 5000);
  // Auto-refresh candle chart every 30s
  setInterval(()=>{ if(candleSymbol) loadCandles(candleSymbol); }, 30000);
})();
</script>
</body>
</html>
"""

# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"\n{'='*54}")
    print(f"  [WEB] Nado Bot Dashboard")
    print(f"  URL : http://localhost:{port}")
    print(f"  Log : {LOG}")
    print(f"  Refresh: 3s status / 5s logs / 10s chart")
    print(f"{'='*54}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

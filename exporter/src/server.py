import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

from src.cleaner import _dir_size_mb
from src.config import Config, set_config_value
from src.ingestor import IngestError, ingest
from src.state import IngestState, SenderState
from src.stats import Stats
from src.updater import Updater, get_update_info, start_upgrade

logger = logging.getLogger("langstash.server")


def create_app(config: Config, ingest_state: IngestState, ingest_state_path: Path,
               sender_state: SenderState, sender_state_path: Path, stats: Stats,
               updater: Updater | None = None) -> FastAPI:
    app = FastAPI(title="Langstash", docs_url=None, redoc_url=None)
    data_dir = Path(config.storage.data_dir)

    @app.post("/ingest")
    async def post_ingest(request: Request) -> JSONResponse:
        body_bytes = await request.body()
        if len(body_bytes) > 10 * 1024 * 1024:
            return JSONResponse({"status": "rejected", "error": "payload exceeds 10MB limit"}, status_code=413)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "rejected", "error": "invalid JSON"}, status_code=422)
        try:
            seq_id = ingest(body, ingest_state, data_dir, ingest_state_path)
        except IngestError as e:
            return JSONResponse({"status": "rejected", "error": e.message}, status_code=e.status)
        return JSONResponse({"status": "accepted", "seq_id": seq_id}, status_code=202)

    @app.post("/ingest/batch")
    async def post_ingest_batch(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"status": "rejected", "error": "invalid JSON"}, status_code=422)
        traces = payload.get("traces", [])
        if not isinstance(traces, list) or len(traces) > 100:
            return JSONResponse({"status": "rejected", "error": "traces must be an array with ≤100 items"}, status_code=422)
        ids: list[int] = []
        for t in traces:
            try:
                seq_id = ingest(t, ingest_state, data_dir, ingest_state_path)
                ids.append(seq_id)
            except IngestError as e:
                logger.warning("batch ingest skip: %s", e.message)
        return JSONResponse({"status": "accepted", "count": len(ids), "seq_ids": ids}, status_code=202)

    @app.get("/stats")
    async def get_stats() -> JSONResponse:
        total_traces = ingest_state.next_seq_id - 1
        total_sent = sender_state.commit_id
        pending = total_traces - total_sent
        if pending < 0:
            pending = 0
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_file = data_dir / "pending" / f"{today}.jsonl"
        traces_today = sum(1 for line in open(today_file, encoding="utf-8") if line.strip()) if today_file.exists() else 0
        tokens_today: dict[str, int] = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        if ingest_state.tokens_date == today:
            tokens_today = {
                "input": ingest_state.tokens_input,
                "output": ingest_state.tokens_output,
                "cache_read": ingest_state.tokens_cache_read,
                "cache_creation": ingest_state.tokens_cache_creation,
            }
        storage_mb = _dir_size_mb(data_dir)
        update_info = get_update_info() or {}
        result: dict[str, Any] = {
            "total_traces": total_traces,
            "total_sent": total_sent,
            "pending_count": pending,
            "traces_today": traces_today,
            "tokens_today": tokens_today,
            "last_success_at": sender_state.last_commit_at or None,
            "last_error": None,
            "storage_used_mb": round(storage_mb, 2),
            "uptime_seconds": stats.uptime_seconds,
            "update_available": False,
            "current_version": "",
            "latest_version": "",
        }
        if sender_state.last_error:
            result["last_error"] = {
                "time": sender_state.last_error.time,
                "seq_id": sender_state.last_error.seq_id,
                "error": sender_state.last_error.error,
                "retries": sender_state.last_error.retries,
            }
        result.update(update_info)
        return JSONResponse(result)

    @app.get("/health")
    async def get_health() -> JSONResponse:
        from src.updater import _read_local_version
        healthy = bool(config.langfuse.public_key and config.langfuse.secret_key)
        status_code = 200 if healthy else 503
        return JSONResponse({
            "status": "healthy" if healthy else "no_credentials",
            "version": _read_local_version(),
            "langfuse_configured": healthy,
        }, status_code=status_code)

    @app.post("/upgrade")
    async def post_upgrade() -> JSONResponse:
        info = get_update_info()
        if not info.get("update_available"):
            return JSONResponse({"status": "up_to_date"})
        started = start_upgrade(include_prerelease=config.update.include_prerelease)
        if not started:
            return JSONResponse({"status": "error", "message": "upgrade script not found"}, status_code=500)
        return JSONResponse({"status": "started", "upgrading_to": info.get("latest_version", "")})

    @app.post("/restart")
    async def post_restart() -> JSONResponse:
        def _delayed_exit() -> None:
            import time
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_delayed_exit, daemon=True).start()
        return JSONResponse({"status": "restarting"})

    @app.get("/settings")
    async def get_settings() -> JSONResponse:
        return JSONResponse({"include_prerelease": config.update.include_prerelease})

    @app.post("/settings")
    async def post_settings(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "error": "invalid JSON"}, status_code=422)
        if "include_prerelease" in body:
            val = bool(body["include_prerelease"])
            set_config_value("update", "include_prerelease", val)
            config.update.include_prerelease = val
            if updater:
                updater._include_prerelease = val
        return JSONResponse({"status": "ok"})

    @app.get("/favicon.svg")
    async def get_favicon() -> FileResponse:
        return FileResponse(ASSETS_DIR / "icon.svg", media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    async def get_webui() -> str:
        return _WEBUI_HTML

    return app


_WEBUI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Langstash</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'SF Pro Text','Helvetica Neue',sans-serif;background:#1a1a1a;color:#ddd;min-height:100vh;padding:20px}
.header{display:flex;justify-content:space-between;align-items:center;padding:16px 0;border-bottom:1px solid #333;margin-bottom:20px}
.header h1{font-size:20px;font-weight:600}
.version{font-size:13px;color:#888}
.update-badge{background:#2d6a2d;color:#8f8;padding:2px 8px;border-radius:10px;font-size:11px;margin-left:8px;cursor:pointer}
.update-badge:hover{background:#3a8a3a}
.restart-btn{background:#444;color:#ccc;border:none;padding:3px 10px;border-radius:10px;font-size:11px;cursor:pointer;margin-left:8px}
.restart-btn:hover{background:#555;color:#fff}
.restart-btn.restarting{background:#6a5a2d;color:#ee8;cursor:wait}
.update-badge.upgrading{background:#6a5a2d;color:#ee8;cursor:wait}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}
.card{background:#262626;border-radius:8px;padding:16px;text-align:center}
.card .icon{font-size:20px;margin-bottom:4px}
.card .value{font-size:28px;font-weight:700}
.card .label{font-size:12px;color:#888;margin-top:4px}
.card.warn .value{color:#e8b730}
.card.error .value{color:#ff6b6b}
.section{background:#262626;border-radius:8px;padding:16px;margin-bottom:12px}
.section h3{font-size:14px;color:#888;margin-bottom:12px}
.row{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #333}
.row:last-child{border:none}
.row .k{color:#888}.row .v{color:#ddd}
.bar-bg{background:#333;border-radius:4px;height:8px;margin-top:8px}
.bar-fill{height:8px;border-radius:4px;background:#4a9eff;transition:width 0.3s}
.bar-fill.warn{background:#e8b730}
.bar-fill.crit{background:#ff6b6b}
.tokens{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;text-align:center}
.tokens .tv{font-size:18px;font-weight:600}
.tokens .tl{font-size:11px;color:#888}
.toggle-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0}
.toggle-row .k{color:#888}
.switch{position:relative;width:40px;height:22px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#555;border-radius:22px;transition:.2s}
.slider:before{content:"";position:absolute;height:16px;width:16px;left:3px;bottom:3px;background:#ccc;border-radius:50%;transition:.2s}
.switch input:checked+.slider{background:#4a9eff}
.switch input:checked+.slider:before{transform:translateX(18px);background:#fff}
</style>
</head>
<body>
<div class="header">
  <h1>Langstash</h1>
  <span><span class="version" id="ver"></span><button class="restart-btn" id="restart-btn" onclick="doRestart()">Restart</button></span>
</div>
<div class="cards">
  <div class="card" id="c-traces"><div class="icon">&#9670;</div><div class="value" id="v-traces">-</div><div class="label">total traces</div></div>
  <div class="card" id="c-sent"><div class="icon">&uarr;</div><div class="value" id="v-sent">-</div><div class="label">sent</div></div>
  <div class="card" id="c-pending"><div class="icon">&#9203;</div><div class="value" id="v-pending">-</div><div class="label">pending</div></div>
</div>
<div class="section">
  <h3>Today</h3>
  <div class="tokens" style="grid-template-columns:repeat(5,1fr)">
    <div><div class="tv" id="v-tday">-</div><div class="tl">Traces</div></div>
    <div><div class="tv" id="v-tin">-</div><div class="tl">Input</div></div>
    <div><div class="tv" id="v-tout">-</div><div class="tl">Output</div></div>
    <div><div class="tv" id="v-tcr">-</div><div class="tl">Cache Read</div></div>
    <div><div class="tv" id="v-tcc">-</div><div class="tl">Cache Create</div></div>
  </div>
</div>
<div class="section">
  <h3>Delivery Status</h3>
  <div class="row"><span class="k">Last success</span><span class="v" id="v-ls">-</span></div>
  <div class="row"><span class="k">Last error</span><span class="v" id="v-le">-</span></div>
  <div class="row"><span class="k">Uptime</span><span class="v" id="v-up">-</span></div>
</div>
<div class="section">
  <h3>Storage</h3>
  <div id="v-stor">-</div>
  <div class="bar-bg"><div class="bar-fill" id="bar"></div></div>
</div>
<div class="section">
  <h3>Settings</h3>
  <div class="toggle-row"><span class="k">Pre-release Updates</span><label class="switch"><input type="checkbox" id="chk-pre" onchange="togglePre()"><span class="slider"></span></label></div>
</div>
<script>
const $=id=>document.getElementById(id);
const fmt=n=>{if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return n};
const ago=ts=>{if(!ts)return'-';const d=Date.now()-new Date(ts).getTime();const m=Math.floor(d/60000);if(m<1)return'just now';if(m<60)return m+'m ago';const h=Math.floor(m/60);return h+'h '+m%60+'m ago'};
const up=s=>{const h=Math.floor(s/3600);const m=Math.floor(s%3600/60);return h+'h '+m+'m'};
async function poll(){
  try{
    const r=await fetch('/stats');const d=await r.json();
    $('v-traces').textContent=fmt(d.total_traces);
    $('v-sent').textContent=fmt(d.total_sent);
    $('v-pending').textContent=fmt(d.pending_count);
    $('c-pending').className='card'+(d.pending_count>0?' warn':'');
    $('v-tday').textContent=d.traces_today;
    $('v-tin').textContent=fmt(d.tokens_today?.input||0);
    $('v-tout').textContent=fmt(d.tokens_today?.output||0);
    $('v-tcr').textContent=fmt(d.tokens_today?.cache_read||0);
    $('v-tcc').textContent=fmt(d.tokens_today?.cache_creation||0);
    $('v-ls').textContent=ago(d.last_success_at);
    const le=d.last_error;
    $('v-le').textContent=le?le.error+' (retry '+le.retries+')':'(none)';
    $('v-le').style.color=le?'#ff6b6b':'#888';
    $('v-up').textContent=up(d.uptime_seconds||0);
    const pct=d.storage_used_mb/(20*1024)*100;
    const stor=d.storage_used_mb<1?(d.storage_used_mb*1024).toFixed(1)+' KB':d.storage_used_mb.toFixed(1)+' MB';
    $('v-stor').textContent=stor+' / 20.0 GB ('+pct.toFixed(2)+'%)';
    const bar=$('bar');bar.style.width=Math.min(pct,100)+'%';
    bar.className='bar-fill'+(pct>95?' crit':pct>80?' warn':'');
    let v='v'+d.current_version;
    if(d.update_available&&!window._upgrading)v+=' <span class="update-badge" onclick="doUpgrade()" title="Click to upgrade">Upgrade to v'+d.latest_version+'</span>';
    if(!window._upgrading)$('ver').innerHTML=v;
  }catch(e){$('v-traces').textContent='--'}
  try{
    const s=await fetch('/settings');const sd=await s.json();
    $('chk-pre').checked=sd.include_prerelease;
  }catch(e){}
}
async function togglePre(){
  const v=$('chk-pre').checked;
  try{await fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({include_prerelease:v})})}catch(e){$('chk-pre').checked=!v}
}
async function doUpgrade(){
  if(window._upgrading)return;
  window._upgrading=true;
  $('ver').innerHTML=$('ver').textContent.replace(/Upgrade to.*/,'')+'<span class="update-badge upgrading">Upgrading...</span>';
  try{
    const r=await fetch('/upgrade',{method:'POST'});
    const d=await r.json();
    if(d.status==='up_to_date'){window._upgrading=false;return}
    if(d.status!=='started'){$('ver').innerHTML+=' <span style="color:#f66">'+d.message+'</span>';window._upgrading=false;return}
    waitRestart(d.upgrading_to);
  }catch(e){$('ver').innerHTML+=' <span style="color:#f66">Error</span>';window._upgrading=false}
}
function waitRestart(targetVer){
  let t=0;
  const iv=setInterval(async()=>{
    t+=2;
    if(t>120){clearInterval(iv);$('ver').innerHTML='<span style="color:#f66">Upgrade timeout</span>';window._upgrading=false;return}
    try{
      const r=await fetch('/health');const d=await r.json();
      if(d.version&&d.version!==targetVer&&t<10)return;
      clearInterval(iv);location.reload();
    }catch(e){$('ver').innerHTML='<span class="update-badge upgrading">Restarting...</span>'}
  },2000);
}
async function doRestart(){
  const btn=$('restart-btn');
  if(btn.classList.contains('restarting'))return;
  btn.classList.add('restarting');btn.textContent='Restarting...';
  try{await fetch('/restart',{method:'POST'})}catch(e){}
  setTimeout(()=>{
    const iv=setInterval(async()=>{
      try{const r=await fetch('/health');if(r.ok){clearInterval(iv);location.reload()}}catch(e){}
    },1000);
    setTimeout(()=>{clearInterval(iv);btn.classList.remove('restarting');btn.textContent='Restart'},30000);
  },1000);
}
poll();setInterval(poll,10000);
</script>
</body>
</html>"""

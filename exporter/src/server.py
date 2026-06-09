import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

from src.cleaner import _dir_size_mb
from src.config import Config
from src.ingestor import IngestError, ingest
from src.state import IngestState, SenderState
from src.stats import Stats
from src.updater import get_update_info

logger = logging.getLogger("langstash.server")


def create_app(config: Config, ingest_state: IngestState, ingest_state_path: Path,
               sender_state: SenderState, sender_state_path: Path, stats: Stats) -> FastAPI:
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
        stats.record_ingest(body)
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
                stats.record_ingest(t)
            except IngestError as e:
                logger.warning("batch ingest skip: %s", e.message)
        return JSONResponse({"status": "accepted", "count": len(ids), "seq_ids": ids}, status_code=202)

    @app.get("/stats")
    async def get_stats() -> JSONResponse:
        pending = ingest_state.next_seq_id - sender_state.commit_id - 1
        if pending < 0:
            pending = 0
        storage_mb = _dir_size_mb(data_dir)
        update_info = get_update_info()
        result = stats.to_dict(
            pending_count=pending,
            storage_used_mb=storage_mb,
            last_error=sender_state.last_error,
            last_commit_at=sender_state.last_commit_at,
            update_info=update_info,
        )
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
.update-badge{background:#2d6a2d;color:#8f8;padding:2px 8px;border-radius:10px;font-size:11px;margin-left:8px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
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
</style>
</head>
<body>
<div class="header">
  <h1>Langstash</h1>
  <span class="version" id="ver"></span>
</div>
<div class="cards">
  <div class="card" id="c-traces"><div class="icon">&#9670;</div><div class="value" id="v-traces">-</div><div class="label">traces today</div></div>
  <div class="card" id="c-sent"><div class="icon">&uarr;</div><div class="value" id="v-sent">-</div><div class="label">sent</div></div>
  <div class="card" id="c-pending"><div class="icon">&#9203;</div><div class="value" id="v-pending">-</div><div class="label">pending</div></div>
  <div class="card" id="c-failed"><div class="icon">&#9888;</div><div class="value" id="v-failed">-</div><div class="label">failed</div></div>
</div>
<div class="section">
  <h3>Tokens Today</h3>
  <div class="tokens">
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
<script>
const $=id=>document.getElementById(id);
const fmt=n=>{if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return n};
const ago=ts=>{if(!ts)return'-';const d=Date.now()-new Date(ts).getTime();const m=Math.floor(d/60000);if(m<1)return'just now';if(m<60)return m+'m ago';const h=Math.floor(m/60);return h+'h '+m%60+'m ago'};
const up=s=>{const h=Math.floor(s/3600);const m=Math.floor(s%3600/60);return h+'h '+m+'m'};
async function poll(){
  try{
    const r=await fetch('/stats');const d=await r.json();
    $('v-traces').textContent=d.traces_today;
    $('v-sent').textContent=d.sent_today;
    $('v-pending').textContent=d.pending_count;
    $('c-pending').className='card'+(d.pending_count>0?' warn':'');
    $('v-failed').textContent=d.failed_count;
    $('c-failed').className='card'+(d.failed_count>0?' error':'');
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
    if(d.update_available)v+=' <span class="update-badge">Update: v'+d.latest_version+'</span>';
    $('ver').innerHTML=v;
  }catch(e){$('v-traces').textContent='--'}
}
poll();setInterval(poll,10000);
</script>
</body>
</html>"""

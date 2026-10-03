"""Release Events v1 HTTP inbox for Sovs Release Bot.

Runs beside the existing polling daemon. Events are persisted before delivery,
so producer retries and cross-source duplicates are idempotent.
"""
from __future__ import annotations
import hashlib, hmac, json, os, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN=os.environ.get("RELEASE_EVENTS_TOKEN","")
STATE=Path(os.environ.get("RELEASE_EVENTS_STATE","/data/release-events-seen.json"))
MAX_SEEN=5000

def _load():
    try:
        data=json.loads(STATE.read_text())
        return data if isinstance(data,list) else []
    except Exception:
        return []

def _save(items):
    STATE.parent.mkdir(parents=True,exist_ok=True)
    tmp=STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[-MAX_SEEN:])+"\n")
    tmp.replace(STATE)

def canonical_id(e):
    if e.get("id"): return str(e["id"]).strip()
    raw="|".join(str(e.get(k,"")).strip().lower() for k in ("kind","name","version","url"))
    return hashlib.sha256(raw.encode()).hexdigest()

def validate(e):
    if e.get("schema")!="release-event/v1": return "unsupported schema"
    for k in ("kind","name","version","source","url"):
        if not str(e.get(k,"")).strip(): return f"missing {k}"
    if e["kind"] not in ("software","model"): return "invalid kind"
    if not str(e["url"]).startswith(("https://","http://")): return "invalid url"
    return ""

def format_event(e):
    icon="🤖" if e["kind"]=="model" else "🚀"
    name=bot._esc(str(e["name"])); version=bot._esc(str(e["version"]))
    summary=bot._esc(str(e.get("summary") or "").strip()[:1600])
    url=bot._esc(str(e["url"]))
    msg=f"{icon} <b>{name} {version}</b>"
    if summary: msg+=f"\n\n{summary}"
    msg+=f'\n\n🔗 <a href="{url}">Release</a>'
    return msg

def accept_event(e):
    err=validate(e)
    if err: return 400,err
    eid=canonical_id(e)
    seen=_load()
    if eid in seen: return 200,"duplicate"
    if not bot.send_telegram(format_event(e)): return 503,"delivery failed"
    seen.append(eid); _save(seen)
    return 202,"accepted"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path=="/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        self.send_response(404); self.end_headers()
    def do_POST(self):
        if self.path!="/v1/releases": self.send_response(404); self.end_headers(); return
        auth=self.headers.get("Authorization","")
        expected=f"Bearer {TOKEN}"
        if not TOKEN or not hmac.compare_digest(auth,expected):
            self.send_response(401); self.end_headers(); return
        try:
            n=int(self.headers.get("Content-Length","0"))
            if n<=0 or n>65536: raise ValueError("bad length")
            e=json.loads(self.rfile.read(n))
            if not isinstance(e,dict): raise ValueError("bad body")
        except Exception:
            self.send_response(400); self.end_headers(); return
        code,msg=accept_event(e)
        self.send_response(code); self.end_headers(); self.wfile.write(msg.encode())
    def log_message(self,fmt,*args):
        print("[EVENTS] "+fmt%args)

def serve():
    port=int(os.environ.get("PORT","8080"))
    print(f"[START] release event inbox on :{port}")
    ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()

if __name__=="__main__":
    import bot
    serve()

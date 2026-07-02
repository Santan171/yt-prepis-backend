from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, re, uuid
from supabase import create_client, Client

app = FastAPI(title="YT Prepis API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def get_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ")[1]
    try:
        resp = supabase.auth.get_user(token)
        if not resp or not resp.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return resp.user
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

def extract_video_id(url: str):
    url = url.strip()
    for p in [r"(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})", r"^([a-zA-Z0-9_-]{11})$"]:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

class YoutubeRequest(BaseModel):
    url: str
    lang: str = "sk"
    timestamps: bool = False
    deduplicate: bool = True

@app.post("/api/youtube")
def transcribe_youtube(req: YoutubeRequest, user=Depends(get_user)):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import WebshareProxyConfig

    vid = extract_video_id(req.url)
    if not vid:
        raise HTTPException(status_code=400, detail="Neplatne YouTube URL")
    try:
        # Try without proxy first, then handle errors
        try:
            tlist = YouTubeTranscriptApi.list_transcripts(vid)
        except Exception as e:
            err = str(e)
            if "no element found" in err or "Too Many Requests" in err or "429" in err:
                raise HTTPException(status_code=503, detail="YouTube dočasne blokuje server. Skús znova o pár minút.")
            raise

        try:
            t = tlist.find_transcript([req.lang])
        except Exception:
            try:
                t = tlist.find_generated_transcript([req.lang])
            except Exception:
                t = next(iter(tlist))

        used_lang = t.language_code
        raw = t.fetch()
        data = []
        for e in raw:
            text = e.text if hasattr(e, "text") else e.get("text", "")
            start = e.start if hasattr(e, "start") else e.get("start", 0)
            data.append({"text": text, "start": start})

        lines, prev = [], ""
        for e in data:
            txt = e["text"].replace("\n", " ").strip()
            st = e["start"]
            if not txt or (req.deduplicate and txt == prev):
                continue
            prev = txt
            if req.timestamps:
                h, m, s = int(st//3600), int((st%3600)//60), int(st%60)
                ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                lines.append(f"[{ts}] {txt}")
            else:
                lines.append(txt)

        content = "\n".join(lines)
        supabase.table("transcripts").insert({
            "id": str(uuid.uuid4()),
            "user_id": user.id,
            "title": f"YouTube - {vid}",
            "content": content,
            "source": req.url,
            "language": used_lang,
        }).execute()
        return {"content": content, "language": used_lang, "segments": len(lines)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history")
def get_history(user=Depends(get_user)):
    return supabase.table("transcripts").select("id,title,source,language,created_at").eq("user_id", user.id).order("created_at", desc=True).execute().data

@app.get("/api/history/{tid}")
def get_transcript(tid: str, user=Depends(get_user)):
    res = supabase.table("transcripts").select("*").eq("id", tid).eq("user_id", user.id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Nenajdene")
    return res.data

@app.delete("/api/history/{tid}")
def delete_transcript(tid: str, user=Depends(get_user)):
    supabase.table("transcripts").delete().eq("id", tid).eq("user_id", user.id).execute()
    return {"ok": True}

@app.get("/health")
def health():
    return {"status": "ok"}

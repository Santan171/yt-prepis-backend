from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, re, uuid, json, tempfile
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
    import yt_dlp

    vid = extract_video_id(req.url)
    if not vid:
        raise HTTPException(status_code=400, detail="Neplatne YouTube URL")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            sub_file = os.path.join(tmpdir, "sub")
            lang = req.lang if req.lang != "auto" else None

            ydl_opts = {
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitlesformat": "json3",
                "outtmpl": sub_file,
                "quiet": True,
                "no_warnings": True,
            }
            if lang:
                ydl_opts["subtitleslangs"] = [lang, lang + "-SK", lang + "-CZ"]
            else:
                ydl_opts["subtitleslangs"] = ["sk", "cs", "en", "all"]

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)

            # Find downloaded subtitle file
            used_lang = lang or "sk"
            content = ""
            for fname in os.listdir(tmpdir):
                if fname.endswith(".json3"):
                    used_lang = fname.split(".")[-2]
                    with open(os.path.join(tmpdir, fname)) as f:
                        data = json.load(f)
                    events = data.get("events", [])
                    lines, prev = [], ""
                    for event in events:
                        segs = event.get("segs", [])
                        txt = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
                        if not txt or txt == "\n":
                            continue
                        if req.deduplicate and txt == prev:
                            continue
                        prev = txt
                        if req.timestamps:
                            ms = event.get("tStartMs", 0)
                            s_total = int(ms / 1000)
                            h, m, s = s_total // 3600, (s_total % 3600) // 60, s_total % 60
                            ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                            lines.append(f"[{ts}] {txt}")
                        else:
                            lines.append(txt)
                    content = "\n".join(lines)
                    break

            if not content:
                raise HTTPException(status_code=404, detail="Pre toto video nie sú dostupné titulky.")

            supabase.table("transcripts").insert({
                "id": str(uuid.uuid4()),
                "user_id": user.id,
                "title": info.get("title", f"YouTube - {vid}"),
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

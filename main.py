from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, re, tempfile, uuid
from supabase import create_client, Client

app = FastAPI(title="YT Prepis API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split(" ")[1]
    try:
        user = supabase.auth.get_user(token)
        return user.user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def extract_video_id(url: str):
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$"
    ]
    for p in patterns:
        m = re.search(p, url.strip())
        if m:
            return m.group(1)
    return None


def save_transcript(user_id: str, title: str, content: str, source: str, lang: str):
    supabase.table("transcripts").insert({
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "title": title,
        "content": content,
        "source": source,
        "language": lang,
    }).execute()


# ── YouTube ──────────────────────────────────────────────
class YoutubeRequest(BaseModel):
    url: str
    lang: str = "sk"
    timestamps: bool = False
    deduplicate: bool = True


@app.post("/api/youtube")
def transcribe_youtube(req: YoutubeRequest, user=Depends(get_user)):
    from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound

    vid = extract_video_id(req.url)
    if not vid:
        raise HTTPException(status_code=400, detail="Neplatné YouTube URL")

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(vid)

        if req.lang == "auto":
            transcript = next(iter(transcript_list))
        else:
            try:
                transcript = transcript_list.find_transcript([req.lang])
            except NoTranscriptFound:
                transcript = next(iter(transcript_list))

        used_lang = transcript.language_code
        data = transcript.fetch()

        lines = []
        prev = ""
        for entry in data:
            text = (entry.text if hasattr(entry, "text") else entry.get("text", "")).replace("\n", " ").strip()
            start = entry.start if hasattr(entry, "start") else entry.get("start", 0)
            if not text or (req.deduplicate and text == prev):
                continue
            prev = text
            if req.timestamps:
                h, m, s = int(start // 3600), int((start % 3600) // 60), int(start % 60)
                ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                lines.append(f"[{ts}] {text}")
            else:
                lines.append(text)

        content = "\n".join(lines)
        save_transcript(user.id, f"YouTube – {vid}", content, req.url, used_lang)
        return {"content": content, "language": used_lang, "segments": len(lines)}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Audio/Video upload ────────────────────────────────────
@app.post("/api/upload")
async def transcribe_upload(file: UploadFile = File(...), user=Depends(get_user)):
    import whisper

    allowed = {"audio/mpeg", "audio/mp4", "audio/wav", "video/mp4", "audio/ogg", "audio/webm"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Nepodporovaný formát súboru")

    suffix = "." + file.filename.split(".")[-1]
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        model = whisper.load_model("large")
        result = model.transcribe(tmp_path, verbose=False)
        content = result["text"].strip()
        lang = result.get("language", "unknown")
        save_transcript(user.id, file.filename, content, "upload", lang)
        return {"content": content, "language": lang, "segments": len(result.get("segments", []))}
    finally:
        os.unlink(tmp_path)


# ── História ──────────────────────────────────────────────
@app.get("/api/history")
def get_history(user=Depends(get_user)):
    res = supabase.table("transcripts") \
        .select("id, title, source, language, created_at") \
        .eq("user_id", user.id) \
        .order("created_at", desc=True) \
        .execute()
    return res.data


@app.get("/api/history/{transcript_id}")
def get_transcript(transcript_id: str, user=Depends(get_user)):
    res = supabase.table("transcripts") \
        .select("*") \
        .eq("id", transcript_id) \
        .eq("user_id", user.id) \
        .single() \
        .execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Nenájdené")
    return res.data


@app.delete("/api/history/{transcript_id}")
def delete_transcript(transcript_id: str, user=Depends(get_user)):
    supabase.table("transcripts") \
        .delete() \
        .eq("id", transcript_id) \
        .eq("user_id", user.id) \
        .execute()
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}

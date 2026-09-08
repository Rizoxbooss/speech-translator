import os
import re
import time
import json
import uuid
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
import requests
from flask import Flask, render_template, request, jsonify, send_file, after_this_request
from youtube_transcript_api import YouTubeTranscriptApi
try:
    from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
except ImportError:
    from youtube_transcript_api.errors import TranscriptsDisabled, NoTranscriptFound, VideoUnavailable
try:
    import yt_dlp
except ImportError:
    yt_dlp=None

MONGODB_URI=os.environ.get("MONGODB_URI","").strip()
_mongo_col=None
if MONGODB_URI:
    try:
        from pymongo import MongoClient
        _mongo_col=MongoClient(MONGODB_URI,serverSelectionTimeoutMS=5000)["speech_translator"]["history"]
        _mongo_col.database.client.server_info()
    except Exception as e:
        print(f"[history] MongoDB unavailable, falling back to local file: {e}")
        _mongo_col=None

BASE=Path(__file__).resolve().parent
DATA_DIR=Path.home()/".error_gang_speech_translator"
DATA_DIR.mkdir(parents=True,exist_ok=True)
ENV=DATA_DIR/".env"
_LEGACY_ENV=BASE/".env"
HISTORY_FILE=DATA_DIR/"history.json"
HISTORY_LIMIT=300

def load_history():
    if _mongo_col is not None:
        try:
            docs=list(_mongo_col.find({},{"_id":0}).sort("_order",-1).limit(HISTORY_LIMIT))
            return docs
        except Exception as e:
            print(f"[history] MongoDB read failed, using local file: {e}")
    if not HISTORY_FILE.exists():return []
    try:
        data=json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data,list) else []
    except (json.JSONDecodeError,OSError):
        return []

def save_history(entries):
    if _mongo_col is not None:
        try:
            _mongo_col.delete_many({})
            if entries:
                docs=[{**e,"_order":len(entries)-i} for i,e in enumerate(entries[:HISTORY_LIMIT])]
                _mongo_col.insert_many(docs)
            return
        except Exception as e:
            print(f"[history] MongoDB write failed, using local file: {e}")
    HISTORY_FILE.write_text(json.dumps(entries[:HISTORY_LIMIT],ensure_ascii=False,indent=1),encoding="utf-8")

def add_history(entry_type,title,output,language="",thumbnail="",video_id="",source_url=""):
    entries=load_history()
    entries.insert(0,{
        "id":uuid.uuid4().hex,
        "type":entry_type,
        "title":(title or "Untitled").strip(),
        "language":language,
        "output":output,
        "thumbnail":thumbnail,
        "video_id":video_id,
        "source_url":source_url,
        "timestamp":datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_history(entries)

def load_env():
    if not ENV.exists() and _LEGACY_ENV.exists():
        # migrate a key saved by an older version of this app (stored next to app.py)
        # into the persistent, update-proof location so it survives future app updates
        try: ENV.write_text(_LEGACY_ENV.read_text(encoding="utf-8"),encoding="utf-8")
        except OSError: pass
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k,v=line.split("=",1); os.environ.setdefault(k.strip(),v.strip())
def save_key(key):
    lines=ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    out=[]; found=False
    for line in lines:
        if line.startswith("GEMINI_API_KEY="):
            out.append("GEMINI_API_KEY="+key); found=True
        elif line.strip(): out.append(line)
    if not found: out.append("GEMINI_API_KEY="+key)
    if not any(x.startswith("GEMINI_MODEL=") for x in out): out.append("GEMINI_MODEL=gemini-3.5-flash-lite")
    ENV.write_text("\n".join(out)+"\n",encoding="utf-8")
    os.environ["GEMINI_API_KEY"]=key

load_env()
app=Flask(__name__)
LANGS=[
    {"flag":"🇬🇧","cc":"gb","native":"English","en":"English"},
    {"flag":"🇪🇸","cc":"es","native":"Español","en":"Spanish"},
    {"flag":"🇫🇷","cc":"fr","native":"Français","en":"French"},
    {"flag":"🇩🇪","cc":"de","native":"Deutsch","en":"German"},
    {"flag":"🇮🇹","cc":"it","native":"Italiano","en":"Italian"},
    {"flag":"🇵🇹","cc":"pt","native":"Português","en":"Portuguese"},
    {"flag":"🇳🇱","cc":"nl","native":"Nederlands","en":"Dutch"},
    {"flag":"🇸🇦","cc":"sa","native":"العربية","en":"Arabic"},
    {"flag":"🇵🇰","cc":"pk","native":"اردو","en":"Urdu"},
    {"flag":"🇮🇳","cc":"in","native":"हिन्दी","en":"Hindi"},
    {"flag":"🇧🇩","cc":"bd","native":"বাংলা","en":"Bengali"},
    {"flag":"🇹🇷","cc":"tr","native":"Türkçe","en":"Turkish"},
    {"flag":"🇷🇺","cc":"ru","native":"Русский","en":"Russian"},
    {"flag":"🇺🇦","cc":"ua","native":"Українська","en":"Ukrainian"},
    {"flag":"🇨🇳","cc":"cn","native":"简体中文","en":"Chinese (Simplified)"},
    {"flag":"🇹🇼","cc":"tw","native":"繁體中文","en":"Chinese (Traditional)"},
    {"flag":"🇯🇵","cc":"jp","native":"日本語","en":"Japanese"},
    {"flag":"🇰🇷","cc":"kr","native":"한국어","en":"Korean"},
    {"flag":"🇮🇩","cc":"id","native":"Bahasa Indonesia","en":"Indonesian"},
    {"flag":"🇲🇾","cc":"my","native":"Bahasa Melayu","en":"Malay"},
    {"flag":"🇵🇭","cc":"ph","native":"Filipino","en":"Filipino (Tagalog)"},
    {"flag":"🇵🇱","cc":"pl","native":"Polski","en":"Polish"},
    {"flag":"🇷🇴","cc":"ro","native":"Română","en":"Romanian"},
    {"flag":"🇬🇷","cc":"gr","native":"Ελληνικά","en":"Greek"},
    {"flag":"🇮🇱","cc":"il","native":"עברית","en":"Hebrew"},
    {"flag":"🇮🇷","cc":"ir","native":"فارسی","en":"Persian (Farsi)"},
    {"flag":"🇦🇫","cc":"af","native":"پښتو","en":"Pashto"},
    {"flag":"🇹🇭","cc":"th","native":"ไทย","en":"Thai"},
    {"flag":"🇻🇳","cc":"vn","native":"Tiếng Việt","en":"Vietnamese"},
    {"flag":"🇲🇲","cc":"mm","native":"မြန်မာ","en":"Burmese"},
    {"flag":"🇰🇭","cc":"kh","native":"ខ្មែរ","en":"Khmer"},
    {"flag":"🇱🇦","cc":"la","native":"ລາວ","en":"Lao"},
    {"flag":"🇳🇵","cc":"np","native":"नेपाली","en":"Nepali"},
    {"flag":"🇱🇰","cc":"lk","native":"සිංහල","en":"Sinhala"},
    {"flag":"🇮🇳","cc":"in","native":"தமிழ்","en":"Tamil"},
    {"flag":"🇮🇳","cc":"in","native":"తెలుగు","en":"Telugu"},
    {"flag":"🇮🇳","cc":"in","native":"मराठी","en":"Marathi"},
    {"flag":"🇮🇳","cc":"in","native":"ગુજરાતી","en":"Gujarati"},
    {"flag":"🇮🇳","cc":"in","native":"ਪੰਜਾਬੀ","en":"Punjabi"},
    {"flag":"🇮🇳","cc":"in","native":"ಕನ್ನಡ","en":"Kannada"},
    {"flag":"🇮🇳","cc":"in","native":"മലയാളം","en":"Malayalam"},
    {"flag":"🇸🇪","cc":"se","native":"Svenska","en":"Swedish"},
    {"flag":"🇳🇴","cc":"no","native":"Norsk","en":"Norwegian"},
    {"flag":"🇩🇰","cc":"dk","native":"Dansk","en":"Danish"},
    {"flag":"🇫🇮","cc":"fi","native":"Suomi","en":"Finnish"},
    {"flag":"🇨🇿","cc":"cz","native":"Čeština","en":"Czech"},
    {"flag":"🇸🇰","cc":"sk","native":"Slovenčina","en":"Slovak"},
    {"flag":"🇭🇺","cc":"hu","native":"Magyar","en":"Hungarian"},
    {"flag":"🇧🇬","cc":"bg","native":"Български","en":"Bulgarian"},
    {"flag":"🇭🇷","cc":"hr","native":"Hrvatski","en":"Croatian"},
    {"flag":"🇷🇸","cc":"rs","native":"Српски","en":"Serbian"},
    {"flag":"🇸🇮","cc":"si","native":"Slovenščina","en":"Slovenian"},
    {"flag":"🇱🇹","cc":"lt","native":"Lietuvių","en":"Lithuanian"},
    {"flag":"🇱🇻","cc":"lv","native":"Latviešu","en":"Latvian"},
    {"flag":"🇪🇪","cc":"ee","native":"Eesti","en":"Estonian"},
    {"flag":"🇦🇱","cc":"al","native":"Shqip","en":"Albanian"},
    {"flag":"🇬🇪","cc":"ge","native":"ქართული","en":"Georgian"},
    {"flag":"🇦🇲","cc":"am","native":"Հայերեն","en":"Armenian"},
    {"flag":"🇦🇿","cc":"az","native":"Azərbaycan","en":"Azerbaijani"},
    {"flag":"🇰🇿","cc":"kz","native":"Қазақ","en":"Kazakh"},
    {"flag":"🇺🇿","cc":"uz","native":"Oʻzbek","en":"Uzbek"},
    {"flag":"🇲🇳","cc":"mn","native":"Монгол","en":"Mongolian"},
    {"flag":"🇰🇪","cc":"ke","native":"Kiswahili","en":"Swahili"},
    {"flag":"🇪🇹","cc":"et","native":"አማርኛ","en":"Amharic"},
    {"flag":"🇳🇬","cc":"ng","native":"Hausa","en":"Hausa"},
    {"flag":"🇳🇬","cc":"ng","native":"Yorùbá","en":"Yoruba"},
    {"flag":"🇳🇬","cc":"ng","native":"Igbo","en":"Igbo"},
    {"flag":"🇿🇦","cc":"za","native":"isiZulu","en":"Zulu"},
]

@app.get("/")
def home(): return render_template("index.html",languages=LANGS)

@app.get("/api/settings")
def settings():
    k=os.getenv("GEMINI_API_KEY","").strip()
    return jsonify(configured=bool(k),masked=("••••••••"+k[-4:] if len(k)>=4 else ""))

@app.post("/api/settings")
def settings_save():
    k=(request.get_json(silent=True) or {}).get("gemini_api_key","").strip()
    if not k:return jsonify(error="Please enter a Gemini API key."),400
    try: save_key(k); return jsonify(ok=True,masked="••••••••"+k[-4:])
    except Exception as e:return jsonify(error=str(e)),500

@app.get("/api/history")
def history_get():
    return jsonify(entries=[e for e in load_history() if e.get("type")=="Beat Competitor"])

@app.post("/api/history")
def history_add():
    d=request.get_json(silent=True) or {}
    entry_type=d.get("type","").strip()
    title=d.get("title","").strip()
    output=d.get("output","").strip()
    language=d.get("language","").strip()
    thumbnail=d.get("thumbnail","").strip()
    video_id=d.get("video_id","").strip()
    source_url=d.get("source_url","").strip()
    if entry_type!="Beat Competitor":return jsonify(error="Invalid history type."),400
    if not output:return jsonify(error="Nothing to save."),400
    add_history(entry_type,title,output,language,thumbnail,video_id,source_url)
    return jsonify(ok=True)

@app.delete("/api/history")
def history_clear():
    save_history([])
    return jsonify(ok=True)

def extract_video_id(url):
    url=url.strip()
    patterns=[
        r"(?:youtube\.com/watch\?v=|youtube\.com/shorts/|youtube\.com/embed/|youtube\.com/live/|youtu\.be/)([0-9A-Za-z_-]{11})",
    ]
    for p in patterns:
        m=re.search(p,url)
        if m: return m.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{11}",url): return url
    return None

def _get_transcript_list(video_id):
    """Support both the old (<1.0) classmethod API and the new (>=1.0) instance API."""
    if hasattr(YouTubeTranscriptApi,"list_transcripts"):
        return YouTubeTranscriptApi.list_transcripts(video_id)
    return YouTubeTranscriptApi().list(video_id)

# Non-speech captions YouTube auto-generates, e.g. [Music], (Applause), 【笑い】
_NONSPEECH_WORDS=r"(?:music|musique|música|musik|musica|applause|aplausos|laughter|laughing|laughs?|laugh|silence|silêncio|silencio|cheering|cheers|clapping|coughing|inaudible|background noise|noise|singing|instrumental|audience|crowd noise|indistinct chatter|indistinct|foreign language|no audio|blank_audio|static)"
_BRACKET_TAG_RE=re.compile(r"[\[\(【][^\]\)】]{0,40}\b"+_NONSPEECH_WORDS+r"\b[^\]\)】]{0,10}[\]\)】]",re.IGNORECASE)
# Fallback: strip ANY short bracketed/parenthesized tag (covers un-listed sound-effect labels
# like [Beat drops], (dramatic music), etc.) as long as it doesn't look like real dialogue.
_GENERIC_BRACKET_RE=re.compile(r"[\[【][^\]】]{1,40}[\]】]")

def _clean_caption_text(t):
    t=_BRACKET_TAG_RE.sub("",t)
    t=_GENERIC_BRACKET_RE.sub("",t)
    return re.sub(r"\s{2,}"," ",t).strip()

def _segments_to_text(data):
    segs=data.to_raw_data() if hasattr(data,"to_raw_data") else data
    parts=[]
    for seg in segs:
        if isinstance(seg,dict):
            t=seg.get("text","")
        else:
            t=getattr(seg,"text","")
        t=_clean_caption_text((t or "").strip())
        if t: parts.append(t)
    return " ".join(parts)

def fetch_youtube_transcript_text(video_id):
    tlist=_get_transcript_list(video_id)
    transcript=None
    try:
        transcript=tlist.find_transcript(["en","en-US","en-GB"])
    except Exception:
        for t in tlist:
            transcript=t
            break
    if transcript is None:
        raise RuntimeError("No transcript track found for this video.")
    data=transcript.fetch()
    return _segments_to_text(data)

def fetch_youtube_title(video_id):
    try:
        r=requests.get("https://www.youtube.com/oembed",params={"url":f"https://www.youtube.com/watch?v={video_id}","format":"json"},timeout=8)
        if r.status_code==200:
            return (r.json().get("title") or "").strip()
    except Exception:
        pass
    return ""

def fetch_youtube_thumbnail(video_id):
    """Return the best-quality thumbnail URL that actually exists for this video.
    YouTube always returns HTTP 200 for these paths (even missing ones serve a tiny
    120x90 placeholder), so we check the payload size to tell a real thumbnail apart
    from the placeholder."""
    for q_ in ("maxresdefault","sddefault","hqdefault","mqdefault","default"):
        url=f"https://img.youtube.com/vi/{video_id}/{q_}.jpg"
        try:
            r=requests.get(url,timeout=6)
            if r.status_code==200 and len(r.content)>2000:
                return url
        except Exception:
            continue
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

@app.post("/api/youtube_transcript")
def youtube_transcript():
    d=request.get_json(silent=True) or {}
    url=d.get("url","").strip()
    if not url:return jsonify(error="Please paste a YouTube video link."),400
    vid=extract_video_id(url)
    if not vid:return jsonify(error="Couldn't read a valid YouTube link."),400
    try:
        text=fetch_youtube_transcript_text(vid)
    except (TranscriptsDisabled,NoTranscriptFound):
        return jsonify(error="This video has no captions/transcript available."),400
    except VideoUnavailable:
        return jsonify(error="That video is unavailable."),400
    except Exception as e:
        return jsonify(error=f"Could not fetch transcript: {e}"),502
    if not text:return jsonify(error="This video has no captions/transcript available."),400
    title=fetch_youtube_title(vid)
    thumbnail=fetch_youtube_thumbnail(vid)
    return jsonify(transcript=text,title=title,thumbnail=thumbnail,video_id=vid)

@app.get("/api/thumbnail_download")
def thumbnail_download():
    from flask import Response
    vid=(request.args.get("video_id") or "").strip()
    if not vid or not re.fullmatch(r"[0-9A-Za-z_-]{11}",vid):
        return jsonify(error="Missing or invalid video_id."),400
    url=fetch_youtube_thumbnail(vid)
    try:
        r=requests.get(url,timeout=15)
        r.raise_for_status()
    except Exception as e:
        return jsonify(error=f"Could not download thumbnail: {e}"),502
    return Response(r.content,mimetype="image/jpeg",headers={"Content-Disposition":f'attachment; filename="{vid}_thumbnail.jpg"'})

def gemini_extract_image_text(image_bytes,key,configured_model):
    """Ask Gemini to read out any text that's visually printed/overlaid on an image (e.g. a YouTube thumbnail).
    Not every configured text model accepts image input (e.g. some '-lite' variants are text-only), so this
    tries the user's configured model first, then falls back through other known vision-capable models."""
    import base64
    b64=base64.b64encode(image_bytes).decode("utf-8")
    prompt=("Look carefully at this image, which is a YouTube video thumbnail. Extract ONLY the text that is "
            "visually printed or overlaid on top of the thumbnail itself (bold captions, titles, numbers, callouts, etc.) "
            "— do not describe the image or guess at anything that isn't actual on-image text. Preserve line breaks as shown. "
            "If there is no visible text anywhere on the thumbnail, respond with exactly: NONE")
    candidates=[configured_model]
    for fb in ("gemini-2.5-flash","gemini-2.0-flash","gemini-1.5-flash","gemini-1.5-pro","gemini-flash-latest"):
        if fb not in candidates:candidates.append(fb)
    last_err=None
    for model in candidates:
        try:
            r=requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",params={"key":key},
                             json={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"image/jpeg","data":b64}}]}]},timeout=60)
            if r.status_code==200:
                parts=r.json().get("candidates",[{}])[0].get("content",{}).get("parts",[])
                result="".join(p.get("text","") for p in parts).strip()
                if result:return result,None
                last_err="Empty response from model.";continue
            try:e=r.json().get("error",{}).get("message",r.text)
            except:e=r.text
            last_err=e;continue
        except requests.RequestException as e:
            last_err=str(e);continue
    return None,f"Thumbnail text API error: {last_err}"

@app.get("/api/thumbnail_text")
def thumbnail_text():
    vid=(request.args.get("video_id") or "").strip()
    if not vid or not re.fullmatch(r"[0-9A-Za-z_-]{11}",vid):
        return jsonify(error="Missing or invalid video_id."),400
    load_env(); key=os.getenv("GEMINI_API_KEY","").strip(); model=os.getenv("GEMINI_MODEL","gemini-3.5-flash-lite")
    if not key:return jsonify(error="Open Settings and add your Gemini API key first."),400
    url=fetch_youtube_thumbnail(vid)
    try:
        r=requests.get(url,timeout=15); r.raise_for_status()
    except Exception as e:
        return jsonify(error=f"Could not fetch thumbnail: {e}"),502
    text,err=gemini_extract_image_text(r.content,key,model)
    if err:return jsonify(error=err),502
    if text.strip().upper()=="NONE":
        return jsonify(text="",found=False)
    return jsonify(text=text,found=True)

@app.post("/api/audio_info")
def audio_info():
    if yt_dlp is None:return jsonify(error="Audio support is not installed. Reinstall the app to get this update."),500
    d=request.get_json(silent=True) or {}
    vid=(d.get("video_id") or "").strip()
    if not vid or not re.fullmatch(r"[0-9A-Za-z_-]{11}",vid):
        return jsonify(error="Missing or invalid video_id."),400
    try:
        with yt_dlp.YoutubeDL({"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True}) as ydl:
            info=ydl.extract_info(f"https://www.youtube.com/watch?v={vid}",download=False)
        duration=int(info.get("duration") or 0)
        mm,ss=divmod(duration,60)
        hh,mm=divmod(mm,60)
        duration_text=f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm}:{ss:02d}"
        return jsonify(duration_seconds=duration,duration_text=duration_text)
    except Exception as e:
        return jsonify(error=f"Could not read audio info: {e}"),502

@app.get("/api/audio_download")
def audio_download():
    if yt_dlp is None:return jsonify(error="Audio support is not installed. Reinstall the app to get this update."),500
    vid=(request.args.get("video_id") or "").strip()
    if not vid or not re.fullmatch(r"[0-9A-Za-z_-]{11}",vid):
        return jsonify(error="Missing or invalid video_id."),400
    tmp_dir=tempfile.mkdtemp(prefix="comp_audio_")
    try:
        ydl_opts={"quiet":True,"no_warnings":True,"noplaylist":True,"format":"bestaudio/best",
                  "outtmpl":os.path.join(tmp_dir,"%(id)s.%(ext)s")}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info=ydl.extract_info(f"https://www.youtube.com/watch?v={vid}",download=True)
            filename=ydl.prepare_filename(info)
        if not os.path.exists(filename):
            shutil.rmtree(tmp_dir,ignore_errors=True)
            return jsonify(error="Could not download audio for this video."),502
        title=re.sub(r'[\\/:*?"<>|]+',"_",(info.get("title") or vid)).strip()[:80] or vid
        ext=os.path.splitext(filename)[1]
        @after_this_request
        def _cleanup(response):
            shutil.rmtree(tmp_dir,ignore_errors=True)
            return response
        return send_file(filename,as_attachment=True,download_name=f"{title}{ext}")
    except Exception as e:
        shutil.rmtree(tmp_dir,ignore_errors=True)
        return jsonify(error=f"Could not download audio: {e}"),502

def gemini_generate(prompt,key,model):
    """Call Gemini with retries. Returns (result_text, error_message)."""
    attempts=3; last_err=None
    for i in range(attempts):
        try:
            r=requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",params={"key":key},json={"contents":[{"parts":[{"text":prompt}]}]},timeout=60)
            if r.status_code==200:
                parts=r.json().get("candidates",[{}])[0].get("content",{}).get("parts",[])
                result="".join(p.get("text","") for p in parts).strip()
                return (result,None) if result else (None,"Empty translation returned.")
            try:e=r.json().get("error",{}).get("message",r.text)
            except:e=r.text
            last_err=e
            if r.status_code in (429,500,502,503,504) and i<attempts-1:
                time.sleep(1.2*(i+1)); continue
            return None,f"Translation API error: {e}"
        except requests.RequestException as e:
            last_err=str(e)
            if i<attempts-1: time.sleep(1.2*(i+1)); continue
            return None,f"Network error: {e}"
    return None,f"Translation API error: {last_err}"

@app.post("/api/translate")
def translate():
    d=request.get_json(silent=True) or {}; text=d.get("text","").strip(); target=d.get("target","").strip(); title=d.get("title","").strip()
    load_env(); key=os.getenv("GEMINI_API_KEY","").strip(); model=os.getenv("GEMINI_MODEL","gemini-3.5-flash-lite")
    if not text:return jsonify(error="Please paste your speech first."),400
    if not key:return jsonify(error="Open Settings and add your Gemini API key first."),400
    prompt=f"""Translate the COMPLETE speech below into {target}.
Do not summarize, shorten, omit, or add ideas. Preserve meaning, emotional tone, intent and paragraph structure.
Use natural fluent {target}, not awkward word-for-word translation.
Correct punctuation professionally, including commas, periods, question marks, exclamation marks, quotation marks and paragraph breaks.
Keep names, numbers and factual details accurate.
Return ONLY the finished translated speech, and nothing else — no title, no heading, no extra commentary.

SOURCE:
{text}"""
    result,err=gemini_generate(prompt,key,model)
    if err:return jsonify(error=err),502
    title_translation=""
    if title:
        title_prompt=f"""Translate ONLY this video title into {target}. Keep it short, natural, and title-like.
Return ONLY the translated title text, nothing else — no quotes, no explanation.

TITLE:
{title}"""
        tt,terr=gemini_generate(title_prompt,key,model)
        if not terr and tt: title_translation=tt
    return jsonify(translation=result,title_translation=title_translation)

@app.post("/api/detect_channel_mention")
def detect_channel_mention():
    d=request.get_json(silent=True) or {}
    transcript=d.get("transcript","").strip()
    load_env(); key=os.getenv("GEMINI_API_KEY","").strip(); model=os.getenv("GEMINI_MODEL","gemini-3.5-flash-lite")
    if not transcript:return jsonify(error="Nothing to check."),400
    if not key:return jsonify(error="Open Settings and add your Gemini API key first."),400
    prompt=f"""Read this YouTube speech/transcript. Does the speaker mention their OWN channel name or brand name anywhere in it (e.g. "subscribe to [X]", "welcome back to [X]", "this is [X] channel")?
If yes, reply with ONLY that exact channel/brand name, nothing else.
If no such mention exists, reply with exactly: NONE

TRANSCRIPT:
{transcript}"""
    result,err=gemini_generate(prompt,key,model)
    if err or not result:return jsonify(found=False,name="")
    result=result.strip().strip('"').strip()
    if result.upper()=="NONE" or len(result)>80:
        return jsonify(found=False,name="")
    return jsonify(found=True,name=result)

@app.post("/api/beat_competitor")
def beat_competitor():
    d=request.get_json(silent=True) or {}
    title=d.get("title","").strip()
    transcript=d.get("transcript","").strip()
    target_chars=d.get("target_chars")
    competitor_channel=d.get("competitor_channel","").strip()
    user_channel=d.get("user_channel","").strip()
    load_env(); key=os.getenv("GEMINI_API_KEY","").strip(); model=os.getenv("GEMINI_MODEL","gemini-3.5-flash-lite")
    if not transcript:return jsonify(error="Fetch a competitor video first."),400
    if not key:return jsonify(error="Open Settings and add your Gemini API key first."),400
    orig_words=len(transcript.split())
    orig_chars=len(transcript)
    try:
        tc=int(target_chars)
        aim_chars=tc if tc>0 else orig_chars
    except (TypeError,ValueError):
        aim_chars=orig_chars
    tol=1500
    min_chars,max_chars=aim_chars-tol,aim_chars+tol
    length_line=f"EXACTLY approximately {aim_chars:,} characters — it must land between {min_chars:,} and {max_chars:,} characters, no more and no less. This is a hard requirement, not a rough guide."
    if competitor_channel and user_channel:
        branding_line=f"""BRANDING: The competitor's speech mentions their own channel/brand name, "{competitor_channel}". Wherever the competitor speech mentions "{competitor_channel}" (e.g. asking viewers to subscribe, welcoming them, or referring to their own channel), your new speech must mention OUR channel/brand name, "{user_channel}", at the equivalent point instead — naturally, as if the speaker is promoting their own channel. Never mention "{competitor_channel}" anywhere in your output."""
    elif user_channel:
        branding_line=f"""BRANDING: Naturally include one mention of our channel/brand name, "{user_channel}", at an appropriate point (e.g. a welcome line or a call to subscribe)."""
    else:
        branding_line="BRANDING: No channel/brand name substitution is needed for this speech."
    prompt=f"""You are an expert YouTube scriptwriter and audience-retention specialist.

Below is a competitor's video title and their full speech/transcript. Read it and extract: the core idea, the main topics/points it covers, the examples and facts it uses, and its overall angle on the subject.

COMPETITOR TITLE:
{title if title else "(not provided)"}

COMPETITOR SPEECH (source material for the core idea and topics ONLY — do not resize, reword, paraphrase, or lightly edit this text):
{transcript}

TASK: This is NOT a resize or rewrite job. Using the same core idea and topics as raw material, WRITE A COMPLETELY NEW SPEECH FROM SCRATCH, for the SAME video title, that is genuinely more engaging and higher-retention than the competitor's — a stronger hook in the first few seconds, tighter pacing, better story structure, more compelling delivery, more vivid language, and smarter use of curiosity/tension to keep viewers watching all the way through. It should cover the same ground (and can go deeper or add sharper examples, or trim to fit) but every sentence should be freshly written by you, not adapted from the competitor's phrasing or structure. The end result must genuinely outperform the competitor's speech in quality and retention.

{branding_line}

REQUIRED LENGTH: {length_line}

Return ONLY the new speech text, nothing else — no title, no notes, no commentary."""
    result,err=gemini_generate(prompt,key,model)
    if err:return jsonify(error=err),502
    attempts=0
    while attempts<6:
        L=len(result)
        if min_chars<=L<=max_chars:break
        if L<min_chars:
            deficit=max(aim_chars-L,min_chars-L+300)
            fix=f"""This speech is currently {L:,} characters, but it must land between {min_chars:,} and {max_chars:,} characters (aim for {aim_chars:,}). Add roughly {deficit:,} more characters — as genuinely valuable content that raises retention further: a sharper example, a deeper explanation, a stronger story beat, an extra angle on the same topic. Do NOT pad with filler wording or repetition. The result must still be more engaging and higher-retention than the competitor's original, and must land in the required range. Return ONLY the expanded speech, nothing else.\n\nTEXT:\n{result}"""
        else:
            excess=max(L-aim_chars,L-max_chars+300)
            fix=f"""This speech is currently {L:,} characters, but it must land between {min_chars:,} and {max_chars:,} characters (aim for {aim_chars:,}). Cut roughly {excess:,} characters by tightening pacing and removing anything that slows retention — keep every strong hook, story beat, and example; remove only redundancy and slack. The result must remain more engaging and higher-retention than the competitor's original, and must land in the required range. Return ONLY the condensed speech, nothing else.\n\nTEXT:\n{result}"""
        r2,e2=gemini_generate(fix,key,model)
        if not e2 and r2:result=r2
        attempts+=1
    return jsonify(speech=result,length=len(result),words=len(result.split()),original_words=orig_words,original_length=orig_chars)

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)

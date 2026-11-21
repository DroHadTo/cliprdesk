"""GTA6cliptool core: Twitch discovery (live spikes + daily top clips), download, 9:16 render, notify, sqlite store."""
import asyncio, json, os, random, re, shutil, sqlite3, statistics, subprocess, sys, threading, time
from collections import Counter, deque, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import cycle
from pathlib import Path
from tempfile import TemporaryDirectory
from hashlib import file_digest
import requests
import secret_store
import network_policy

ROOT = Path(__file__).resolve().parent
if __name__ == "__main__":
    sys.modules["cliptool"] = sys.modules[__name__]
SECRET_LOAD_FAILURES = secret_store.initialize(ROOT / ".env")
legacy_data = ROOT / "data"
if (legacy_data / "cliptool.db").is_file() or not os.getenv("LOCALAPPDATA"):
    DATA = legacy_data
else:
    DATA = Path(os.environ["LOCALAPPDATA"]) / "ClipDesk" / "data"
OUT = DATA / "clips"
OUT.mkdir(parents=True, exist_ok=True)
DB = DATA / "cliptool.db"
TDCLI = os.getenv("TDCLI", str(ROOT / "bin" / "TwitchDownloaderCLI.exe"))
FFMPEG = os.getenv("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"  # TwitchDownloaderCLI needs a full path
HANDLE = os.getenv("TIKTOK_HANDLE", "")
TARGET = int(os.getenv("DAILY_TARGET", "10"))
CLIP_LENGTHS = (45, 60, 75)
DEFAULT_CLIP_LENGTH = CLIP_LENGTHS[0]
MAX_CLIP_LENGTH = max(CLIP_LENGTHS)
AUTO_CLIP_LEAD = 8
MAX_PER = int(os.getenv("MAX_PER_STREAMER_PER_DAY", "40"))
LANGS = [l for l in os.getenv("LANGUAGES", "en").split(",") if l]
PORT = int(os.getenv("PORT", "8788"))
WATCHLIST_ONLY = os.getenv("WATCHLIST_ONLY", "").lower() in {"1", "true", "yes"}
TODAY_PATH = "/today"
FONT = os.getenv("FONT", "C\\:/Windows/Fonts/arialbd.ttf")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")   # auto = try CUDA, fall back to CPU int8
RENDER_WORKERS = threading.BoundedSemaphore(int(os.getenv("RENDER_WORKERS", "2")))
RECORD_MAX = int(os.getenv("RECORD_MAX", "4"))
KEYWORDS = {"lul": 1, "kekw": 1, "omegalul": 2, "lmao": 1, "pog": 1, "pogchamp": 1, "poggers": 1,
            "clip": 3, "clipit": 5, "clip it": 5, "holy": 1, "wtf": 1, "monkas": 1, "w": 1, "kekl": 1}
# always-on chat watch words (merged with the user's own list on the Watchlist page)
BASE_WORDS = ["clip", "clip it", "clipped", "clip that", "lmao", "lmfao", "lol", "lul", "kekw", "omegalul", "icant", "wtf", "omg",
              "no way", "nah", "bro", "holy", "pog", "poggers", "w", "l", "ratio", "dead", "crazy", "insane", "yooo", "sheesh",
              "goat", "cops", "police", "wanted", "chase", "crash", "rip", "monkas", "aware", "ez", "gg", "fail", "help", "run"]
DROUGHT = int(os.getenv("CLIP_EVERY_MIN", "30")) * 60  # no clip this long -> take the next modest chat bump

def log(*a):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} " + " ".join(map(str, a))
    print(line, flush=True)
    with open(DATA / "cliptool.log", "a", encoding="utf-8") as f: f.write(line + "\n")

def today(): return datetime.now().strftime("%Y-%m-%d")

# ---------------- store ----------------
SCHEMA_VERSION = 2
TRIAL_SCHEMA = """create table if not exists tiktok_post_trials(clip_id text primary key, channel text not null,
  mode text not null check(mode in ('regular','experiment','fallback')), score integer not null,
  event text not null, expected_views integer, baseline_samples integer not null);"""
SCHEMA = """
create table if not exists clips(id text primary key, day text, created text, source text, streamer text,
  title text, views int, url text, game text, file text, hook text, caption text, hashtags text, credit text,
  spike text, posted text, tiktok_url text, variant text, workspace text not null default 'gta6');
create table if not exists creator_channels(channel text primary key, own integer not null,
  enabled integer not null default 0);
create table if not exists stats(clip_id text, hours int, views int, likes int, shares int, comments int, saves int,
  entered text, primary key(clip_id, hours));
create table if not exists takedowns(id integer primary key, clip_id text, date text, who text, reason text, action text);
create table if not exists creator_permissions(id integer primary key, scope text not null, subject text not null,
  credit text not null, evidence text not null, monetize integer not null, expires text not null,
  recorded text not null default current_timestamp, revoked text);
create index if not exists creator_permission_subject on creator_permissions(scope,subject) where revoked is null;
create table if not exists campaigns(id integer primary key, platform text, name text, url text, rate text,
  min_sec int, max_sec int, aspect text, required_tags text, required_link text, gta_ok text, notes text, active int default 1);
create table if not exists submissions(id integer primary key, clip_id text, campaign_id int, date text,
  link text, views int, payout text, status text);
create table if not exists whop_oauth(id integer primary key check(id=1), client text not null, worker text not null,
  access text not null, refresh text not null, expires real not null);
create table if not exists whop_scan(id integer primary key check(id=1), checked real not null, detail text not null);
create table if not exists whop_bounties(id text primary key, checked real not null, revision text not null, payload text not null);
create table if not exists whop_rules(bounty_id text primary key, revision text not null, worker text not null, rules text not null);
create table if not exists whop_entries(bounty_id text primary key, clip_id text not null unique, worker text not null,
  url text not null unique, day text not null, attempted real not null, state text not null, remote_id text,
  views integer not null, reward text not null, checked real not null, detail text not null default '');
create table if not exists post_drafts(clip_id text primary key, revision integer not null,
  state text not null, run_after real not null, attempts integer not null default 0,
  caption text not null default '', scene text not null default '', embedding text,
  detail text not null default '', reserved_day text, caption_style integer not null default 0);
create table if not exists tiktok_automation(id integer primary key check(id=1), enabled integer not null,
  organization text not null, channel text not null, account_name text not null, after_clip integer not null);
create table if not exists tiktok_uploads(clip_id text primary key, organization text not null,
  channel text not null, state text not null, asset_key text not null, asset_url text, buffer_id text,
  created real not null, submitted real, run_after real not null, detail text not null default '');
create table if not exists growth_posts(channel text not null, buffer_id text not null, sent real not null,
  observed real not null, url text not null, caption text not null, views integer not null,
  shares integer, watch_seconds real, checkpoint_views integer, checkpoint_time real,
  primary key(channel, buffer_id));
create table if not exists growth_sync(channel text primary key, checked real not null, status text not null);
create table if not exists monitored_posts(platform text not null, account_id text not null, post_id text not null,
  buffer_id text not null, published real not null, detected real not null, url text not null, caption text not null,
  primary key(platform, account_id, post_id));
create index if not exists monitored_posts_recent on monitored_posts(platform,account_id,published desc);
create table if not exists monitored_metrics(platform text not null, account_id text not null, post_id text not null,
  observed real not null, values_json text not null,
  primary key(platform,account_id,post_id,observed),
  foreign key(platform,account_id,post_id) references monitored_posts(platform,account_id,post_id));
create table if not exists audience_replies(id integer primary key, platform text not null, account_id text not null,
  post_id text not null, comment text not null, reply text not null default '', state text not null default 'pending',
  updated real not null, unique(platform,account_id,post_id,comment),
  foreign key(platform,account_id,post_id) references monitored_posts(platform,account_id,post_id));
create table if not exists growth_references(url text primary key, views integer, opening text not null,
  observed real not null);
create table if not exists growth_trends(id integer primary key check(id=1), checked real not null,
  country text not null, status text not null, data text not null);
create table if not exists growth_source_research(source text primary key, checked real not null,
  status text not null, data text not null);
create table if not exists growth_visual_research(url text primary key, source text not null,
  creator text not null, checked real not null, pattern text not null, opening text not null, payoff text not null);
create table if not exists clip_quality(clip_id text primary key, stamp integer not null, state text not null,
  score integer, detail text not null, checked real not null, retry_after real not null default 0);
create table if not exists clip_publish_captions(clip_id text primary key, stamp integer not null,
  caption text not null, angle text not null);
create table if not exists clip_virality(clip_id text primary key, stamp integer not null,
  preliminary integer not null, score integer not null, state text not null,
  audience integer not null, event text not null default '', calibration integer not null default 0);
create table if not exists tiktok_caption_receipts(clip_id text primary key, channel text not null,
  caption text not null, angle text not null, created real not null);
create index if not exists tiktok_caption_recent on tiktok_caption_receipts(channel,created desc);
""" + TRIAL_SCHEMA + """
create table if not exists growth_milestones(channel text not null, target integer not null,
  reached real not null, hits integer not null, samples integer not null, primary key(channel,target));
create table if not exists buffer_requests(at real not null);
create table if not exists buffer_cooldown(id integer primary key check(id=1), until real not null);
create table if not exists r2_staging_usage(asset_key text not null, bytes integer not null,
  at real not null, retained integer not null default 1);
"""
def migrate_schema(conn):
    # Take the write lock before inspecting legacy columns so concurrent workers can start safely.
    conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(clips)")}
    for name, definition in (
        ("workspace", "text not null default 'gta6'"),
        ("swap", "text"), ("flags", "text"), ("pick", "text"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {definition}")
    if "caption_style" not in {row["name"] for row in conn.execute("PRAGMA table_info(post_drafts)")}:
        conn.execute("ALTER TABLE post_drafts ADD COLUMN caption_style integer not null default 0")
    trial_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='tiktok_post_trials'").fetchone()[0]
    if "'fallback'" not in trial_sql:
        conn.execute("ALTER TABLE tiktok_post_trials RENAME TO tiktok_post_trials_previous")
        conn.execute(TRIAL_SCHEMA)
        conn.execute("INSERT INTO tiktok_post_trials SELECT * FROM tiktok_post_trials_previous")
        conn.execute("DROP TABLE tiktok_post_trials_previous")
    conn.execute("CREATE INDEX IF NOT EXISTS clips_workspace_recent ON clips(workspace, created)")
    conn.execute("CREATE INDEX IF NOT EXISTS clips_workspace_day ON clips(workspace, day, created)")
    conn.execute("CREATE INDEX IF NOT EXISTS takedowns_clip ON takedowns(clip_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS takedowns_subject ON takedowns(lower(trim(clip_id)))")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def db():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            migrate_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def used_today(c, workspace="gta6"):
    rows = c.execute("select streamer, count(*) n from clips where day=? and workspace=? group by streamer",
                     (today(), workspace)).fetchall()
    return {r["streamer"].lower(): r["n"] for r in rows}

def stored_watchlist():
    return json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))


def creator_profile():
    path = DATA / "creators.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def creator_names(profile):
    return {name.strip().lower(): key for key, creator in (profile or {}).items()
            for name in (creator["name"], *creator.get("aliases", ()), *creator.get("twitch", ()), *creator.get("kick", ()))}


def creator_channel_allowed(channel, profile):
    if profile is None:
        return True
    platform, slug = ("kick", channel[5:]) if channel.startswith("kick:") else ("twitch", channel)
    return any(slug in creator.get(platform, ()) for creator in profile.values())


TAKEN_DOWN = """EXISTS (SELECT 1 FROM takedowns t WHERE t.clip_id=c.id
                  OR lower(trim(t.clip_id))=lower(trim(c.streamer)))"""


def creator_filter(profile, selected=""):
    names = creator_names(profile)
    available = f"c.workspace='gta6' AND NOT {TAKEN_DOWN}"
    if profile is None:
        return (f"{available} AND lower(trim(c.streamer))=?", (selected,)) if selected else (available, ())
    values = tuple(name for name, key in names.items() if not selected or key == selected)
    return (f"{available} AND lower(trim(c.streamer)) IN (" + ",".join("?" for _ in values) + ")", values) if values else ("0", ())


CREATOR_LIBRARY = f"c.workspace='creators' AND NOT {TAKEN_DOWN}"


def library_filter():
    gta, params = creator_filter(creator_profile())
    return f"(({gta}) OR ({CREATOR_LIBRARY}))", params


def creator_channels():
    with closing(db()) as conn:
        return {row["channel"]: dict(row) for row in conn.execute("SELECT * FROM creator_channels ORDER BY channel")}


def creator_enabled(channel, conn=None):
    if conn is None:
        with closing(db()) as reader:
            return creator_enabled(channel, reader)
    return bool(conn.execute("SELECT 1 FROM creator_channels WHERE channel=? AND enabled=1", (channel,)).fetchone())


def parse_creator_channel(platform, raw):
    if platform not in {"twitch", "kick"}:
        raise ValueError("Choose Twitch or Kick.")
    raw = raw.strip().lower()
    host = "twitch.tv" if platform == "twitch" else "kick.com"
    name = r"[a-z0-9_]{2,25}" if platform == "twitch" else r"[a-z0-9_-]{2,40}"
    pattern = fr"(?:(?:https?://)?(?:www\.)?{re.escape(host)}/|@)?({name})/?"
    match = re.fullmatch(pattern, raw)
    if not match:
        raise ValueError(f"Enter a {platform.title()} channel URL or username.")
    return ("kick:" if platform == "kick" else "") + match[1]


def watchlist():
    settings = stored_watchlist()
    local_boxes = DATA / "facecams.json"
    if local_boxes.is_file():
        settings.setdefault("facecam", {}).update(json.loads(local_boxes.read_text(encoding="utf-8")))
    profile = creator_profile()
    if profile is not None:
        excluded = {name.lower() for name in settings["exclude"]}
        for platform, field in (("twitch", "include"), ("kick", "kick_include")):
            settings[field] = list(dict.fromkeys(slug for creator in profile.values() for slug in creator.get(platform, ())
                                               if slug not in excluded and f"{platform}:{slug}" not in excluded))
    return settings


def find_facecam(raw, start, duration, streamer, prior=None):
    import facecam

    name = (streamer or "").strip().lower()
    key = creator_names(creator_profile()).get(name, name)
    boxes = watchlist().get("facecam", {})
    try:
        return facecam.locate(raw, start, duration, boxes.get(key) or boxes.get(name) or prior)
    except Exception as exc:
        log("facecam scan failed", streamer, type(exc).__name__)
        return None


# ---------------- Twitch Helix (official API) ----------------
_tok = {"v": None, "exp": 0}
def helix(path, **params):
    cid, sec = os.getenv("TWITCH_CLIENT_ID"), secret_store.value("TWITCH_CLIENT_SECRET")
    if not cid or not sec: raise RuntimeError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET missing in .env (add them on the Setup page)")
    if time.time() > _tok["exp"] - 60:
        r = requests.post("https://id.twitch.tv/oauth2/token", data={"client_id": cid, "client_secret": sec,
                          "grant_type": "client_credentials"}, timeout=20, allow_redirects=False); r.raise_for_status()
        j = r.json(); _tok.update(v=j["access_token"], exp=time.time() + j["expires_in"])
    r = requests.get(f"https://api.twitch.tv/helix/{path}", params=params, timeout=20, allow_redirects=False,
                     headers={"Client-Id": cid, "Authorization": f"Bearer {_tok['v']}"})
    r.raise_for_status(); return r.json()

_games = {}
def game_ids():
    names = watchlist()["games"]
    missing = [n for n in names if n not in _games]
    if missing:
        for g in helix("games", name=missing).get("data", []): _games[g["name"]] = g["id"]
    return {_games[n]: n for n in names if n in _games}

def iso(dt): return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------- Live now lists (Twitch + Kick official APIs) ----------------
def twitch_live(n=50):
    games = list(game_ids())
    profile = creator_profile()
    if profile is None and not WATCHLIST_ONLY:
        rows = helix("streams", game_id=games, first=n, type="live")["data"]
    else:
        logins = watchlist()["include"]
        rows = helix("streams", user_login=logins[:100], type="live")["data"] if logins else []
        rows = [stream for stream in rows if stream["game_id"] in games and creator_channel_allowed(stream["user_login"], profile)]
    return sorted(rows, key=lambda s: -s["viewer_count"])

_kick = {"v": None, "exp": 0, "cats": None}
def kick_live(n=50):
    """Kick public API (api.kick.com). List-only: Kick has no public clips API, so Kick is not auto-clipped."""
    cid, sec = os.getenv("KICK_CLIENT_ID"), secret_store.value("KICK_CLIENT_SECRET")
    if not cid or not sec: return None
    if time.time() > _kick["exp"] - 60:
        r = requests.post("https://id.kick.com/oauth/token", data={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}, timeout=20, allow_redirects=False)
        r.raise_for_status(); j = r.json(); _kick.update(v=j["access_token"], exp=time.time() + j.get("expires_in", 3600))
    h = {"Authorization": f"Bearer {_kick['v']}"}
    if _kick["cats"] is None:
        r = requests.get("https://api.kick.com/public/v2/categories", params={"name": ",".join(watchlist()["games"])}, headers=h, timeout=20, allow_redirects=False)
        r.raise_for_status(); _kick["cats"] = [c["id"] for c in r.json().get("data", [])]
    out = []
    for cat in _kick["cats"]:
        r = requests.get("https://api.kick.com/public/v1/livestreams", params={"category_id": cat, "sort": "viewer_count", "limit": min(n, 100)}, headers=h, timeout=20, allow_redirects=False)
        r.raise_for_status(); out += r.json().get("data", [])
    return sorted(out, key=lambda s: -s.get("viewer_count", 0))

# ---------------- captions (local faster-whisper, free, CPU) ----------------
_wm = None
def captions(src, start, dur, work):
    """Write captions.ass: 1-2 words at a time, huge bold uppercase white, thick black outline, centred."""
    global _wm
    wav = work / "a.wav"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-i", str(src), "-t", str(dur), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                   check=True, creationflags=NOWIN)
    with wav.open("rb") as audio_file:
        digest = file_digest(audio_file, "sha1")
    digest.update(WHISPER_MODEL.encode())
    key = digest.hexdigest()
    cache = DATA / "txcache" / f"{key}-hl.ass"; cache.parent.mkdir(exist_ok=True)
    if cache.exists():
        shutil.copy(cache, work / "captions.ass"); wav.unlink(missing_ok=True)
        transcript = cache.with_suffix(".txt")
        if transcript.exists():
            shutil.copyfile(transcript, work / "transcript.txt")
        return "captions.ass", (cache.with_suffix(".n").read_text() if cache.with_suffix(".n").exists() else "0")
    try:
        from faster_whisper import BatchedInferencePipeline, WhisperModel
    except ImportError:
        wav.unlink(missing_ok=True)
        log("faster-whisper not installed: no captions")
        return None, "0"
    if _wm is None:
        for dev, ct_ in ([("cuda", "float16")] if WHISPER_DEVICE in ("auto", "cuda") else []) + [("cpu", "int8")]:
            try:
                _wm = BatchedInferencePipeline(model=WhisperModel(WHISPER_MODEL, device=dev, compute_type=ct_)); log("whisper on", dev); break
            except Exception as e: log("whisper", dev, "unavailable:", str(e)[:120])
    import wave, numpy as np  # decode the wav ourselves: avoids PyAV version clashes inside faster-whisper
    with wave.open(str(wav)) as wf: audio = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float32) / 32768
    try: segs, _ = _wm.transcribe(audio, word_timestamps=True, vad_filter=True, batch_size=8, task="translate"); segs = list(segs)
    except Exception as e:  # CUDA libs missing at run time -> CPU int8
        log("whisper GPU failed, using CPU:", str(e)[:120])
        _wm = BatchedInferencePipeline(model=WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8"))
        segs, _ = _wm.transcribe(audio, word_timestamps=True, vad_filter=True, batch_size=8, task="translate")
    words = [w for s in segs for w in (s.words or []) if w.word.strip()]
    wav.unlink(missing_ok=True)
    if not words: return None, "0"
    transcript = " ".join(s.text.strip() for s in segs if s.text.strip())
    (work / "transcript.txt").write_text(transcript, encoding="utf-8")
    t = lambda x: f"{int(x // 3600)}:{int(x % 3600 // 60):02d}:{x % 60:05.2f}"
    ev, i = [], 0
    while i < len(words):
        grp = words[i:i + 2]
        if len(grp) == 2 and len(grp[0].word.strip() + grp[1].word.strip()) > 12: grp = grp[:1]
        clean = [re.sub(r"[{}\\]", "", w.word.strip()).upper() for w in grp]
        for k, w in enumerate(grp):  # word highlight: the word being spoken sits on a neon-pink box
            end = grp[k + 1].start if k + 1 < len(grp) else max(w.end, grp[0].start + 0.3)
            txt = " ".join("{\\3c&H882EFF&\\bord22}" + c + "{\\3c&H000000&\\bord10}" if j == k else c for j, c in enumerate(clean))
            ev.append(f"Dialogue: 0,{t(w.start)},{t(max(end, w.start + 0.15))},Cap,,0,0,0,,{txt}")
        i += len(grp)
    (work / "captions.ass").write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Cap,Arial Black,118,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,10,0,5,60,60,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + "\n".join(ev) + "\n", encoding="utf-8", newline="\n")
    shutil.copy(work / "captions.ass", cache); cache.with_suffix(".n").write_text(str(len(words)))
    cache.with_suffix(".txt").write_text(transcript, encoding="utf-8")
    return "captions.ass", str(len(words))

# ---------------- download + render ----------------
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BRAND_LAYOUT = "gta-vi"
CREATOR_LAYOUT = "creator-highlight"
VIDEO_HEIGHT = 920
VIDEO_TOP = 388


def edit_metadata(path):
    sidecar = Path(path).with_suffix(".json")
    if sidecar.is_file():
        return json.loads(sidecar.read_text(encoding="utf-8"))
    return {}


def select_edit(src, dst):
    """Replace the selected video and track the exact cut it carries."""
    with TemporaryDirectory(dir=dst.parent) as folder:
        pending = Path(folder) / dst.name
        shutil.copyfile(src, pending)
        details = src.with_suffix(".json")
        if details.is_file():
            shutil.copyfile(details, pending.with_suffix(".json"))
        pending.replace(dst)
        if pending.with_suffix(".json").is_file():
            pending.with_suffix(".json").replace(dst.with_suffix(".json"))
        else:
            dst.with_suffix(".json").unlink(missing_ok=True)


def render(src, dst, credit, start=0, dur=None, facecam=None, subs=None, style="split", peak=None, workspace="gta6"):
    """Render the workspace title, footage, creator credit and captions."""
    dst = Path(dst)
    work = dst.parent
    (work / "credit.txt").write_text(ascii_only(credit), encoding="utf-8", newline="\n")
    fcrop = f"crop=iw*{facecam[2]}:ih*{facecam[3]}:iw*{facecam[0]}:ih*{facecam[1]}" if facecam else ""
    bg = "0x170e22"
    def fit(height):
        return (f"scale=1080:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,"
                f"pad=1080:{height}:(ow-iw)/2:(oh-ih)/2:{bg}")

    fill = (f"scale=1080:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase:force_divisible_by=2,"
            f"crop=1080:{VIDEO_HEIGHT},setsar=1")
    game = fill if style in ("corner", "hype") else fit(VIDEO_HEIGHT)
    if style == "hype":
        p = float(peak) if peak is not None else 20.0
        z = f"(1+0.28*between(t,{p - 0.6},{p + 1.8})+0.12*lt(mod(t,6),0.35))"
        game += f",scale=w='trunc(1080*{z}/2)*2':h=-2:eval=frame,crop=1080:{VIDEO_HEIGHT},setsar=1"
    if style == "split" and facecam:
        vf = (f"[0:v]split[a][b];[a]{fcrop},{fit(344)}[top];"
              f"[b]{fit(VIDEO_HEIGHT - 344)}[bottom];[top][bottom]vstack,")
    elif facecam:
        vf = (f"[0:v]split[a][b];[a]{game}[game];[b]{fcrop},scale=338:260:force_original_aspect_ratio=decrease:"
              "force_divisible_by=2,setsar=1,pad=iw+8:ih+8:4:4:color=0xff2e88[fc];"
              "[game][fc]overlay=W-w-24:24,")
    else:
        vf = f"[0:v]{game},"
    heading, size = ("HIGHLIGHT", 116) if workspace == "creators" else ("GTA VI", 158)
    vf += (f"pad=1080:1920:0:{VIDEO_TOP}:{bg},"
           f"drawtext=fontfile='{FONT}':text='{heading}':fontsize={size}:fontcolor=0xfff3fb:borderw=6:bordercolor=0x9d2264:"
           "shadowcolor=0x521e66:shadowx=0:shadowy=7:x=(w-text_w)/2:y=202,"
           f"drawtext=fontfile='{FONT}':textfile=credit.txt:expansion=none:fontsize=40:fontcolor=0xe5d3ea:"
           "x=(w-text_w)/2:y=1550")
    if subs:
        subtitles = (work / subs).read_text(encoding="utf-8")
        subtitles = subtitles.replace("Style: Cap,Arial Black,118,", "Style: Cap,Arial Black,94,", 1)
        subtitles = subtitles.replace(",5,60,60,0,1\n", ",2,60,60,417,1\n", 1)
        (work / "captions_pos.ass").write_text(subtitles, encoding="utf-8", newline="\n")
        vf += ",ass=captions_pos.ass"
    vf += "[v]"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-ss", str(start), "-i", str(Path(src).resolve())]
    if dur:
        cmd += ["-t", str(dur)]
    cmd += ["-filter_complex_threads", "1", "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", *ENC_ARGS,
            "-pix_fmt", "yuv420p", "-r", "30", "-af",
            "highpass=f=55,acompressor=threshold=0.16:ratio=2:attack=15:release=180,alimiter=limit=0.89:level=false:latency=true",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", dst.name]
    r = subprocess.run(cmd, cwd=work, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=900, creationflags=NOWIN)
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-400:]}")
    if subs:
        shutil.copyfile(work / subs, dst.with_suffix(".ass"))
    else:
        dst.with_suffix(".ass").unlink(missing_ok=True)
    layout = CREATOR_LAYOUT if workspace == "creators" else BRAND_LAYOUT
    dst.with_suffix(".json").write_text(json.dumps({"layout": layout, "start": start, "dur": dur, "face": facecam,
                                                   "face_visible": bool(facecam), "peak": peak}), encoding="utf-8")


def _pick_encoder():
    """GPU encode if the driver allows it: NVENC (needs NVIDIA driver 610+), then Intel QuickSync, else x264."""
    opts = {"nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"],
            "qsv": ["-c:v", "h264_qsv", "-preset", "faster", "-global_quality", "23"]}
    want = os.getenv("ENCODER", "auto")
    for name in (["nvenc", "qsv"] if want == "auto" else [want] if want in opts else []):
        try:
            r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=1280x720:d=0.3", "-pix_fmt", "yuv420p", *opts[name], "-f", "null", "-"],
                               capture_output=True, timeout=20, creationflags=NOWIN)
            if r.returncode == 0: return name, opts[name]
        except Exception: pass
    return "x264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
ENCODER, ENC_ARGS = _pick_encoder()

def probe_duration(path):
    r = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True, errors="ignore", timeout=30, creationflags=NOWIN)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0

def tdcli(*args):
    r = subprocess.run([TDCLI, *args, "--banner", "false"], capture_output=True, text=True, errors="ignore", timeout=300, creationflags=NOWIN)
    if r.returncode: raise RuntimeError(f"TwitchDownloaderCLI failed: {(r.stderr or r.stdout)[-400:]}")

STYLES = [s.strip() for s in os.getenv("VARIANTS", "split,corner,hype").split(",") if s.strip()]
RENDER_ALL_VARIANTS = os.getenv("RENDER_ALL_VARIANTS", "").lower() in ("1", "true", "yes")
def qa(path, subs, facecam, want_dur):
    """Pass/fail checks on a rendered clip. Returns '' if fine, else the reason."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True, errors="ignore", timeout=180, creationflags=NOWIN)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr); d = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0
    if abs(d - want_dur) > 2: return f"duration {d:.1f}s, wanted {want_dur:.0f}s"
    if "1080x1920" not in r.stderr: return "not 1080x1920"
    v = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
    if v and float(v[1]) < -55: return f"audio silent ({v[1]} dB)"
    if subs and not (Path(path).parent / subs).exists(): return "captions missing"
    if facecam and (facecam[2] * facecam[3] < 0.005 or facecam[0] + facecam[2] > 1.01 or facecam[1] + facecam[3] > 1.01): return "facecam box empty or off-frame"
    return ""
def lesson(text):
    with open(ROOT / "lessons.md", "a", encoding="utf-8") as f: f.write(f"- {today()}: {text}\n")

def make_clip(item):
    """item: id, source, streamer, title, views, url, game and one of: clip_id | vod_id+begin+end | local+start+dur."""
    profile = creator_profile()
    streamer = item["streamer"].strip().lower()
    workspace = item.get("workspace", "gta6")
    if workspace == "gta6":
        if profile is not None and streamer not in creator_names(profile):
            return None
        if WATCHLIST_ONLY and profile is None:
            wl = watchlist()
            if streamer not in {name.lower() for name in (*wl["include"], *wl.get("kick_include", []))}:
                return None
    elif workspace != "creators":
        return None
    with closing(db()) as c:
        if workspace == "creators" and not creator_enabled(item.get("channel", ""), c):
            return None
        if c.execute("select 1 from clips where id=?", (item["id"],)).fetchone(): return None
        if c.execute("SELECT 1 FROM takedowns WHERE clip_id=? OR lower(trim(clip_id))=?",
                     (item["id"], streamer)).fetchone():
            return None
    t0 = time.time()
    work = OUT / today() / re.sub(r"[^\w-]", "_", item["id"]); work.mkdir(parents=True, exist_ok=True)
    raw, start = work / "raw.mp4", item.get("start", 0)
    if item.get("clip_id"): tdcli("clipdownload", "--id", item["clip_id"], "-o", str(raw), "--ffmpeg-path", FFMPEG,
                                  "--collision", "Overwrite")
    elif item.get("vod_id"): tdcli("videodownload", "--id", item["vod_id"], "-b", f"{item['begin']}s", "-e", f"{item['end']}s",
                                    "-q", "720p60", "-o", str(raw), "--ffmpeg-path", FFMPEG, "--temp-path", str(work),
                                    "--collision", "Overwrite")
    elif item["source"] in ("live-buffer", "vod-import"):
        shutil.copyfile(item["local"], raw)
    else: raw = Path(item["local"])
    d = probe_duration(raw) - start
    if d < 7: log("skip short", item["id"], d); return None
    dur = min(d, item.get("dur") or MAX_CLIP_LENGTH, MAX_CLIP_LENGTH)
    import growth, posts, virality, writer
    w = writer.template(item)  # The publish caption is written after reviewing the finished edit.
    credit = f"@{item['streamer']} on {item.get('platform', 'Twitch')}"
    with RENDER_WORKERS:  # bounded render pool: captions + one GPU/CPU encode pass
        (work / "transcript.txt").unlink(missing_ok=True)
        try: subs, nwords = captions(raw, start, dur, work)
        except Exception as e: log("captions failed", e); subs, nwords = None, "0"
        face = find_facecam(raw, start, dur, item["streamer"])
        peak = max(0.0, min(dur - 1, float(item.get("peak", 20))))
        flags, ok = [], []
        preferred_style = STYLES[0] if workspace == "creators" else growth.choose_style(STYLES, item["id"])
        styles = [preferred_style, *(style for style in STYLES if style != preferred_style)]
        for st in styles:
            dst, why = work / f"tiktok_{st}.mp4", ""
            for attempt in range(3):
                try: render(raw, dst, credit, start=start, dur=dur, facecam=face, subs=subs, style=st, peak=peak, workspace=workspace); why = qa(dst, subs, face, dur)
                except Exception as e: why = f"render error: {str(e)[-120:]}"
                if not why: break
            if why: flags.append(f"{st}: {why}"); lesson(f"{st} render failed 3x for {item['streamer']}: {why}")
            else:
                ok.append(st)
                if not RENDER_ALL_VARIANTS:
                    break
        if not ok:
            log("no edit passed QA", item["id"])
            return None
        for st in ok:
            transcript = work / "transcript.txt"
            per_style = work / f"transcript_{st}.txt"
            if transcript.exists(): shutil.copyfile(transcript, per_style)
            else: per_style.unlink(missing_ok=True)
        chosen = ok[0]
        select_edit(work / f"tiktok_{chosen}.mp4", work / "tiktok.mp4")
    # Swap test: a clip where the streamer barely speaks and chat isn't laughing is probably generic game footage.
    laughs = sum(v for k, v in item.get("spike", {}).get("keywords", {}).items() if k in ("lul", "kekw", "omegalul", "lmao", "kekl"))
    swap = "unique" if int(nwords) >= 15 or laughs >= 5 else "generic"
    (work / "meta.json").write_text(json.dumps({"raw": str(Path(raw).resolve()), "start": start, "dur": dur, "face": face, "peak": peak}))  # raw kept for re-renders
    with posts.transaction() as c:
        if workspace == "creators" and not creator_enabled(item.get("channel", ""), c):
            return None
        if c.execute("SELECT 1 FROM takedowns WHERE clip_id=? OR lower(trim(clip_id))=?",
                     (item["id"], streamer)).fetchone():
            return None
        c.execute("insert into clips(id,day,created,source,streamer,title,views,url,game,file,hook,caption,hashtags,credit,spike,variant,swap,flags,pick,workspace)"
                  " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (item["id"], today(), datetime.now().isoformat(timespec="seconds"), item["source"], item["streamer"],
                   item["title"], item.get("views", 0), item["url"], item.get("game", ""), (work / "tiktok.mp4").relative_to(OUT).as_posix(),
                   w["hook"], w["caption"], " ".join(w["hashtags"]), credit, json.dumps(item.get("spike", {})), w.get("variant", ""), swap, "; ".join(flags), chosen, workspace))
        posts.enqueue(c, item["id"])
        row = c.execute("SELECT id, file, source, views, spike, pick FROM clips WHERE id=?", (item["id"],)).fetchone()
        if workspace == "gta6":
            virality.record_preliminary(c, row, work / "tiktok.mp4", (work / "tiktok.mp4").stat().st_mtime_ns)
    notify(f"New clip rendered: {item['streamer']}", f"{w['hook']} · Ready in the TikTok queue")
    log("made", item["id"], item["streamer"], f"{time.time() - t0:.0f}s")
    return item["id"]

# ---------------- notify ----------------
def notify(title, msg):
    topic = secret_store.value("NTFY_TOPIC")
    server = network_policy.endpoint(os.getenv("NTFY_SERVER", "https://ntfy.sh"))
    if server and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        try:
            response = requests.post(f"{server}/{topic}",
                                     data=msg.encode("utf-8"), timeout=10, allow_redirects=False,
                                     headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": "movie_camera"})
            if not response.ok or response.is_redirect:
                log("ntfy failed", response.status_code)
        except requests.RequestException as exc:
            log("ntfy failed", type(exc).__name__)

# ---------------- chat spike scoring (shared by LIVE and VOD) ----------------
BUCKET = 10        # seconds per chat bucket
BASE = 30          # buckets in rolling baseline (5 min)


def lead_seconds(length):
    return 2 * length // 3


def vod_window(moment, length, total=None):
    start = max(0, moment - lead_seconds(length))
    if total is None:
        return start, start + length
    start = min(start, max(0, total - length))
    return start, min(start + length, total)


def vod_clip_id(video_id, moment):
    return f"vod-{video_id}-{max(0, moment - 20)}"  # retain IDs from the original 20s lead-in


def make_twitch_clip(clip, item, length):
    """Use extended source footage when a Twitch highlight is shorter than the target."""
    available = clip["duration"]
    offset, video_id = clip.get("vod_offset"), clip.get("video_id")
    if available < length and offset is not None and video_id:
        moment = offset + available / 2
        begin, end = vod_window(moment, length)
        try:
            result = make_clip(dict(item, vod_id=video_id, begin=begin, end=end, dur=length, peak=moment - begin))
            if result:
                return result
        except Exception as e:
            log("extended Twitch clip unavailable", clip["id"], type(e).__name__)
    return make_clip(dict(item, clip_id=clip["id"], dur=length, start=max(0, (available - length) / 2)))

def score(text):
    t = text.lower(); s, hit = 1.0, None
    for k, v in KEYWORDS.items():
        if (" " in k and k in t) or k in t.split(): s += v; hit = k
    return s, hit

def is_hot(n, hist):  # sustain check for the bucket after a spike
    return len(hist) >= 6 and n >= max(10, statistics.mean(hist) * 1.5)

def is_spike(n, hist):
    if len(hist) < 6: return False
    mu = statistics.mean(hist); sd = statistics.pstdev(hist) or 1
    return n >= max(12, mu * 2, mu + 2.5 * sd)

# ---------------- VOD backfill via TwitchDownloader chat JSON ----------------
def vod(vod_id, max_clips=5, workspace="gta6"):
    vod_id = re.sub(r"\D", "", str(vod_id).split("videos/")[-1])
    if not vod_id:
        raise ValueError("A Twitch VOD ID is required.")
    work = DATA / "vodchat"; work.mkdir(exist_ok=True); f = work / f"{vod_id}.json"
    if not f.exists(): tdcli("chatdownload", "--id", vod_id, "-o", str(f))
    j = json.loads(f.read_text(encoding="utf-8"))
    stream_video = j.get("video") or {}
    streamer = j.get("streamer", {}).get("name", "unknown"); title = stream_video.get("title", "")
    channel = streamer.strip().lower()
    profile = creator_profile()
    if workspace == "creators" and not creator_enabled(channel):
        raise ValueError("Enable the Twitch channel in Creator channels before importing its VOD.")
    if workspace == "gta6" and profile is not None and channel not in creator_names(profile):
        log("VOD skipped for this creator list", streamer)
        return []
    total = stream_video.get("length")
    total = total if isinstance(total, (int, float)) and total > 0 else None
    buckets = defaultdict(float)
    reactions = defaultdict(Counter)
    for m in j.get("comments", []):
        bucket = int(m["content_offset_seconds"] // BUCKET)
        points, hit = score((m.get("message") or {}).get("body", ""))
        buckets[bucket] += points
        if hit:
            reactions[bucket][hit] += 1
    hist, spikes = deque(maxlen=BASE), []
    evidence = {}
    for b in range(max(buckets, default=0) + 1):
        n = buckets.get(b, 0.0)
        if is_spike(n, hist):
            baseline = statistics.mean(hist)
            offset = b * BUCKET
            spikes.append((n / (baseline or 1), offset))
            evidence[offset] = {"offset": offset, "score": n, "baseline": baseline, "keywords": reactions[b]}
        hist.append(n)
    picked = []
    for ratio, off in sorted(spikes, reverse=True):
        if all(abs(off - p) >= MAX_CLIP_LENGTH for p in picked): picked.append(off)
        if len(picked) >= max_clips: break
    made = []
    for off, length in zip(sorted(picked), cycle(CLIP_LENGTHS)):
        b, e = vod_window(off, length, total)
        try:
            clip_id = vod_clip_id(vod_id, off)
            r = make_clip(dict(id=f"creator-{clip_id}" if workspace == "creators" else clip_id,
                               source="vod-chat-spike", streamer=streamer, title=title, views=0, channel=channel,
                               workspace=workspace, game=stream_video.get("game") or "",
                               url=f"https://www.twitch.tv/videos/{vod_id}?t={b:g}s", vod_id=vod_id, begin=b, end=e,
                               dur=length, peak=off - b, spike=evidence[off]))
            if r: made.append(r)
        except Exception as e: log("vod clip failed", off, e)
    log(f"vod {vod_id}: {len(spikes)} spikes, made {len(made)}"); return made

# ---------------- daily backfill: top clips last 24h, 1 per streamer ----------------
def backfill(target=TARGET):
    lock = DATA / "backfill.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 3600: log("backfill already running"); return
    lock.touch()
    try:
        c = db(); used = used_today(c); wl = watchlist()
        excl = {s.lower() for s in wl["exclude"]}
        need = target - len(used)
        if need <= 0: log("backfill: target met", len(used)); return
        since = iso(datetime.now(timezone.utc) - timedelta(hours=24))
        gids = game_ids(); cands = []
        if creator_profile() is None and not WATCHLIST_ONLY:
            for gid, gname in gids.items():
                cur = None
                for _ in range(3):
                    j = helix("clips", game_id=gid, started_at=since, first=100, **({"after": cur} if cur else {}))
                    cands += [dict(x, _game=gname) for x in j["data"]]
                    cur = j.get("pagination", {}).get("cursor")
                    if not cur: break
        for name in wl["include"]:
            u = helix("users", login=name)["data"]
            if u: cands += [dict(x, _game=gids[x["game_id"]]) for x in helix("clips", broadcaster_id=u[0]["id"], started_at=since, first=20)["data"] if x["game_id"] in gids]
        cands.sort(key=lambda x: x["view_count"], reverse=True)
        made = 0
        for x in cands:
            s = x["broadcaster_name"].lower()
            if made >= need: break
            if s in used or s in excl or (LANGS and x.get("language") not in LANGS) or x["duration"] < 7: continue
            try:
                length = CLIP_LENGTHS[made % len(CLIP_LENGTHS)]
                if make_twitch_clip(x, dict(id=f"clip-{x['id']}", source="daily-top", streamer=x["broadcaster_name"], title=x["title"],
                                           views=x["view_count"], url=x["url"], game=x["_game"]), length):
                    used[s] = 1; made += 1
            except Exception as e: log("clip failed", x["id"], e)
        log(f"backfill done: +{made}, today {len(used)} streamers")
    finally: lock.unlink(missing_ok=True)

# ---------------- LIVE: Streamlink rolling buffer + anonymous IRC chat spikes ----------------
SEG = 10; WRAP = 18  # 10s segments, last 3 minutes kept per recorded streamer

def streamlink_bin():
    here = Path(sys.executable).parent  # in a .venv this is .venv\Scripts, where streamlink.exe lives
    sl = str(here / "streamlink.exe") if (here / "streamlink.exe").exists() else (shutil.which("streamlink") or str(here / "Scripts" / "streamlink.exe"))
    if not Path(sl).exists():
        sl = str(Path(os.getenv("APPDATA", "")) / "Python" / f"Python{sys.version_info[0]}{sys.version_info[1]}" / "Scripts" / "streamlink.exe")
    return sl

def ascii_only(t):
    """English-only on-screen text: drop emoji and non-Latin characters, tidy spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7E]", " ", t or "")).strip()

def twitch_live_nokey(login):
    """No API keys: ask Streamlink (its normal stream lookup) whether the channel is live, and in which category."""
    r = subprocess.run([streamlink_bin(), "--json", f"https://twitch.tv/{login}"], capture_output=True, text=True, encoding="utf-8", errors="ignore",
                       timeout=30, creationflags=NOWIN)
    try: j = json.loads(r.stdout or "{}")
    except ValueError: return None
    if not j.get("streams"): return None
    m = j.get("metadata") or {}
    return {"user_login": login, "user_name": m.get("author") or login, "game_name": m.get("category") or "", "title": ascii_only(m.get("title") or ""),
            "viewer_count": 0, "user_id": None, "id": m.get("id"), "language": "", "game_id": ""}

class Recorder:
    """streamlink -> ffmpeg segment muxer ring buffer. Started when a watched streamer goes live."""
    def __init__(self, login, url=None):
        self.started = time.time()
        base = Path(os.environ["LOCALAPPDATA"]) / "ClipDesk" if os.getenv("LOCALAPPDATA") else DATA
        root = base / "buffer" / str(PORT)
        try:
            root.mkdir(parents=True, exist_ok=True)
            self.buffer = TemporaryDirectory(prefix=f"{login.replace(':', '_')}-", dir=root, ignore_cleanup_errors=True)
        except OSError as exc:
            raise ValueError("Windows could not create the recording folder. Check available disk space and write permissions.") from exc
        self.dir = Path(self.buffer.name)
        try:
            self.sl = subprocess.Popen(
                [streamlink_bin(), "--stdout", "--twitch-disable-ads", url or f"https://twitch.tv/{login}", "720p60,720p,best"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NOWIN,
            )
        except OSError as exc:
            self.buffer.cleanup()
            raise ValueError("Streamlink could not start. Run Clip Desk Setup to repair the streaming tools.") from exc
        try:
            self.ff = subprocess.Popen(
                [FFMPEG, "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
                 "-f", "segment", "-segment_time", str(SEG), "-segment_wrap", str(WRAP),
                 "-reset_timestamps", "1", str(self.dir / "seg%03d.ts")],
                stdin=self.sl.stdout, stderr=subprocess.DEVNULL, creationflags=NOWIN,
            )
        except OSError as exc:
            self.stop_process(self.sl)
            self.buffer.cleanup()
            raise ValueError("FFmpeg could not start. Check the FFmpeg installation or configured path.") from exc
        finally:
            self.sl.stdout.close()
        log("recording", login)

    def alive(self): return self.sl.poll() is None and self.ff.poll() is None

    @staticmethod
    def stop_process(process):
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def stop(self):
        self.stop_process(self.sl)
        self.stop_process(self.ff)

    def segments(self):
        segments = []
        for file in self.dir.glob("seg*.ts"):
            try:
                stat = file.stat()
            except FileNotFoundError:
                continue
            if stat.st_size:
                segments.append((stat.st_mtime, file))
        return sorted(segments)

    def has_lead(self, moment, before):
        return self.started <= moment - before and any(
            end <= moment - before + SEG for end, _ in self.segments())

    def cut(self, t, out, before, after):
        """Join buffered segments around the moment and return the trim offset inside the joined file."""
        start = t - before
        segs = [(end, file) for end, file in self.segments() if start - SEG <= end <= t + after + SEG + 3]
        if not segs: return None
        first_start = segs[0][0] - SEG
        tmp = Path(out).with_suffix(".parts"); tmp.mkdir(exist_ok=True)
        lst = tmp / "list.txt"; lines = []
        for k, (_, f) in enumerate(segs):  # copy first: the ring buffer keeps overwriting
            dst = tmp / f"{k:03d}.ts"; shutil.copy(f, dst); lines.append(f"file '{dst.name}'")
        lst.write_text("\n".join(lines), encoding="utf-8")
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", str(Path(out).resolve())],
                       cwd=tmp, check=True, creationflags=NOWIN)  # stream copy only, no re-encode
        shutil.rmtree(tmp, ignore_errors=True)
        return max(0, start - first_start)

def kick_channel(slug):
    """kick.com channel JSON (public web endpoint): livestream info + chatroom id for the chat socket."""
    r = requests.get(f"https://kick.com/api/v2/channels/{slug}", timeout=15, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    r.raise_for_status(); return r.json()

class Live:
    def __init__(self):
        self.counts = defaultdict(float); self.kw = defaultdict(lambda: defaultdict(int))
        self.hist = defaultdict(lambda: deque(maxlen=BASE)); self.last = defaultdict(float)
        self.pending = {}; self.quiet = defaultdict(float)  # sustain candidates; raid/sub-alert mute until
        self.streams = {}; self.kick = {}; self.joined = set(); self.writer = None; self.rec = {}
        self.status = {"channels": 0, "kick": 0, "recording": 0, "spikes": 0, "last_spike": None}
        self.record_lock = threading.Lock()
        self.recording_closed = False
        self.armed = set()
        self.creator_buffers = set()
        self.creator_streams = {}
        self.manual = {}
        self.auto_lengths = cycle(CLIP_LENGTHS)
        self.manual_length_index = 0
        self.words, self.word_min = [], 5; self.wc = defaultdict(lambda: defaultdict(int))  # user chat-watch words
        self.wavg = defaultdict(lambda: defaultdict(float))  # per-word rolling average, so busy chats need a real burst

    def hit(self, ch, text):
        s, k = score(text); self.counts[ch] += s
        if k: self.kw[ch][k] += 1
        if self.words:
            t = text.lower(); toks = set(re.findall(r"[a-z0-9_']+", t))
            for w in self.words:
                if (" " in w and w in t) or w in toks: self.wc[ch][w] += 1

    async def irc(self):
        while True:
            try:
                r, w = await asyncio.open_connection("irc.chat.twitch.tv", 6697, ssl=True, ssl_handshake_timeout=15)
                self.writer = w; self.joined = set()
                w.write(f"CAP REQ :twitch.tv/tags twitch.tv/commands\r\nNICK justinfan{random.randint(10000, 99999)}\r\n".encode()); await w.drain()
                await self.sync_joins()
                while line := (await r.readline()).decode("utf-8", "ignore"):
                    if line.startswith("PING"): w.write(b"PONG :tmi.twitch.tv\r\n"); await w.drain(); continue
                    m = re.search(r" PRIVMSG #(\w+) :(.*)", line)
                    if m: self.hit(m[1], m[2]); continue
                    m = re.search(r"msg-id=(raid|subgift|submysterygift|sub|resub|announcement)\b.* USERNOTICE #(\w+)", line)
                    if m: self.quiet[m[2]] = time.time() + 45  # alert bursts have no on-screen moment
            except Exception as e: log("irc error", e)
            self.writer = None; await asyncio.sleep(10)

    async def kick_chat(self):
        """Kick chat via its Centrifugo realtime socket (guest token, no account). Reconnects every 40 min (token lives 45)."""
        import uuid
        try: import websockets
        except ImportError: log("kick chat off: run setup.bat again to install websockets"); return
        H = {"User-Agent": "Mozilla/5.0", "Origin": "https://kick.com", "Accept": "application/json"}
        while True:
            if not self.kick: await asyncio.sleep(30); continue
            try:
                cid = str(uuid.uuid4())
                url = (await asyncio.to_thread(lambda: requests.post("https://web.kick.com/api/v1/realtime/connection", headers=H, timeout=15, json={
                    "client": {"id": cid, "type": "web"}, "capabilities": {"accepted_providers": [{"provider": "centrifugo"}]}}).json()))["data"]["connections"][0]["credentials"]["url"]
                tok = (await asyncio.to_thread(lambda: requests.post("https://web.kick.com/api/v1/realtime/auth/connection", headers=H, timeout=15,
                                                                     json={"client_id": cid}).json()))["data"]["token"]
                async with websockets.connect(url, origin="https://kick.com", user_agent_header="Mozilla/5.0") as ws:
                    await ws.send(json.dumps({"id": 1, "connect": {"token": tok, "name": "js"}})); n, subbed, t0 = 2, {}, time.time()
                    while time.time() - t0 < 2400:
                        want = {f"chatrooms.{k['chatroom']}.v2": slug for slug, k in self.kick.items()}
                        for feed in set(want) - set(subbed):
                            await ws.send(json.dumps({"id": n, "subscribe": {"channel": feed}})); n += 1
                        subbed = want
                        try: raw = await asyncio.wait_for(ws.recv(), 30)
                        except asyncio.TimeoutError: continue
                        for line in raw.splitlines():
                            m = json.loads(line) if line.strip() else {}
                            if not m: await ws.send("{}"); continue  # centrifugo ping -> pong
                            p = m.get("push", {}); d = p.get("pub", {}).get("data", {})
                            if str(d.get("event", "")).endswith("ChatMessageEvent") and p.get("channel") in subbed:
                                body = json.loads(d["data"]) if isinstance(d.get("data"), str) else d.get("data", {})
                                self.hit(f"kick:{subbed[p['channel']]}", body.get("content", ""))
            except Exception as e: log("kick chat error", str(e)[:150]); await asyncio.sleep(30)

    async def sync_joins(self):
        if not self.writer: return
        want = set(self.streams)
        for ch in want - self.joined: self.writer.write(f"JOIN #{ch}\r\n".encode())
        for ch in self.joined - want: self.writer.write(f"PART #{ch}\r\n".encode())
        await self.writer.drain(); self.joined = want; self.status["channels"] = len(want)

    def sync_recorders(self, include):
        profile = creator_profile()
        enabled = {channel for channel, source in creator_channels().items() if source["enabled"]} if self.creator_buffers else set()
        with self.record_lock:
            if self.recording_closed:
                return
            live = set(self.streams) | {f"kick:{k}" for k in self.kick}
            for ch in list(self.rec):
                allowed = ch in enabled if ch in self.creator_buffers else creator_channel_allowed(ch, profile)
                if not allowed or (ch not in live and ch not in self.armed) or not self.rec[ch].alive():
                    self.rec.pop(ch).stop()
                    self.armed.discard(ch)
                    self.creator_buffers.discard(ch)
            for ch in [c for c in include if c in live and c not in self.rec][:max(0, RECORD_MAX - len(self.rec))]:
                try: self.rec[ch] = Recorder(ch, f"https://kick.com/{ch[5:]}" if ch.startswith("kick:") else None)
                except Exception as e: log("recorder failed", ch, e)
            self.status["recording"] = len(self.rec)

    def stream_info(self, ch, workspace="gta6"):
        if workspace == "creators":
            return self.creator_streams.get(ch, {})
        return (self.kick.get(ch[5:]) if ch.startswith("kick:") else self.streams.get(ch)) or {}

    def clip_moment(self, ch, clicked, length=0, workspace="gta6"):
        if length and length not in CLIP_LENGTHS:
            raise ValueError("Choose a clip length of 45, 60 or 75 seconds.")
        kick = ch.startswith("kick:")
        slug = ch.removeprefix("kick:")
        if workspace == "creators":
            if not creator_enabled(ch):
                raise ValueError("Enable this channel in Creator channels first.")
        elif workspace != "gta6" or not creator_channel_allowed(ch, creator_profile()):
            raise ValueError("This creator is outside your clip list.")
        excluded = {s.lower() for s in watchlist()["exclude"]}
        if slug in excluded or ch in excluded:
            raise ValueError("This streamer is excluded on the watchlist.")
        url = f"https://{'kick.com' if kick else 'twitch.tv'}/{slug}"
        with self.record_lock:
            if self.recording_closed:
                raise ValueError("The recorder is shutting down. Reopen Clip Desk and try again.")
            requested = length or CLIP_LENGTHS[self.manual_length_index]
            before = lead_seconds(requested)
            active = self.rec.get(ch)
            if self.manual.get(ch, (None,))[0] == "pending":
                return "A clip is already being captured for this streamer."
            if active and active.alive():
                if not active.has_lead(clicked, before):
                    return f"Keep buffering until {before} seconds of lead-in are available for a {requested}-second clip."
                info = self.stream_info(ch, workspace)
                item = dict(id=f"moment-{ch.replace(':', '_')}-{int(clicked * 1000)}", source="live-buffer", platform="Kick" if kick else "Twitch",
                            streamer=info.get("name" if kick else "user_name", slug), title=info.get("title", ""),
                            views=info.get("viewers" if kick else "viewer_count", 0), game=info.get("game" if kick else "game_name", ""),
                            workspace=workspace, channel=ch,
                            url=url, dur=requested, peak=before, spike={"at": datetime.fromtimestamp(clicked).isoformat(timespec="seconds")})
                self.manual[ch] = ("pending", None)
                threading.Thread(target=self.finish_moment, args=(ch, clicked, active, item), daemon=True).start()
                if not length:
                    self.manual_length_index = (self.manual_length_index + 1) % len(CLIP_LENGTHS)
                destination = "Creator clips" if workspace == "creators" else "Today"
                return f"Capturing a {requested}-second clip. The finished video will appear in {destination}."
            if active:
                self.rec.pop(ch).stop()
            if len(self.rec) >= RECORD_MAX:
                raise ValueError(f"All {RECORD_MAX} recording slots are in use. Try a stream already recording.")
            self.rec[ch] = Recorder(ch, url)
            self.armed.add(ch)
            if workspace == "creators":
                self.creator_buffers.add(ch)
            else:
                self.creator_buffers.discard(ch)
            self.status["recording"] = len(self.rec)
        return f"Buffer started for {slug}. Choose a length and clip once there is enough lead-in."

    def stop_creator_buffer(self, ch):
        with self.record_lock:
            if ch in self.creator_buffers:
                recorder = self.rec.pop(ch, None)
                self.creator_buffers.discard(ch)
                self.armed.discard(ch)
                if recorder:
                    recorder.stop()
                self.status["recording"] = len(self.rec)
            self.creator_streams.pop(ch, None)

    async def poll_creators(self):
        slots = asyncio.Semaphore(4)

        async def lookup(ch):
            async with slots:
                try:
                    if not ch.startswith("kick:"):
                        return ch, await asyncio.to_thread(twitch_live_nokey, ch)
                    slug = ch[5:]
                    data = await asyncio.to_thread(kick_channel, slug)
                    live = data.get("livestream")
                    if live:
                        categories = live.get("categories") or [{}]
                        return ch, {"name": data.get("user", {}).get("username", slug),
                                    "title": live.get("session_title", ""),
                                    "viewers": live.get("viewer_count", 0), "game": categories[0].get("name", "")}
                except (requests.RequestException, OSError, ValueError, subprocess.TimeoutExpired):
                    return ch, None
                return ch, None

        while True:
            excluded = set(watchlist().get("exclude", ()))
            channels = [ch for ch, row in creator_channels().items() if row["enabled"]
                        and ch not in excluded and ch.removeprefix("kick:") not in excluded]
            pairs = await asyncio.gather(*(lookup(ch) for ch in channels))
            self.creator_streams = {ch: stream for ch, stream in pairs if stream}
            await asyncio.sleep(60)

    def finish_moment(self, ch, clicked, recorder, item):
        out = recorder.dir.parent / f"{item['id']}.mp4"
        try:
            before, length = item["peak"], item["dur"]
            after = length - before
            time.sleep(max(0, clicked + after + SEG + 3 - time.time()))
            offset = recorder.cut(clicked, out, before, after) if self.rec.get(ch) in (None, recorder) else None
            clip_id = make_clip(dict(item, local=str(out), start=offset)) if offset is not None else None
            self.manual[ch] = ("done", clip_id) if clip_id else ("failed", None)
        except Exception as exc:
            self.manual[ch] = ("failed", None)
            log("manual moment failed", ch, exc)
        finally:
            out.unlink(missing_ok=True)

    async def poll_streams(self):
        while True:
            wl = watchlist(); excl = {s.lower() for s in wl["exclude"]}; profile = creator_profile()
            self.streams = {key: stream for key, stream in self.streams.items() if creator_channel_allowed(key, profile)}
            self.words = list(dict.fromkeys([w.lower() for w in wl.get("chat_words", []) if w.strip()][:50] + BASE_WORDS)); self.word_min = max(2, int(wl.get("chat_word_min", 4)))
            if not os.getenv("TWITCH_CLIENT_ID"):  # keyless mode: watchlist only, live check via Streamlink every ~2 min
                if int(time.time() / 60) % 2 == 0 or not self.streams:
                    found = await asyncio.gather(*[asyncio.to_thread(twitch_live_nokey, l) for l in wl["include"] if l not in excl], return_exceptions=True)
                    self.streams = {s["user_login"]: s for s in found if isinstance(s, dict) and re.search(r"grand theft auto|\bgta\b|gta ?(5|6|v|vi)\b", (s["game_name"]+" "+s["title"]).lower())}
                    await self.sync_joins()
            else:
              try:
                gids = list(game_ids())
                data = helix("streams", game_id=gids, first=wl.get("live_top_streams", 20), type="live")["data"] if profile is None and not WATCHLIST_ONLY else []
                if wl["include"]: data += helix("streams", user_login=wl["include"][:100], type="live")["data"]
                self.streams = {s["user_login"]: s for s in data if s["game_id"] in gids and s["user_login"] not in excl
                                and (not LANGS or s["language"] in LANGS or s["user_login"] in wl["include"])}
                await self.sync_joins()
              except Exception as e: log("stream poll error", e)
            kick = {}
            for slug in wl.get("kick_include", []):
                try:
                    j = await asyncio.to_thread(kick_channel, slug); ls = j.get("livestream")
                    if ls and any("grand theft auto" in (c.get("name") or "").lower() for c in ls.get("categories", [])):
                        kick[slug] = {"chatroom": j["chatroom"]["id"], "title": ls.get("session_title", ""), "viewers": ls.get("viewer_count", 0),
                                      "name": j.get("user", {}).get("username", slug), "game": ls["categories"][0]["name"]}
                except Exception as e: log("kick channel error", slug, str(e)[:80])
            self.kick = kick; self.status["kick"] = len(kick)
            self.sync_recorders(wl["include"] + [f"kick:{k}" for k in wl.get("kick_include", [])])  # record in list order, up to RECORD_MAX
            await asyncio.sleep(60)

    async def detect(self):
        while True:
            await asyncio.sleep(BUCKET); now = time.time()
            for ch in list(self.streams) + [f"kick:{k}" for k in self.kick]:
                n = self.counts.pop(ch, 0.0); h = self.hist[ch]; wc = self.wc.pop(ch, {})
                avg = self.wavg[ch]; burst = {w: c for w, c in wc.items() if c >= max(self.word_min, avg[w] * 2.5)}
                for w in set(avg) | set(wc): avg[w] = avg[w] * 0.85 + wc.get(w, 0) * 0.15
                top = max(burst.items(), key=lambda x: x[1], default=(None, 0))
                if top[1] and len(h) >= 6 and now - self.last[ch] > 90 and now > self.quiet[ch]:  # user word said N times in 10s
                    self.last[ch] = now; self.status["spikes"] += 1; self.pending.pop(ch, None)
                    self.status["last_spike"] = f"{ch} {datetime.now():%H:%M:%S} chat word '{top[0]}' x{top[1]}"
                    log("WORD", self.status["last_spike"]); kws = dict(self.kw[ch]); kws[top[0]] = top[1]
                    asyncio.create_task(self.capture(ch, now - BUCKET / 2, n, statistics.mean(h) if h else 0, kws))
                    h.append(n); self.kw[ch].clear(); continue
                if ch in self.pending:  # sustain: the next bucket must stay hot, or it was one emote wave
                    t, n0, mu, kws = self.pending.pop(ch)
                    if is_hot(n, h) and now > self.quiet[ch]:
                        self.last[ch] = now; self.status["spikes"] += 1
                        self.status["last_spike"] = f"{ch} {datetime.fromtimestamp(t):%H:%M:%S} score {n0:.0f} vs {mu:.1f}"
                        log("SPIKE", self.status["last_spike"], kws)
                        asyncio.create_task(self.capture(ch, t, n0, mu, kws))
                elif now - self.last[ch] > 90 and now > self.quiet[ch] and (is_spike(n, h) or (
                        now - self.last[ch] > DROUGHT and len(h) >= 6 and n >= max(6, statistics.mean(h) * 1.4))):
                    self.pending[ch] = (now - BUCKET / 2, n, statistics.mean(h), dict(self.kw[ch]))
                h.append(n); self.kw[ch].clear()

    async def capture(self, ch, t, n, mu, kws):
        import growth, research
        capture_id = f"{ch}:{int(t)}"
        length = growth.choose_length(next(self.auto_lengths), capture_id)
        recorder = self.rec.get(ch)
        before = AUTO_CLIP_LEAD
        if recorder and recorder.started <= t:
            before = research.opening_lead(capture_id, before)
            before = min(before, int(t - recorder.started))
            wait = max(0, t + length - before + SEG + 3 - time.time())
        else:
            recorder, wait = None, 90  # give Twitch time to publish an available highlight or VOD segment
        await asyncio.sleep(wait)
        await asyncio.to_thread(self._capture, ch, t, n, mu, kws, length, before, recorder)

    def _capture(self, ch, t, n, mu, kws, length, before, recorder):
        if not creator_channel_allowed(ch, creator_profile()):
            return
        kick = ch.startswith("kick:")
        s = self.kick.get(ch[5:]) if kick else self.streams.get(ch)
        if not s: return
        name = s["name"] if kick else s["user_name"]
        if used_today(db()).get(name.lower(), 0) >= MAX_PER: log("live: daily cap for", ch); return
        spike = {"score": round(n), "baseline": round(mu, 1), "keywords": kws, "at": datetime.fromtimestamp(t).isoformat(timespec="seconds")}
        base = dict(source="live-spike", streamer=name, platform="Kick" if kick else "Twitch", spike=spike,
                    game=s["game"] if kick else s["game_name"], title=s["title"], views=s["viewers"] if kick else s["viewer_count"],
                    url=f"https://kick.com/{ch[5:]}" if kick else f"https://twitch.tv/{ch}")
        try:
            if recorder and self.rec.get(ch) in (None, recorder):
                out = DATA / "buffer" / f"{ch.replace(':', '_')}-{int(t)}.mp4"
                try:
                    off = recorder.cut(t, out, before, length - before)
                    if off is not None:
                        opening = {4: "tight", AUTO_CLIP_LEAD: "standard"}.get(before, "")
                        clip_id = make_clip(dict(base, id=f"live-{ch.replace(':', '_')}-{int(t)}", source="live-buffer",
                                                 local=str(out), start=off, dur=length, peak=before,
                                                 spike=spike | {"opening_test": opening}))
                        if clip_id:
                            return clip_id
                except (OSError, subprocess.CalledProcessError) as e:
                    log("live buffer unavailable", ch, type(e).__name__)
                finally:
                    out.unlink(missing_ok=True)
            if kick or not s.get("user_id"): log("live: spike but not recording (add to watchlist top / raise RECORD_MAX)", ch); return
            st = datetime.fromtimestamp(t - 120, timezone.utc)
            clips = [x for x in helix("clips", broadcaster_id=s["user_id"], started_at=iso(st), ended_at=iso(datetime.now(timezone.utc)), first=20)["data"] if x["duration"] >= 7]
            if clips:
                x = max(clips, key=lambda x: x["view_count"])
                return make_twitch_clip(x, dict(base, id=f"clip-{x['id']}", title=x["title"], views=x["view_count"], url=x["url"]), length)
            v = helix("videos", user_id=s["user_id"], type="archive", first=1)["data"]
            if v and v[0].get("stream_id") == s["id"]:
                off = max(0, int(t - datetime.fromisoformat(v[0]["created_at"].replace("Z", "+00:00")).timestamp()))
                b, e = vod_window(off, length)
                return make_clip(dict(base, id=vod_clip_id(v[0]["id"], off), url=f"https://www.twitch.tv/videos/{v[0]['id']}?t={b}s",
                                      vod_id=v[0]["id"], begin=b, end=e, dur=length, peak=off - b))
            log("live: no buffer, clip or VOD for spike", ch)
        except Exception as e: log("live capture failed", ch, e)

    async def run(self):
        try:
            await asyncio.gather(self.poll_streams(), self.poll_creators(), self.detect(),
                                 self.kick_chat(), self.irc(), return_exceptions=True)
        finally:
            self.close_recorders()

    def close_recorders(self):
        with self.record_lock:
            self.recording_closed = True
            for recorder in self.rec.values():
                recorder.stop()
            self.rec.clear()
            self.armed.clear()
            self.creator_buffers.clear()
            self.status["recording"] = 0

LIVE = Live()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    if cmd == "live":
        import posts
        threading.Thread(target=posts.run, args=(threading.Event(),), daemon=True).start()
    if cmd == "backfill": backfill()
    elif cmd == "live": asyncio.run(LIVE.run())
    elif cmd == "vod": vod(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 5)
    elif cmd == "test-clip": make_clip(dict(id=f"clip-{sys.argv[2]}", source="manual", streamer=sys.argv[3], title=sys.argv[4],
                                            views=0, url=f"https://clips.twitch.tv/{sys.argv[2]}", clip_id=sys.argv[2]))
    elif cmd == "test-local": make_clip(dict(id=f"local-{int(time.time())}", source="manual", streamer=sys.argv[3], title=sys.argv[4],
                                             views=0, url="", local=sys.argv[2], start=float(sys.argv[5]) if len(sys.argv) > 5 else 0,
                                             dur=int(sys.argv[6]) if len(sys.argv) > 6 else DEFAULT_CLIP_LENGTH))
    elif cmd == "nightly": import writer; writer.nightly()
    if cmd in ("backfill", "vod", "test-clip", "test-local", "drafts"):
        import posts
        while posts.process_next():
            pass

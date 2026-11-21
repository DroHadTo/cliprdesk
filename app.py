"""Local clip desk. Run start.bat to open Today on the configured port."""
import asyncio, re, csv, hashlib, hmac, html, io, json, os, secrets, socket, threading, time
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
import shutil
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlencode, urlsplit
import requests, uvicorn
import secret_store
import network_policy
import creator_rights
from fastapi import FastAPI, Form, Query, UploadFile, File, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from starlette.background import BackgroundTask
import cliptool as ct
import posts
import autopost
import growth
import media_tools
import quality
import research
import virality
import earnings_worker
import whop_api

@asynccontextmanager
async def lifespan(_):
    stop = threading.Event()
    start()
    threading.Thread(target=autopost.run, args=(stop,), daemon=True).start()
    threading.Thread(target=growth.run, args=(stop,), daemon=True).start()
    threading.Thread(target=quality.run, args=(stop,), daemon=True).start()
    threading.Thread(target=earnings_worker.run, args=(stop,), daemon=True).start()
    try:
        yield
    finally:
        stop.set()
        ct.LIVE.close_recorders()
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CSRF = secrets.token_urlsafe(32)
LOCAL_HOSTS = {f"127.0.0.1:{ct.PORT}", f"localhost:{ct.PORT}"}
WORKER_HEADER = "X-GTA6-Clipper"
UI_ASSETS = ct.ROOT / "web" / "static"
VOD_SLOT = threading.BoundedSemaphore(1)


@app.middleware("http")
async def guard(req: Request, call_next):
    host = req.headers.get("host", "")
    if host not in LOCAL_HOSTS:
        return HTMLResponse("Forbidden host", status_code=403)
    if req.method not in {"GET", "HEAD", "OPTIONS"}:
        if req.headers.get("origin") != f"http://{host}":
            return HTMLResponse("Bad origin", status_code=403)
        token = req.cookies.get("csrf", "")
        if not token.isascii() or not hmac.compare_digest(token, CSRF):
            return HTMLResponse("Bad CSRF token. Reload the page.", status_code=403)
    resp = await call_next(req)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.set_cookie("csrf", CSRF, httponly=True, samesite="strict")
    resp.headers.update({
        "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin", "Cross-Origin-Resource-Policy": "same-origin",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
        f"base-uri 'none'; frame-ancestors 'none'; form-action 'self' http://localhost:{ct.PORT} https://whop.com https://api.whop.com",
        WORKER_HEADER: "local",
    })
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.get("/pair")
@app.get("/login")
def old_login():
    return RedirectResponse(ct.TODAY_PATH, 303)


@app.api_route("/clipdesk.css", methods=["GET", "HEAD"], include_in_schema=False)
def theme():
    return FileResponse(UI_ASSETS / "clipdesk.css", media_type="text/css",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.api_route("/desk.js", methods=["GET", "HEAD"], include_in_schema=False)
def desk_script():
    return FileResponse(UI_ASSETS / "desk.js", media_type="text/javascript",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.get("/healthz")
def health():
    with closing(ct.db()) as conn:
        conn.execute("SELECT 1 FROM clips LIMIT 1").fetchone()
    return {"ok": True, "service": "clip-desk-worker"}


E = lambda s: html.escape(str(s if s is not None else ""))


def safe_link(value):
    text = str(value or "")
    if any(char.isspace() or ord(char) < 32 or char == "\\" for char in text):
        return "#"
    try:
        url = urlsplit(text)
        if url.scheme in {"https", "http"} and url.hostname and "@" not in url.netloc and url.port != 0:
            return E(text)
    except ValueError:
        pass
    return "#"


def latest_list(rows, *, published=False):
    items = []
    for row in rows:
        title = row["hook"] or row["title"] or row["streamer"] or "Highlight"
        when = row["posted"] if published else row["created"]
        try:
            moment = datetime.fromisoformat(when or "")
        except ValueError:
            timestamp = ""
        else:
            local = moment.astimezone(posts.SAST) if moment.tzinfo else moment
            timestamp = f"<time datetime='{E(when)}'>{local.strftime('%d %b · %H:%M')}</time>"
        details = " · ".join(part for part in (E(row["streamer"]), timestamp) if part)
        review_url = f"/review/{quote(row['id'], safe='')}#tiktok"
        preview = ""
        if row["file"] and finished_file(row["file"]):
            media = quote(row["file"])
            preview = f"<video src='/media/{media}' controls playsinline preload=none aria-label='{E(title)}'></video>"
        tiktok_url = row["tiktok_url"] or ""
        tiktok = (f"<a class=latest-tiktok href='{safe_link(tiktok_url)}' target=_blank rel=noopener>View on TikTok</a>"
                  if published and tiktok_url.startswith("https://") else "")
        items.append(f"<li class=latest-item>{preview}<a href='{review_url}'>{E(title)}</a>"
                     f"<small>{details}</small>{tiktok}</li>")
    if items:
        return f"<ul class=latest-items>{''.join(items)}</ul>"
    return "<small>No TikTok posts yet.</small>" if published else "<small>No finished videos yet.</small>"


def latest_activity(workspace="gta6"):
    allowed, params = (ct.CREATOR_LIBRARY, ()) if workspace == "creators" else ct.creator_filter(ct.creator_profile())
    clips = f"""SELECT c.id,c.file,c.streamer,c.hook,c.title,c.created,c.posted,c.tiktok_url FROM clips c
                WHERE {allowed} AND NOT {ct.TAKEN_DOWN}"""
    with closing(ct.db()) as conn:
        videos = conn.execute(clips + " AND c.file<>'' ORDER BY c.created DESC, c.rowid DESC LIMIT 3", params).fetchall()
        sent = conn.execute(clips + " AND c.posted<>'' ORDER BY datetime(c.posted) DESC, c.rowid DESC LIMIT 3", params).fetchall()
    return (f"<div class=latest-groups><div class='panel latest-group'><h3>Latest videos</h3>{latest_list(videos)}</div>"
            f"<div class='panel latest-group'><h3>Latest TikTok posts</h3>{latest_list(sent, published=True)}</div></div>")


@app.get("/latest-activity")
def latest_activity_fragment(workspace: str = "gta6"):
    return HTMLResponse(latest_activity(workspace))


def page(title, body, req=None, *, workspace="gta6"):
    st = ct.LIVE.status
    if workspace == "creators":
        channels = tuple(ct.LIVE.creator_streams)
        kick = sum(channel.startswith("kick:") for channel in channels)
        st = {"channels": len(channels) - kick, "kick": kick, "recording": len(ct.LIVE.creator_buffers)}
    cur = req.url.path if req else ""
    groups = (
        ("Creators", (("/creators", "Creator channels"), ("/creators/library", "Creator clips"))),
        ("GTA 6", ((ct.TODAY_PATH, "Today"), ("/posts", "TikTok"), ("/monitor", "Post Monitor"), ("/live", "Live now"),
                   ("/all", "All clips"), ("/score", "Scoreboard"), ("/earnings", "Earnings"),
                   ("/watchlist", "Watchlist"), ("/learning", "Learning"))),
        ("Shared settings", (("/rights", "Creator rights"), ("/takedowns", "Takedowns"), ("/setup", "Setup"))),
    )
    nav = "".join(
        f"<div class=nav-group><span class=nav-label>{name}</span>" + "".join(
            f'<a href="{url}"' + (' aria-current="page"' if cur == url else '') + f'>{label}</a>'
            for url, label in links
        ) + "</div>"
        for name, links in groups
    )
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{E(title)} &middot; Clip Desk</title><link rel=stylesheet href=/clipdesk.css?v=3>"
        "<script defer src=/desk.js?v=2></script></head>"
        "<body><a class=skip-link href=#content>Skip to content</a><div class=app-shell>"
        "<aside class=sidebar><a class=brand href=/creators><span class=brand-mark aria-hidden=true></span>Clip Desk</a>"
        f"<nav aria-label='Workspace'>{nav}</nav>"
        "<div class=sidebar-credit><small>Created by <a href='https://x.com/DroMeta' target=_blank rel=noopener>DroMeta</a></small></div>"
        "</aside><div class=workspace>"
        f"<header class=windowbar><div><small class=workspace-kicker>{'Creators' if workspace == 'creators' else 'GTA 6'} workspace</small>"
        f"<h1 class=workspace-heading>{E(title)}</h1></div><div class=stat>"
        f"<span class=pill><span class=dot></span>{st['channels']} Twitch &middot; {st['kick']} Kick</span>"
        f"<span class=pill>{st['recording']} recording</span></div></header>"
        f"<main id=content><section class=latest aria-labelledby=latest-title><h2 id=latest-title>Latest videos and posts</h2>"
        f"<div id=latest-activity data-workspace={workspace}>{latest_activity(workspace)}</div></section>{body}</main></div></div></body></html>"
    )


POST_STATUS = {"uploading": "Preparing automatic upload", "submitting": "Checking Buffer submission",
               "buffer": "With Buffer", "confirmation": "Check Buffer", "failed": "Automatic upload needs a review",
               "review": "Waiting for quality check"}
QUALITY_STATUS = {"checking": "Checking quality", "waiting": "Waiting for quality check", "held": "Held for review"}
CLIPS_WITH_DRAFTS = """SELECT c.*, p.reserved_day, u.state AS upload_state,
                            u.detail AS upload_detail,q.state AS quality_state,q.detail AS quality_detail,
                            v.score AS viral_score,v.state AS viral_state
                            FROM clips c LEFT JOIN post_drafts p ON p.clip_id=c.id
                            LEFT JOIN tiktok_uploads u ON u.clip_id=c.id LEFT JOIN clip_quality q ON q.clip_id=c.id
                            LEFT JOIN clip_virality v ON v.clip_id=c.id"""
CLIPS_PER_PAGE = 24
TIKTOK_STUDIO = "https://www.tiktok.com/tiktokstudio/upload"
BUFFER_CHANNELS = "https://publish.buffer.com/settings/channels"
BUFFER_KEYS = "https://publish.buffer.com/settings/api"
R2_SETUP = "https://developers.cloudflare.com/r2/get-started/"


def automation_enabled(settings):
    return bool(settings and settings["enabled"] and autopost.configured() and autopost.delivery_base())


def active_upload(clip):
    return clip["upload_state"] in ("uploading", "submitting", "buffer", "confirmation")


def quality_status(clip, fallback):
    if clip["upload_state"] not in (None, "review"):
        return POST_STATUS.get(clip["upload_state"], fallback), clip["upload_detail"] or ""
    if clip["quality_state"] == "approved":
        if clip["viral_state"] == "fallback":
            return "Eligible for quiet-hour posting", clip["quality_detail"] or ""
        if clip["viral_state"] == "verified":
            label = "Eligible for experiment" if clip["viral_score"] < virality.MIN_PUBLISH_SCORE else "Approved for auto-posting"
            return label, clip["quality_detail"] or ""
        return "Waiting for virality review", ""
    return QUALITY_STATUS.get(clip["quality_state"], fallback), clip["quality_detail"] or ""


def virality_label(clip):
    state = clip["viral_state"]
    if state == "unavailable":
        return "Virality score unavailable · MP4 missing"
    if clip["viral_score"] is None:
        return "Virality score pending" if clip["file"] else ""
    label = {"verified": "Verified", "fallback": "Quiet-hour fallback", "held": "Held", "preliminary": "Preliminary"}[state]
    return f"Virality {clip['viral_score']}/100 · {label}"


def upload_action(clip, day, available=True):
    cid = E(clip["id"])
    review_url = f"/review/{cid}#tiktok"
    if clip["posted"]:
        return f"<a href='{review_url}'>View post</a>"
    if clip["reserved_day"] == day:
        return f"<a class=upload-link href='{review_url}'>Continue TikTok upload</a>"
    if clip["file"] and not active_upload(clip) and available:
        return (f"<form method=post action='/post/{cid}/reserve' data-busy='Preparing video…'>"
                "<button>Post to TikTok</button></form>"
                f"<a href='{review_url}'>Review clip</a>")
    return f"<a href='{review_url}'>Review clip</a>"


def card(r, campaigns=()):
    status = f"Posted {r['posted']}" if r["posted"] else quality_status(r, "Ready to review")[0]
    matches = [c["name"] for c in campaigns if r["streamer"].lower() in f"{c['name']} {c['notes'] or ''}".lower()]
    return (f"<div class=card><video src='/media/{quote(r['file'] or '')}' controls playsinline preload=none "
            f"aria-label='{E(r['hook'] or r['title'] or 'Clip preview')}'></video>"
            + (f"<span class='tag bad'>GENERIC? FAILS SWAP TEST</span>" if r["swap"] == "generic" else "<span class='tag ok'>STREAMER REACTION</span>" if r["swap"] else "") +
            f"<h3>{E(r['hook'])}</h3><small>{E(r['streamer'])} · {E(r['game'])} · {E(r['views'])} views · {E(r['source'])} · <a href='{safe_link(r['url'])}' target=_blank>source</a></small>"
            + (f"<span class='tag bad'>REVIEW: {E(r['flags'])}</span><br>" if r["flags"] else "") +
            f"<div class=post-actions>{upload_action(r, posts.posting_day())}</div><small>{E(status)}</small>"
            f"<p><small>{E(virality_label(r))}</small></p>"
            + (f"<p class=ok>Campaign match: {E(', '.join(matches))}</p>" if matches else "") + "</div>")


def creator_tabs(conn, profile, path, selected):
    allowed, params = ct.creator_filter(profile)
    recent = conn.execute(f"""SELECT c.streamer,max(c.created) AS latest FROM clips c
                             WHERE {allowed} AND c.file<>'' AND c.streamer<>''
                             AND NOT EXISTS (SELECT 1 FROM takedowns t WHERE t.clip_id=c.id)
                             GROUP BY lower(trim(c.streamer)) ORDER BY latest DESC""", params).fetchall()
    aliases = ct.creator_names(profile)
    labels = {}
    for row in recent:
        name = row["streamer"]
        key = aliases.get(name.strip().lower(), name.strip().lower())
        labels.setdefault(key, profile[key]["name"] if profile is not None else name)
    if profile is not None:
        for key, creator in profile.items():
            labels.setdefault(key, creator["name"])
    links = []
    for key, label in [("", "All creators"), *labels.items()]:
        active = " aria-current=page" if key == selected else ""
        url = path + ("?" + urlencode({"creator": key}) if key else "")
        links.append(f"<a href='{E(url)}'{active}>{E(label)}</a>")
    refresh = "/creator-tabs?" + urlencode({"view": path, "creator": selected})
    return f"<nav class=creator-tabs aria-label='Filter clips by creator' data-refresh='{E(refresh)}'>{''.join(links)}</nav>"


@app.get("/creator-tabs")
def refresh_creator_tabs(view: str, creator: str = ""):
    if view not in (ct.TODAY_PATH, "/all", "/posts"):
        return HTMLResponse(status_code=404)
    with closing(ct.db()) as conn:
        return HTMLResponse(creator_tabs(conn, ct.creator_profile(), view, creator))


def clip_page(conn, filters, params, number):
    rows = conn.execute(
        CLIPS_WITH_DRAFTS + f" WHERE {filters} AND NOT {ct.TAKEN_DOWN}"
        " ORDER BY c.created DESC, c.rowid DESC LIMIT ? OFFSET ?",
        (*params, CLIPS_PER_PAGE + 1, (number - 1) * CLIPS_PER_PAGE),
    ).fetchall()
    return rows[:CLIPS_PER_PAGE], len(rows) > CLIPS_PER_PAGE


def clip_pager(path, number, has_next, creator=""):
    def page_link(target, label):
        url = path + "?" + urlencode({"page": target, **({"creator": creator} if creator else {})})
        return f'<a class="btn secondary" href="{E(url)}">{label}</a>'

    previous = page_link(number - 1, "Newer clips") if number > 1 else ""
    following = page_link(number + 1, "Older clips") if has_next else ""
    if not previous and not following:
        return ""
    return (f"<nav class=pagination aria-label='Clip pages'><small>Page {number}</small>"
            f"<div class=actions>{previous}{following}</div></nav>")


@app.get("/")
@app.get("/gta6")
def home():
    return RedirectResponse(ct.TODAY_PATH, 307)


@app.get(ct.TODAY_PATH)
def today(request: Request, creator: str = "", number: int = Query(1, alias="page", ge=1, le=100000)):
    profile = ct.creator_profile()
    allowed, params = ct.creator_filter(profile, creator)
    filters, params = f"c.day=? AND {allowed}", (ct.today(), *params)
    with closing(ct.db()) as c:
        tabs = creator_tabs(c, profile, ct.TODAY_PATH, creator)
        rows, has_next = clip_page(c, filters, params, number)
        count, n = c.execute(
            f"SELECT count(*),count(DISTINCT lower(c.streamer)) FROM clips c WHERE {filters} AND NOT {ct.TAKEN_DOWN}",
            params,
        ).fetchone()
        camps = c.execute("select * from campaigns where active=1").fetchall()
        posted, reserved = posts.budget(c, posts.posting_day())
    top = (f"<div class=hero><div class=panel><div class=big>{count}</div><small>clips today</small></div><div class=panel><div class=big>{n}/{ct.TARGET}</div><small>streamers (daily goal)</small></div>"
           f"<div class=panel><div class=big>{posted}/{posts.DAILY_LIMIT}</div><small>posted · {reserved} reserved</small><br><a href=/posts>TikTok queue</a></div><div><h2>Today, {ct.today()}</h2></div></div>")
    top += ("<form method=post action=/run style=display:inline><button>Run backfill now</button></form> "
            "<form method=post action=/vod style=display:inline><input name=vod placeholder='Twitch VOD link' size=34><input name=n value=5 size=2><button>Clip VOD chat spikes</button></form>")
    if not os.getenv("TWITCH_CLIENT_ID"): top += "<p><small>Keyless mode: watching your Twitch + Kick watchlist live. Add Twitch keys only for the full GTA live list and the daily top-clips backfill.</small></p>"
    listing = "<div class=grid>" + "".join(card(r, camps) for r in rows) + "</div>" if rows else "<p>No clips today for this selection.</p>"
    return page("Today", top + tabs + listing + clip_pager(ct.TODAY_PATH, number, has_next, creator), request)

@app.get("/all")
def all_clips(request: Request, creator: str = "", number: int = Query(1, alias="page", ge=1, le=100000)):
    profile = ct.creator_profile()
    allowed, params = ct.creator_filter(profile, creator)
    with closing(ct.db()) as c:
        tabs = creator_tabs(c, profile, "/all", creator)
        rows, has_next = clip_page(c, allowed, params, number)
    listing = "<div class=grid>" + "".join(card(r) for r in rows) + "</div>" if rows else "<p>No finished clips for this selection.</p>"
    return page("All clips", tabs + listing + clip_pager("/all", number, has_next, creator), request)


@app.get("/posts")
def post_queue(request: Request, creator: str = "", number: int = Query(1, alias="page", ge=1, le=100000)):
    day = posts.posting_day()
    settings = autopost.get_settings()
    profile = ct.creator_profile()
    allowed, params = ct.creator_filter(profile, creator)
    with closing(ct.db()) as c:
        posted, reserved = posts.budget(c, day)
        hourly, experiments = posts.recent_post_modes(c)
        tabs = creator_tabs(c, profile, "/posts", creator)
        rows, has_next = clip_page(c, f"c.posted IS NULL AND c.file<>'' AND {allowed}", params, number)
    remaining = max(0, posts.DAILY_LIMIT - posted - reserved)
    publishing = automation_enabled(settings)
    items = []
    for r in rows:
        queued = publishing and not r["upload_state"] and r["reserved_day"] != day
        status, detail = quality_status(r, "Waiting for quality check" if queued else "Ready to post")
        items.append(f"<li class=panel><h3>{E(r['hook'])} · {E(r['streamer'])}</h3>"
                     f"<small>{E(status)} {E(detail)}</small>"
                     f"<p><small>{E(virality_label(r))}</small></p>"
                     f"<div class=post-actions>{upload_action(r, day, bool(remaining) and hourly < posts.HOURLY_LIMIT)}</div></li>")
    listing = f"<ul class=post-queue>{''.join(items)}</ul>" if items else "<p>Finished videos will appear here.</p>"
    status = f"Automatic posting on for {E(settings['account_name'])}" if publishing else "Automatic posting off"
    return page("TikTok", f"<h2>TikTok queue</h2><p class=panel>{posted} posted · {reserved} reserved · "
                f"{remaining} of {posts.DAILY_LIMIT} slots available today (SAST) · "
                f"{hourly}/{posts.HOURLY_LIMIT} regular and {experiments}/{posts.EXPERIMENT_HOURLY_LIMIT} experimental posts in the last hour.</p>"
                f"<p>{status} · <a href=/autopost>Automatic posting settings</a></p>"
                f"{tabs}{listing}{clip_pager('/posts', number, has_next, creator)}", request)


def autopost_redirect(**message):
    return RedirectResponse(f"/autopost?{urlencode(message)}", 303)


@app.get("/autopost")
def autopost_page(request: Request, connect: bool = False, notice: str = "", error: str = ""):
    settings = autopost.get_settings()
    credentials_saved = autopost.configured()
    enabled = automation_enabled(settings)
    delivery_saved = bool(autopost.delivery_base())
    status = (f"On for {E(settings['account_name'])}" if enabled else
              "Waiting for the public R2 delivery URL" if credentials_saved and not delivery_saved else
              f"Paused for {E(settings['account_name'])}" if settings else
              "Credentials saved · Ready to connect" if credentials_saved else "Waiting for credentials")
    controls = ""
    if enabled:
        controls = ("<form method=post action=/autopost/pause><button class=ghost>Pause automatic posting</button></form>"
                    "<p>Pausing stops new submissions. Buffer can still finish sending videos it already received.</p>")
    elif credentials_saved and delivery_saved:
        controls = "<p><a class=upload-link href='/autopost?connect=1'>Find my TikTok channel</a></p>"
    if connect and credentials_saved and delivery_saved and not enabled:
        try:
            channels = autopost.channels()
            if channels:
                options = "".join(f"<option value='{E(ch['id'])}'>{E(ch['name'])}</option>" for ch in channels)
                controls = ("<form method=post action=/autopost/enable class=post-actions data-busy='Checking connection…'>"
                            f"<label>TikTok channel <select name=channel_id required>{options}</select></label>"
                            "<button>Enable automatic posting</button></form>")
            else:
                error = "No connected TikTok channel found. Link your account in Buffer, then check again."
        except autopost.CannotPublish as exc:
            error = str(exc)
        except autopost.RateLimited:
            error = "Buffer is limiting API requests. Try again later."
        except (requests.RequestException, ValueError, KeyError, TypeError):
            error = "Couldn't check Buffer. Check the API key, permissions and internet connection."
    getting_started = (f"<ol><li>Sign in to Buffer and <a href='{BUFFER_CHANNELS}' target=_blank rel=noopener>connect your TikTok channel</a>. "
                       f"Then <a href='{BUFFER_KEYS}' target=_blank rel=noopener>create a Buffer API key</a> with posting permissions.</li>"
                       f"<li><a href='{R2_SETUP}' target=_blank rel=noopener>Create a dedicated Cloudflare R2 bucket</a> "
                       "with a public delivery URL and a bucket-scoped Object Read &amp; Write key.</li>"
                       "<li><a href='/setup#autopost'>Save the Buffer and R2 credentials and delivery URL on this PC</a>, then find your TikTok channel here.</li></ol>")
    feedback = f"<p role=alert>{E(error)}</p>" if error else f"<p role=status>{E(notice)}</p>" if notice else ""
    feedback += "<p>Automatic posting holds clips until you record <a href=/rights>creator permissions</a>.</p>"
    allowed, params = ct.creator_filter(ct.creator_profile())
    with closing(ct.db()) as conn:
        hourly, experiments = posts.recent_post_modes(conn)
        api_used, storage_used = autopost.quota_usage(conn)
        eligibility = conn.execute(f"""SELECT count(*) AS library,
            count(*) FILTER (WHERE {creator_rights.permitted()}) AS cleared,
            count(*) FILTER (WHERE q.state='approved' AND v.state='verified' AND k.caption<>''
                AND v.score>=? AND {creator_rights.permitted()}) AS cleared_verified,
            count(*) FILTER (WHERE q.state='approved' AND v.state='fallback' AND k.caption<>''
                AND {creator_rights.permitted()}) AS fallback,
            count(*) FILTER (WHERE q.state='held' OR v.state='held') AS held
            FROM clips c
            LEFT JOIN clip_quality q ON q.clip_id=c.id
            LEFT JOIN clip_virality v ON v.clip_id=c.id AND v.stamp=q.stamp
            LEFT JOIN clip_publish_captions k ON k.clip_id=c.id AND k.stamp=q.stamp
            WHERE c.posted IS NULL AND c.file<>'' AND {allowed} AND NOT {ct.TAKEN_DOWN}""",
            (virality.MIN_EXPERIMENT_SCORE, *params)).fetchone()
        last = conn.execute("""SELECT max(c.posted) FROM clips c JOIN tiktok_uploads u ON u.clip_id=c.id
            WHERE u.state='sent' AND u.channel=?""", (settings["channel"] if settings else "",)).fetchone()[0]
        jobs = conn.execute(f"""SELECT u.clip_id,u.state,u.detail,c.hook,c.streamer,t.mode,t.score FROM tiktok_uploads u
                                JOIN clips c ON c.id=u.clip_id
                                LEFT JOIN tiktok_post_trials t ON t.clip_id=u.clip_id AND t.channel=u.channel
                                WHERE {allowed} ORDER BY u.state IN ({posts.ACTIVE_UPLOADS}) DESC,
                                u.created DESC LIMIT 10""", params).fetchall()
    recent = []
    for job in jobs:
        cid = E(job["clip_id"])
        resolve = ""
        if job["state"] == "confirmation":
            resolve = (f"<div class=post-actions><form method=post action='/autopost/resolve/{cid}'>"
                       "<button class=ghost>Recheck Buffer</button></form>"
                       f"<form method=post action='/autopost/resolve/{cid}'>"
                       "<label><input type=checkbox name=confirm_absent value=true required> I checked Buffer and TikTok and cleared any pending post for this video.</label> "
                       "<button>Release for manual upload</button></form></div>")
        mode_label = {"experiment": "Experiment", "fallback": "Quiet-hour fallback"}.get(job["mode"])
        label = f" · {mode_label} {job['score']}/100" if mode_label else ""
        recent.append(f"<li class=panel><a href='/review/{cid}#tiktok'>{E(job['hook'])} · {E(job['streamer'])}{label}</a>"
                      f"<p>{E(POST_STATUS.get(job['state'], 'Posted'))} {E(job['detail'])}</p>{resolve}</li>")
    activity = f"<ul class=post-queue>{''.join(recent)}</ul>" if recent else "<p>No automatic uploads yet.</p>"
    model = os.getenv("OLLAMA_QUALITY_MODEL", "").strip() or posts.vision_model()
    if enabled and (not posts.ollama_url() or not model):
        feedback += "<p role=status>Save an Umbrel Ollama address and vision model in Setup to review clips.</p>"
    if enabled and eligibility["library"] and not eligibility["cleared"]:
        feedback += ("<p role=status>Auto posting is on. No unposted clips have a publishing grant. "
                     "<a href=/rights>Record permission</a> for footage you can publish.</p>")
    elif enabled and not (eligibility["cleared_verified"] or eligibility["fallback"]):
        feedback += ("<p role=status>No clips with a publishing grant have passed the video and caption checks yet. "
                     "<a href=/posts>Review the TikTok queue</a>.</p>")
    pipeline = (f"<dl class=queue-metrics><div><dt>In library</dt><dd>{eligibility['library']}</dd></div>"
                f"<div><dt>With permission</dt><dd>{eligibility['cleared']}</dd></div>"
                f"<div><dt>Priority ready</dt><dd>{eligibility['cleared_verified']}</dd></div>"
                f"<div><dt>Quiet-hour ready</dt><dd>{eligibility['fallback']}</dd></div></dl>"
                f"<p>{eligibility['held']} held by review.</p>"
                f"<p>Last confirmed automatic post {E(last or 'awaiting first post')}.</p>")
    if settings:
        controls += "<form method=post action=/autopost/verify><button class=ghost>Verify linked TikTok channel</button></form>"
    return page("Automatic TikTok posting", f"<section class=panel><h2 class=section-title>{status}</h2>"
                f"{feedback}{pipeline}<p>Every automatic clip needs a verified opening, payoff, visible facecam and original caption.</p>"
                f"<p>The publisher ranks verified clips across your full library. Regular posts need {virality.MIN_PUBLISH_SCORE}/100. "
                f"Experiments test scores of {virality.MIN_EXPERIMENT_SCORE}–{virality.MIN_PUBLISH_SCORE - 1}. "
                "If the past 60 minutes are empty and those queues have no eligible clip, the highest-scoring fallback can use a regular slot.</p>"
                f"<p>{hourly}/{posts.HOURLY_LIMIT} regular slots and {experiments}/{posts.EXPERIMENT_HOURLY_LIMIT} experimental slots used in the last hour "
                f"· up to {posts.DAILY_LIMIT} total per SAST day.</p>"
                f"<p>30-day clipper usage · {api_used:,}/{autopost.BUFFER_REQUEST_LIMIT:,} Buffer requests · "
                f"{storage_used / 1e9:.2f}/{autopost.R2_MONTH_BYTES / 1e9:.0f} GB staged in R2.</p>"
                "<p>Umbrel writes a scene-specific caption and checks it against recent posts. Every automatic upload includes two relevant hashtags.</p>"
                f"{controls}{getting_started if not enabled else ''}</section>"
                f"<div class=post-actions><h2>Recent uploads</h2><a href=/autopost>Refresh</a>"
                f"<a href=/posts>TikTok queue</a></div>{activity}", request)


@app.post("/autopost/enable")
def autopost_enable(channel_id: str = Form(max_length=160)):
    try:
        autopost.enable(channel_id)
    except autopost.CannotPublish as exc:
        return autopost_redirect(error=str(exc))
    except autopost.RateLimited:
        return autopost_redirect(error="Buffer is limiting API requests. Try again later.")
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return autopost_redirect(error="Couldn't verify Buffer and R2. Check the credentials and connection, then try again.")
    return autopost_redirect(notice="Automatic posting enabled.")


@app.post("/autopost/pause")
def autopost_pause():
    autopost.pause()
    return autopost_redirect(notice="Automatic posting paused.")


@app.post("/autopost/verify")
def autopost_verify():
    settings = autopost.get_settings()
    if not settings:
        return autopost_redirect(error="Connect your TikTok channel in Buffer first.")
    try:
        connected = any(ch["id"] == settings["channel"] for ch in autopost.channels())
    except autopost.RateLimited:
        return autopost_redirect(error="Buffer is rate limited. Try again later.")
    except (autopost.CannotPublish, requests.RequestException, ValueError, KeyError, TypeError):
        return autopost_redirect(error="Could not verify the linked channel in Buffer. Check the API key and connection.")
    if not connected:
        return autopost_redirect(error="Buffer could not find the saved TikTok channel. Check the Buffer channel connection.")
    return autopost_redirect(notice="Buffer confirmed your TikTok channel is connected.")


@app.post("/autopost/resolve/{cid}")
def autopost_resolve(cid: str, confirm_absent: bool = Form(False)):
    try:
        autopost.resolve(cid, confirm_absent)
    except autopost.CannotPublish as exc:
        return autopost_redirect(error=str(exc))
    return autopost_redirect(notice="Clip released for manual upload." if confirm_absent else "Buffer recheck queued.")


@app.post("/run")
def run():
    threading.Thread(target=ct.backfill, daemon=True).start(); return RedirectResponse(ct.TODAY_PATH, 303)

@app.post("/vod")
def vod(vod: str = Form(max_length=50), n: int = Form(5, ge=1, le=15), workspace: str = Form("gta6")):
    if workspace not in {"gta6", "creators"}:
        return HTMLResponse("Choose a valid workspace.", 400)
    if not re.fullmatch(r"(?:(?:https://)?(?:www\.)?twitch\.tv/videos/)?\d{1,20}", vod.strip()):
        return HTMLResponse("Enter a Twitch VOD ID or its twitch.tv/videos link.", 400)
    if not VOD_SLOT.acquire(blocking=False):
        return HTMLResponse("A VOD import is already running. Check Today shortly.", 409)

    def load_vod():
        try:
            ct.vod(vod, n, workspace)
        except (OSError, ValueError, RuntimeError, TimeoutExpired) as exc:
            ct.log("VOD import", type(exc).__name__)
        finally:
            VOD_SLOT.release()

    threading.Thread(target=load_vod, daemon=True).start()
    return RedirectResponse("/creators/library" if workspace == "creators" else ct.TODAY_PATH, 303)

@app.post("/posted/{cid}")
def posted(cid: str, url: str = Form("", max_length=500)):
    return post_action(cid, posts.mark_posted, "Post recorded.", url.strip())

def moment_button(channel, workspace="gta6"):
    active = ct.LIVE.rec.get(channel)
    armed = active and active.alive()
    state, clip_id = ct.LIVE.manual.get(channel, (None, None))
    label = "Capturing…" if state == "pending" else "Clip moment" if armed else "Start buffer"
    lengths = "".join(f"<option value='{length}'>{length} sec</option>" for length in ct.CLIP_LENGTHS)
    select = (f"<label>Length <select name=length><option value=0>Mix lengths</option>{lengths}</select></label>"
              if armed and state != "pending" else "")
    button = (f"<form class=moment method=post action=/live/clip><input type=hidden name=channel value='{E(channel)}'>"
              f"<input type=hidden name=workspace value='{E(workspace)}'>"
              f"{select}<button {'disabled' if state == 'pending' else ''}>{label}</button></form>")
    if armed and channel in ct.LIVE.armed and state != "pending":
        button += (f"<form class=moment method=post action=/live/stop-buffer><input type=hidden name=channel value='{E(channel)}'>"
                   f"<input type=hidden name=workspace value='{E(workspace)}'>"
                   "<button class=ghost>Stop buffer</button></form>")
    if clip_id:
        button += f" <a href='/review/{E(clip_id)}'>View latest clip</a>"
    elif state == "failed":
        button += " <small>Last capture failed. Try again while buffering.</small>"
    return button


@app.post("/live/clip")
def live_clip(channel: str = Form(max_length=48), length: int = Form(0), workspace: str = Form("gta6")):
    clicked = time.time()
    if not re.fullmatch(r"(?:kick:)?[a-z0-9_-]{2,40}", channel):
        params = {"error": "Invalid streamer name."}
    else:
        try:
            params = {"notice": ct.LIVE.clip_moment(channel, clicked, length, workspace=workspace)}
        except (ValueError, OSError) as exc:
            ct.log("moment request", channel, type(exc).__name__)
            params = {"error": str(exc) if isinstance(exc, ValueError) else "Windows could not open the live buffer. Check available disk space and try again."}
    dest = "/creators" if workspace == "creators" else "/live"
    return RedirectResponse(dest + "?" + urlencode(params), 303)


@app.post("/live/stop-buffer")
def stop_live_buffer(channel: str = Form(max_length=48), workspace: str = Form("gta6")):
    with ct.LIVE.record_lock:
        if channel in ct.LIVE.armed and ct.LIVE.manual.get(channel, (None,))[0] != "pending":
            ct.LIVE.armed.discard(channel)
            ct.LIVE.creator_buffers.discard(channel)
            recorder = ct.LIVE.rec.pop(channel, None)
            if recorder:
                recorder.stop()
            ct.LIVE.status["recording"] = len(ct.LIVE.rec)
    return RedirectResponse("/creators" if workspace == "creators" else "/live", 303)


@app.get("/live")
def live_now(request: Request, notice: str = "", error: str = ""):
    profile = ct.creator_profile()
    body = (f"<h2>Live now: GTA</h2><p>Start the buffer before watching. <b>Clip moment</b> can mix lengths or use the "
            f"45-, 60-, or 75-second length you select. It saves about two thirds before your click and the rest after. "
            f"Allow up to {ct.lead_seconds(ct.MAX_CLIP_LENGTH)} seconds of buffered footage for the longest clip.</p>")
    if error: body += f"<p class=panel role=alert>{E(error)}</p>"
    elif notice: body += f"<p class=panel role=status>{E(notice)}</p>"
    if not os.getenv("TWITCH_CLIENT_ID"):
        rows = "".join(f"<tr id='{E(k)}'><td><a href='https://twitch.tv/{E(k)}' target=_blank>{E(s['user_name'])}</a> <b class=ok>watching chat{' + recording' if k in ct.LIVE.rec else ''}</b>{moment_button(k)}</td>"
                       f"<td>{E(s['game_name'])}</td><td><small>{E(s['title'])}</small></td></tr>" for k, s in ct.LIVE.streams.items()
                       if ct.creator_channel_allowed(k, profile))
        body += (f"<h3>Twitch: your watchlist, live in GTA (no keys needed)</h3><table><tr><th>Streamer</th><th>Game</th><th>Title</th></tr>{rows or '<tr><td>none live right now (checked every 2 min)</td></tr>'}</table>"
                 + ("<p><small>Add Twitch keys in .env to see the whole GTA category sorted by viewers.</small></p>" if profile is None else ""))
    else:
     try:
        rows = "".join(f"<tr id='{E(s['user_login'])}'><td><a href='https://twitch.tv/{E(s['user_login'])}' target=_blank>{E(s['user_name'])}</a>{' <b class=ok>watching chat</b>' if s['user_login'] in ct.LIVE.joined else ''}{moment_button(s['user_login'])}</td>"
                       f"<td>{E(s['viewer_count'])}</td><td>{E(s['game_name'])}</td><td>{E(s['language'])}</td><td><small>{E(s['title'])}</small></td></tr>" for s in ct.twitch_live())
        body += f"<h3>Twitch</h3><table><tr><th>Streamer</th><th>Viewers</th><th>Game</th><th>Lang</th><th>Title</th></tr>{rows}</table>"
     except Exception as e: body += f"<p class=bad>Twitch: {E(e)}</p>"
    try:
        kw = "".join(f"<tr id='kick-{E(k)}'><td><a href='https://kick.com/{E(k)}' target=_blank>{E(v['name'])}</a> <b class=ok>watching chat{' + recording' if 'kick:' + k in ct.LIVE.rec else ''}</b>{moment_button('kick:' + k)}</td>"
                     f"<td>{E(v['viewers'])}</td><td>{E(v['game'])}</td><td><small>{E(v['title'])}</small></td></tr>" for k, v in ct.LIVE.kick.items()
                     if ct.creator_channel_allowed(f"kick:{k}", profile))
        body += f"<h3>Kick: watched streamers live in GTA</h3><table><tr><th>Streamer</th><th>Viewers</th><th>Game</th><th>Title</th></tr>{kw or '<tr><td>none live right now</td></tr>'}</table>"
        k = ct.kick_live() if profile is None else None
        if k is None and profile is None: body += "<p>Add KICK_CLIENT_ID / KICK_CLIENT_SECRET to .env for the full Kick GTA category list (Setup page).</p>"
        if k is not None:
            rows = "".join(f"<tr id='kick-{E(s['channel']['slug'])}'><td><a href='https://kick.com/{E(s['channel']['slug'])}' target=_blank>{E(s['broadcaster_user']['username'])}</a>{moment_button('kick:' + s['channel']['slug'])}</td><td>{E(s['viewer_count'])}</td>"
                           f"<td>{E(s['category']['name'])}</td><td>{E(s['language_code'])}</td><td><small>{E(s['title'])}</small></td></tr>" for s in k if s['channel']['slug'] not in ct.LIVE.kick)
            body += f"<h3>Kick GTA category</h3><table><tr><th>Streamer</th><th>Viewers</th><th>Game</th><th>Lang</th><th>Title</th></tr>{rows}</table>"
    except Exception as e: body += f"<p class=bad>Kick: {E(e)}</p>"
    return page("Live now", body, request)

# ---------- scoreboard (numbers you enter only) ----------
F = ["views", "likes", "shares", "comments", "saves"]
@app.get("/score")
def score():
    c = ct.db(); rows = c.execute("select * from clips where workspace='gta6' and posted is not null order by posted desc limit 100").fetchall()
    stats = {(s["clip_id"], s["hours"]): s for s in c.execute("select * from stats")}
    tr = ""
    for r in rows:
        cells = "".join(f"<td>{h}h: " + (" / ".join(str(stats[(r['id'], h)][f]) for f in F) if (r['id'], h) in stats else "-") + "</td>" for h in (12, 24, 72))
        tr += (f"<tr><td>{E(r['hook'])}<br><small>{E(r['streamer'])} · {E(r['variant'])} · {E(r['posted'])}</small></td>{cells}<td><form method=post action=/stats>"
               f"<input type=hidden name=clip_id value='{E(r['id'])}'><select name=hours><option>12<option>24<option>72</select>"
               + "".join(f"<input name={f} placeholder={f} size=6 required>" for f in F) + "<button>Save</button></form></td></tr>")
    return page("Scoreboard", "<h2>Scoreboard</h2><p>Enter real TikTok numbers (views / likes / shares / comments / saves) at 12h, 24h, 72h. Nothing is guessed.</p>"
                "<form method=post action=/stats/csv enctype=multipart/form-data>CSV import (clip_id,hours,views,likes,shares,comments,saves): "
                f"<input type=file name=f><button>Import</button></form><table><tr><th>Clip</th><th>12h</th><th>24h</th><th>72h</th><th>Enter</th></tr>{tr}</table>"
                "<p><a href=/score.csv>Export scoreboard.csv</a></p>")

def save_stat(c, d):
    c.execute("insert or replace into stats values(?,?,?,?,?,?,?,?)", (d["clip_id"], int(d["hours"]), *[int(d[f]) for f in F], datetime.now().isoformat(timespec="minutes")))

@app.post("/stats")
async def stats(clip_id: str = Form(), hours: int = Form(), views: int = Form(), likes: int = Form(), shares: int = Form(), comments: int = Form(), saves: int = Form()):
    c = ct.db(); save_stat(c, locals()); c.commit(); return RedirectResponse("/score", 303)

@app.post("/stats/csv")
async def stats_csv(f: UploadFile = File()):
    limit = 2 * 1024 * 1024
    raw = await f.read(limit + 1)
    await f.close()
    if len(raw) > limit:
        return HTMLResponse("The scoreboard CSV must be 2 MiB or smaller.", 413)
    try:
        rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        with closing(ct.db()) as conn, conn:
            for number, row in enumerate(rows, 1):
                if number > 10000:
                    raise csv.Error("Row limit exceeded")
                try:
                    save_stat(conn, row)
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
    except (UnicodeError, csv.Error):
        return HTMLResponse("Use a UTF-8 scoreboard CSV with at most 10,000 rows.", 400)
    return RedirectResponse("/score", 303)

@app.get("/score.csv")
def score_csv():
    rows = ct.db().execute("select c.id clip_id,c.streamer,c.hook,c.variant,c.posted,s.hours,s.views,s.likes,s.shares,s.comments,s.saves from stats s join clips c on c.id=s.clip_id where c.workspace='gta6'").fetchall()
    out = io.StringIO(); w = csv.writer(out); w.writerow(rows[0].keys() if rows else ["clip_id"]); w.writerows([tuple(r) for r in rows])
    return HTMLResponse(out.getvalue(), media_type="text/csv")

# ---------- review: 3 edit versions, pick one, tweak, then post yourself ----------
STYLE_NAMES = {"split": "A · Facecam above gameplay", "corner": "B · Gameplay + corner cam", "hype": "C · Hype punch-ins"}


def review_redirect(cid, message="", error=False):
    query = urlencode({"error" if error else "notice": message}) if message else ""
    return RedirectResponse(f"/review/{cid}{'?' + query if query else ''}#tiktok", 303)


def post_action(cid, action, success, *args):
    try:
        action(cid, *args)
    except posts.PostError as exc:
        return review_redirect(cid, str(exc), error=True)
    return review_redirect(cid, success)


def post_panel(clip, draft, notice, error):
    cid = E(clip["id"])
    prefix = f"<section class='panel post-panel' id=tiktok><h2>TikTok upload</h2>"
    if error:
        prefix += f"<p role=alert>{E(error)}</p>"
    elif notice:
        prefix += f"<p role=status>{E(notice)}</p>"
    label = virality_label(clip)
    if label:
        prefix += f"<p><strong>{E(label)}</strong></p>"
    if clip["posted"]:
        link = (f" · <a href='{safe_link(clip['tiktok_url'])}' target=_blank rel=noopener>View on TikTok</a>"
                if (clip['tiktok_url'] or '').startswith('https://') else '')
        return prefix + f"<p>Posted {E(clip['posted'])}{link}</p></section>"
    video = ct.OUT / clip["file"]
    if not video.is_file():
        return prefix + "<p>The finished video is missing.</p></section>"
    if clip["upload_state"]:
        prefix += f"<p>{E(POST_STATUS.get(clip['upload_state']))} {E(clip['upload_detail'])}</p>"
    if clip["quality_state"] and not active_upload(clip):
        label, detail = quality_status(clip, "Quality review")
        prefix += f"<p>{E(label)} {E(detail)}</p>"
    if ct.edit_metadata(video).get("face_visible"):
        prefix += "<p>Creator facecam included.</p>"
    if active_upload(clip):
        return prefix + "<p><a href=/autopost>Check automatic posting</a></p></section>"
    creator_clip = clip["workspace"] == "creators"
    layout = ct.CREATOR_LAYOUT if creator_clip else ct.BRAND_LAYOUT
    old_layout = ct.edit_metadata(video).get("layout") != layout
    if old_layout and video.with_name("meta.json").is_file() and (not draft or draft["reserved_day"] != posts.posting_day()):
        prefix += (f"<form class=post-actions method=post action='/review/{cid}/branding' data-busy='Rendering…'>"
                   f"<button class=ghost>Apply {'highlight' if creator_clip else 'GTA VI'} layout</button></form>")
    version = video.stat().st_mtime_ns
    preview = f"<video class=post-preview src='/media/{quote(clip['file'])}?v={version}' controls playsinline preload=metadata aria-label='Selected clip'></video>"
    prefix += "<div class=post-workspace><div>"
    draft_text = draft["caption"] if draft else ""
    with closing(ct.db()) as conn:
        credit = creator_rights.credit(conn, clip["id"])
    if not credit:
        prefix += f"<p><a href='/rights?clip={quote(str(clip['id']), safe='')}'>Record permission to publish this clip</a>.</p>"
    text = creator_rights.attributed(posts.post_text(draft_text, clip["game"], clip["workspace"]), credit)
    caption = f"<label>Caption and hashtags<textarea id=post-copy readonly>{E(text)}</textarea></label>"
    if credit and draft and draft["reserved_day"] == posts.posting_day():
        options = (("<p>This video uses an earlier layout.</p>" if old_layout else "") +
                         "<p>Download the video and copy the caption. In TikTok Studio, select the downloaded MP4, paste the caption and publish.</p>"
                         + caption +
                         "<div class=post-actions><button type=button data-copy-caption>Copy caption</button>"
                         f"<a href='/post/{cid}/video' download>Download MP4</a><a class=upload-link href='{TIKTOK_STUDIO}' target=_blank rel=noopener>Open TikTok Studio</a></div>"
                         "<small id=copy-status role=status></small>"
                         f"<div class=post-actions><form method=post action='/posted/{cid}'><label>TikTok link (optional) <input name=url maxlength=500></label> "
                         f"<button>Mark posted</button></form><form method=post action='/post/{cid}/release'><button class=ghost>Release unused slot</button></form></div>")
        return prefix + options + f"</div>{preview}</div></section>"
    options = (f"<form method=post action='/post/{cid}/caption'><input type=hidden name=revision value='{draft['revision'] if draft else 0}'>"
               f"<label>Caption<textarea name=caption maxlength=320 required>{E(draft_text)}</textarea></label>"
               "<div class=post-actions><button name=intent value=save class=ghost>Save caption</button>"
               "<button name=intent value=upload>Post to TikTok</button>"
               + ("" if creator_clip else "<a href=/autopost>Automatic posting settings</a>") + "</div></form>")
    return prefix + options + f"</div>{preview}</div></section>"


@app.post("/post/{cid}/retry")
def retry_draft(cid: str):
    def queue(clip_id):
        with posts.transaction() as conn:
            posts.assert_editable(conn, clip_id)
            row = conn.execute("SELECT file FROM clips WHERE id=?", (clip_id,)).fetchone()
            if not (ct.OUT / row["file"]).is_file():
                raise posts.PostError("The finished video is missing. Pick an available edit first.")
            posts.enqueue(conn, clip_id)
            conn.execute("DELETE FROM clip_quality WHERE clip_id=?", (clip_id,))
            conn.execute("DELETE FROM clip_publish_captions WHERE clip_id=?", (clip_id,))
    return post_action(cid, queue, "Draft queued.")


@app.post("/post/{cid}/caption")
def edit_draft(cid: str, revision: int = Form(), caption: str = Form(max_length=320), intent: str = Form("save")):
    if intent != "upload":
        return post_action(cid, posts.save_caption, "Caption saved.", revision, caption)
    def prepare(clip_id):
        draft = posts.get_draft(clip_id)
        if not (draft and draft["state"] == "ready" and draft["revision"] == revision and draft["caption"] == caption.strip()):
            posts.save_caption(clip_id, revision, caption)
        reserve_branded(clip_id)
    return post_action(cid, prepare, "Upload ready.")


@app.post("/post/{cid}/reserve")
def reserve_post(cid: str):
    return post_action(cid, reserve_branded, "Upload ready.")


@app.post("/post/{cid}/release")
def release_post(cid: str):
    return post_action(cid, posts.release, "Posting slot released.")


@app.get("/post/{cid}/video")
def download_post(cid: str):
    allowed, params = ct.library_filter()
    with closing(ct.db()) as conn:
        row = conn.execute(f"""SELECT c.file FROM clips c JOIN post_drafts p ON p.clip_id=c.id
                              WHERE c.id=? AND p.reserved_day=? AND c.posted IS NULL AND {allowed}
                              AND {creator_rights.permitted()}""",
                           (cid, posts.posting_day(), *params)).fetchone()
    path = finished_file(row["file"]) if row else None
    if path is None:
        return review_redirect(cid, "Reserve a posting slot to download the finished video.", error=True)
    return FileResponse(path, filename=f"{cid}.mp4", media_type="video/mp4")


def finished_file(name):
    try:
        path = (ct.OUT / name).resolve()
        if path.is_relative_to(ct.OUT.resolve()) and path.suffix.lower() == ".mp4" and path.is_file():
            return path
    except (ValueError, OSError, RuntimeError):
        pass
    return None


@app.api_route("/media/{name:path}", methods=["GET", "HEAD"])
def media(name: str):
    if "\\" in name or ":" in name or any(part in {"", ".", ".."} for part in name.split("/")):
        return HTMLResponse("Video unavailable.", 404)
    allowed, params = ct.library_filter()
    parent, _, leaf = name.rpartition("/")
    variant = bool(parent) and leaf in {f"tiktok_{style}.mp4" for style in ct.STYLES}
    prefix = parent + "/"
    with closing(ct.db()) as conn:
        row = conn.execute(f"""SELECT c.file FROM clips c WHERE {allowed} AND
            (c.file=? OR (? AND substr(c.file,1,?)=? AND instr(substr(c.file,?),'/')=0)) LIMIT 1""",
            (*params, name, variant, len(prefix), prefix, len(prefix) + 1)).fetchone()
    path = finished_file(name) if row else None
    return FileResponse(path, media_type="video/mp4") if path else HTMLResponse("Video unavailable.", 404)


@app.get("/review/{cid}")
def review(cid: str, request: Request, notice: str = "", error: str = ""):
    allowed, params = ct.library_filter()
    with closing(ct.db()) as conn:
        r = conn.execute(CLIPS_WITH_DRAFTS + f" WHERE c.id=? AND {allowed}", (cid, *params)).fetchone()
    if not r: return RedirectResponse(ct.TODAY_PATH, 303)
    draft = posts.get_draft(cid)
    editable = not r["posted"] and not active_upload(r) and r["reserved_day"] != posts.posting_day()
    base = r["file"].rsplit("/", 1)[0]; work = ct.OUT / base
    failed = {flag.split(":", 1)[0].strip() for flag in (r["flags"] or "").split(";")}
    vids = "".join(f"<div class=card><h3>{STYLE_NAMES.get(st, st)}{' ✓ picked' if r['pick'] == st else ''}</h3><video src='/media/{quote(base)}/tiktok_{st}.mp4' controls playsinline preload=none aria-label='{E(STYLE_NAMES.get(st, st))}'></video>"
                   + (f"<form method=post action='/review/{E(cid)}/pick'><input type=hidden name=style value='{E(st)}'><button>Pick this</button></form>" if editable and st not in failed else "") + "</div>"
                   for st in ct.STYLES if (work / f"tiktok_{st}.mp4").exists())
    if not vids:
        vids = f"<div class=card><video src='/media/{quote(r['file'])}' controls playsinline preload=none aria-label='Clip preview'></video></div>"
    flags = f"<p class=panel>QA flagged: {E(r['flags'])}</p>" if r["flags"] else ""
    tweak = (f"<h2>Trim and re-render</h2><form method=post action='/review/{E(cid)}/tweak' class=panel data-busy='Rendering…'>"
             f"<label>Trim start (s) <input name=tin type=number min=0 value=0 step=0.1></label> "
             f"<label>Trim end (s) <input name=tout type=number min=0 value=0 step=0.1></label> "
             f"<label>Style <select name=style>{''.join(f'<option>{E(s)}</option>' for s in ct.STYLES)}</select></label> "
             "<button>Re-render and replace draft</button></form>") if editable else ""
    package = (f"<p><a href='/review/{E(cid)}/package' download>Download edit package</a></p>"
               if (ct.OUT / r["file"]).is_file() else "")
    variants = f"<details><summary>Review edit versions</summary>{flags}<div class=grid>{vids}</div>{tweak}</details>" if editable else ""
    return page("Review", f"<h2>Review · {E(r['streamer'])}</h2>" + post_panel(r, draft, notice, error) + package + variants,
                request, workspace=r["workspace"])


@app.get("/review/{cid}/package")
def download_edit_package(cid: str):
    allowed, params = ct.library_filter()
    with closing(ct.db()) as conn:
        row = conn.execute(f"""SELECT c.* FROM clips c WHERE c.id=? AND {allowed}
            AND NOT EXISTS (SELECT 1 FROM takedowns t WHERE t.clip_id=c.id)""", (cid, *params)).fetchone()
    if not row:
        return HTMLResponse("This clip is unavailable.", 404)
    folder = TemporaryDirectory(dir=ct.DATA)
    archive = Path(folder.name) / "edit.zip"
    try:
        media_tools.export_project(row, archive)
    except (OSError, ValueError) as exc:
        folder.cleanup()
        ct.log("edit package", type(exc).__name__)
        return review_redirect(cid, "The edit package couldn't be created. Check that the clip is available.", error=True)
    filename = "creator-edit.zip" if row["workspace"] == "creators" else "gta6-edit.zip"
    return FileResponse(archive, filename=filename, media_type="application/zip",
                        background=BackgroundTask(folder.cleanup))


@app.post("/review/{cid}/pick")
def review_pick(cid: str, style: str = Form()):
    def pick(clip_id):
        with posts.transaction() as conn:
            posts.assert_editable(conn, clip_id)
            r = conn.execute("SELECT file, flags, pick FROM clips WHERE id=?", (clip_id,)).fetchone()
            dst = ct.OUT / r["file"]
            src = dst.with_name(f"tiktok_{style}.mp4") if style in ct.STYLES else None
            if not src or not src.is_file() or any(flag.strip().startswith(style + ':') for flag in (r["flags"] or "").split(";")):
                raise posts.PostError("This version has no video that passed QA.")
            if style == r["pick"] and dst.is_file():
                return
            try:
                ct.select_edit(src, dst)
            except OSError as exc:
                raise posts.PostError("The video file is in use. Stop the preview and try again.") from exc
            conn.execute("UPDATE clips SET pick=? WHERE id=?", (style, clip_id))
            posts.enqueue(conn, clip_id)
    return post_action(cid, pick, "Selected video saved.")


@app.post("/review/{cid}/tweak")
def review_tweak(cid: str, tin: float = Form(0, ge=0), tout: float = Form(0, ge=0), style: str = Form("split")):
    return post_action(cid, render_edit, "Updated video saved.", style, tin, tout)


@app.post("/review/{cid}/branding")
def apply_branding(cid: str):
    def apply(clip_id):
        if not render_edit(clip_id, branding=True):
            raise posts.PostError("This older edit needs its original footage and trim points to apply the new layout.")
    with closing(ct.db()) as conn:
        row = conn.execute("SELECT workspace FROM clips WHERE id=?", (cid,)).fetchone()
    return post_action(cid, apply, "Highlight layout saved." if row and row["workspace"] == "creators" else "GTA VI layout saved.")


def reserve_branded(clip_id):
    draft = posts.get_draft(clip_id)
    if not draft or not draft["caption"]:
        raise posts.PostError("Write and save a caption about this clip before uploading.")
    if draft["reserved_day"] != posts.posting_day():
        render_edit(clip_id, branding=True)
    posts.reserve(clip_id)


def render_edit(clip_id, style=None, tin=0, tout=0, *, branding=False):
    with posts.transaction() as conn:
        posts.assert_editable(conn, clip_id)
        row = conn.execute("SELECT file, credit, pick, streamer, workspace FROM clips WHERE id=?", (clip_id,)).fetchone()
    dst = ct.OUT / row["file"]
    selected = ct.edit_metadata(dst)
    layout = ct.CREATOR_LAYOUT if row["workspace"] == "creators" else ct.BRAND_LAYOUT
    if branding and selected.get("layout") == layout:
        return True
    meta_file = dst.with_name("meta.json")
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
    raw = Path(meta["raw"]) if meta.get("raw") else None
    style = style or row["pick"] or (ct.STYLES[0] if ct.STYLES else "split")
    if not raw or not raw.is_file() or style not in ct.STYLES:
        if branding:
            return False
        raise posts.PostError("This clip needs its original footage to re-render in the selected style.")
    cut = selected if branding and selected else meta
    if branding and (not dst.is_file() or (not selected and abs(ct.probe_duration(dst) - cut["dur"]) > .5)):
        return False
    stamp = dst.stat().st_mtime_ns if dst.is_file() else None
    start, dur = cut["start"] + tin, cut["dur"] - tin - tout
    if not 7 <= dur <= cut["dur"]:
        raise posts.PostError("The trimmed video needs to be at least seven seconds long.")
    face = ct.find_facecam(raw, start, dur, row["streamer"], cut.get("face"))
    peak = max(0, (cut.get("peak") or 0) - tin)
    with ct.RENDER_WORKERS, TemporaryDirectory(dir=ct.DATA) as temp:
        work = Path(temp)
        original_subs = dst.with_name("captions.ass")
        subs = None
        try:
            if branding and original_subs.is_file() and start == meta["start"] and dur == meta["dur"]:
                shutil.copyfile(original_subs, work / original_subs.name)
                subs = original_subs.name
            else:
                subs, _ = ct.captions(raw, start, dur, work)
        except Exception as exc:
            ct.log("review captions failed", clip_id, type(exc).__name__)
        render_file = work / f"tiktok_{style}.mp4"
        try:
            ct.render(raw, render_file, row["credit"], start=start, dur=dur, facecam=face, subs=subs, style=style,
                      peak=peak, workspace=row["workspace"])
            why = ct.qa(render_file, subs, face, dur)
        except Exception as exc:
            ct.log("review render failed", clip_id, type(exc).__name__)
            raise posts.PostError("The new edit couldn't be rendered. The current video is still available.") from exc
        if why:
            raise posts.PostError(f"The new edit failed QA ({why}).")
        with posts.transaction() as conn:
            posts.assert_editable(conn, clip_id)
            if stamp != (dst.stat().st_mtime_ns if dst.is_file() else None):
                raise posts.PostError("Another edit changed this video. Reload and try again.")
            old_flags = conn.execute("SELECT flags FROM clips WHERE id=?", (clip_id,)).fetchone()["flags"]
            try:
                variant = dst.with_name(render_file.name)
                render_file.replace(variant)
                render_file.with_suffix(".json").replace(variant.with_suffix(".json"))
                subtitle_file = render_file.with_suffix(".ass")
                if subtitle_file.is_file():
                    subtitle_file.replace(variant.with_suffix(".ass"))
                else:
                    variant.with_suffix(".ass").unlink(missing_ok=True)
                ct.select_edit(variant, dst)
            except OSError as exc:
                raise posts.PostError("The video file is in use. Stop the preview and try again.") from exc
            flags = "; ".join(f.strip() for f in (old_flags or "").split(";") if f.strip() and not f.strip().startswith(style + ':'))
            conn.execute("UPDATE clips SET pick=?, flags=? WHERE id=?", (style, flags, clip_id))
            if not branding:
                transcript, target = work / "transcript.txt", dst.with_name(f"transcript_{style}.txt")
                if transcript.is_file():
                    transcript.replace(target)
                else:
                    target.unlink(missing_ok=True)
                posts.enqueue(conn, clip_id)
    return True

@app.get("/monitor")
def post_monitor(request: Request):
    account, synced, tracked = growth.monitored()
    status = "Connect a TikTok channel in Automatic posting settings."
    if account:
        status = f"{account['account_name']} through Buffer. Waiting for the first sync."
        if synced:
            checked = datetime.fromtimestamp(synced["checked"], posts.SAST).strftime("%d %b at %H:%M SAST")
            status = f"{account['account_name']} through Buffer · {synced['status']} · {checked}"
    rows = []
    for row in tracked:
        date = datetime.fromtimestamp(row["published"], posts.SAST).strftime("%d %b at %H:%M")
        caption = E(row["caption"] or "Open TikTok post")
        cells = []
        for key, label in growth.MONITOR_METRICS:
            value = row["metrics"].get(key)
            if key == "reactions" and value is None:
                value = row["metrics"].get("likes")
            cells.append(f"<td>{value:,.0f}</td>" if value is not None else '<td aria-label="Unavailable">—</td>')
        rows.append(f"<tr><td><a href='{safe_link(row['url'])}' target=_blank rel=noopener>{caption}</a><br><small>{date}</small></td>"
                    f"{''.join(cells)}<td>{row['snapshots']:,}</td></tr>")
    headings = "".join(f"<th scope=col>{label}</th>" for _, label in growth.MONITOR_METRICS)
    table = (f"<div class=growth-scroll><table><thead><tr><th scope=col>TikTok post</th>{headings}"
             f"<th scope=col>Snapshots</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>" if rows else
             "<p>Published TikTok posts will appear after Buffer returns their links.</p>")
    return page("Post Monitor", f"<h2>TikTok Post Monitor</h2><p>{E(status)}</p>"
                "<p><a href=/autopost>Connected account</a> · <a href=/learning>Learning and analytics</a> · <a href=/engagement>Reply inbox</a></p>"
                f"<section class='panel growth-section'><h3>Recent published posts</h3>{table}</section>"
                "<p>Buffer supplies available counts. A dash means unavailable. To read or reply to individual comments, open the post in TikTok.</p>", request)


@app.get("/engagement")
def engagement(request: Request, message: str = ""):
    account, _, tracked = growth.monitored()
    if not account or not tracked:
        return page("Reply inbox", "<h2>Reply inbox</h2><p>Sync published posts from your connected TikTok account first.</p>"
                    "<p><a href=/monitor>Post Monitor</a></p>", request)
    options = []
    for entry in tracked:
        date = datetime.fromtimestamp(entry["published"], posts.SAST).strftime("%d %b %H:%M")
        options.append(f"<option value='{entry['post_id']}'>{E(date + ' · ' + str(entry['post_id']))}</option>")
    items = []
    for row in growth.reply_inbox(account["channel"]):
        destination = f"<a href='{safe_link(row['url'])}' target=_blank rel=noopener>Open post in TikTok</a>"
        items.append(f"<article class=panel><p><b>{E(row['state'].capitalize())}</b> · {destination}</p>"
                     f"<p>{E(row['comment'])}</p><form method=post action='/engagement/{row['id']}'>"
                     f"<label>Reply draft<textarea name=reply maxlength=300 rows=3>{E(row['reply'])}</textarea></label>"
                     "<div class=post-actions><button name=action value=suggest>Suggest reply</button>"
                     "<button name=action value=save>Save edit</button><button name=action value=approve>Approve draft</button>"
                     "<button name=action value=reject>Reject</button><button name=action value=escalate>Escalate</button></div>"
                     "</form></article>")
    feedback = f"<p role=status>{E(message)}</p>" if message else ""
    return page("Reply inbox", f"<h2>Reply inbox</h2>{feedback}<p><a href=/monitor>Post Monitor</a></p>"
                "<p>Paste a real comment from one of your TikTok posts. Approved replies stay here for you to send in TikTok.</p>"
                "<form method=post action=/engagement class=panel><label>TikTok post"
                f"<select name=post_id>{''.join(options)}</select></label>"
                "<label>Audience comment<textarea name=comment required maxlength=1500 rows=3></textarea></label>"
                f"<button>Add to inbox</button></form>{''.join(items)}", request)


@app.post("/engagement")
def add_engagement(post_id: str = Form(pattern=r"^[0-9]{1,32}$"), comment: str = Form(min_length=1, max_length=1500)):
    account = autopost.get_settings()
    try:
        if not account:
            raise posts.PostError("Connect your TikTok account first.")
        growth.queue_comment(account["channel"], post_id, comment)
        message = "Audience comment saved."
    except posts.PostError as exc:
        message = str(exc)
    return RedirectResponse(f"/engagement?{urlencode({'message': message})}", status_code=303)


@app.post("/engagement/{reply_id}")
def update_engagement(reply_id: int, action: str = Form(max_length=12), reply: str = Form("", max_length=300)):
    account = autopost.get_settings()
    try:
        if not account:
            raise posts.PostError("Connect your TikTok account first.")
        growth.review_reply(account["channel"], reply_id, action, reply)
        message = "Reply review saved."
    except posts.PostError as exc:
        message = str(exc)
    return RedirectResponse(f"/engagement?{urlencode({'message': message})}", status_code=303)


@app.get("/learning")
def learning(request: Request, notice: str = "", error: str = ""):
    synced, tracked, progress = growth.report(milestones=True)
    target, hits, samples = progress["target"], progress["hits"], progress["samples"]
    hit_rate = f"{hits / samples:.0%}" if samples else "—"
    best = f"{progress['best']:,}" if progress["best"] is not None else "—"
    reached = progress["reached"]
    last_sync = (f"{E(synced['status'])} · {datetime.fromtimestamp(synced['checked'], posts.SAST):%d %b at %H:%M} SAST"
                 if synced else "Waiting for the first Buffer analytics sync.")
    feedback = f"<p role=alert>{E(error)}</p>" if error else f"<p role=status>{E(notice)}</p>" if notice else ""
    metrics = ("<div class=growth-grid>"
               f"<div class=panel><p class=growth-metric>{target:,}</p><small>{'Current' if target in reached else 'Next'} views milestone per post</small></div>"
               f"<div class=panel><p class=growth-metric>{hit_rate}</p><small>{hits} of {samples} recent measured posts hit this milestone</small></div>"
               f"<div class=panel><p class=growth-metric>{best}</p><small>Best day-three views in that group</small></div></div>")
    steps = "".join(f"<li><strong>{goal:,}</strong> · "
                    f"{'Achieved' if goal in reached else 'In progress' if goal == target else 'Upcoming'}</li>"
                    for goal in growth.MILESTONES)
    rows = []
    for row in sorted(tracked, key=lambda r: r["views"], reverse=True)[:12]:
        published = datetime.fromtimestamp(row["sent"], posts.SAST).strftime("%d %b · %H:%M")
        link = f"<a href='{safe_link(row['url'])}' rel=noopener target=_blank>View TikTok</a>" if row["url"].startswith("https://") else ""
        checkpoint = row["checkpoint_views"]
        watch = f"{row['watch_seconds']:.1f}s" if row["watch_seconds"] is not None else "—"
        shares = f"{row['shares']:,}" if row["shares"] is not None else "—"
        rows.append(f"<tr><td>{published}<br>{link}</td><td>{row['views']:,}</td><td>{checkpoint if checkpoint is not None else '—'}</td>"
                    f"<td>{watch}</td><td>{shares}</td></tr>")
    leading = ("<div class=growth-scroll><table><tr><th>Posted (SAST)</th><th>Latest views</th><th>Day 3 views</th>"
               f"<th>Average watch</th><th>Shares</th></tr>{''.join(rows)}</table></div>" if rows else "<p>Buffer will supply these metrics after posts are published.</p>")
    trials = growth.trial_results(tracked, target)
    trial_rows = []
    for name, result in trials.items():
        median_views = f"{result['median']:,.0f}" if result["median"] is not None else "—"
        beats = f"{result['beat_forecast']}/{result['forecasts']}" if result["forecasts"] else "—"
        label = "Quiet-hour fallback" if name == "fallback" else name.capitalize()
        trial_rows.append(f"<tr><td>{E(label)}</td><td>{result['samples']}</td><td>{median_views}</td>"
                          f"<td>{result['hits']}/{result['samples']}</td><td>{beats}</td></tr>")
    scoring_trials = ("<div class=growth-scroll><table><tr><th>Post type</th><th>Measured</th><th>Median day-three views</th>"
                      f"<th>Reached milestone</th><th>Beat prior score-band median</th></tr>{''.join(trial_rows)}</table></div>")
    experiments = growth.cohorts(tracked, target)
    comparisons = []
    for kind, results in experiments.items():
        for cohort in results:
            choice = cohort["choice"]
            if kind == "style":
                label = STYLE_NAMES.get(choice, choice)
            elif kind == "duration":
                label = f"{choice} seconds"
            else:
                label = f"{kind.capitalize()} · {choice}"
            comparisons.append(f"<tr><td>{E(label)}</td><td>{cohort['n']}</td>"
                               f"<td>{cohort['hits']}/{cohort['n']}</td><td>{cohort['median']:,.0f}</td></tr>")
    duration = growth.preferred(experiments["duration"])
    style = growth.preferred(experiments["style"])
    angle = growth.preferred(experiments["caption"])
    preference = " · ".join(filter(None, (f"{duration} seconds" if duration else "", STYLE_NAMES.get(style, ""),
                                          f"{angle} caption" if angle else "")))
    testing = f"<p>Currently favoring <b>{E(preference)}</b> and continuing to test alternatives.</p>" if preference else "<p>Collecting results from comparable clips before favoring a format.</p>"
    learning_results = ("<div class=growth-scroll><table><tr><th>Format</th><th>Sample count</th><th>Hit target</th>"
                        f"<th>Median day 3 views</th></tr>{''.join(comparisons)}</table></div>" if comparisons else "")
    references = "".join(f"<li><a href='{safe_link(row['url'])}' target=_blank rel=noopener>View example</a>"
                         f"{' · ' + format(row['views'], ',') + ' observed views' if row['views'] is not None else ''} · {E(row['opening'])}</li>"
                         for row in growth.references())
    discovery_status, trends = growth.discovery_report()
    discovered = []
    for trend in trends:
        links = " · ".join(f"<a href='{safe_link(url)}' target=_blank rel=noopener>Watch ranked clip {number}</a>"
                           for number, url in enumerate(trend["videos"], 1))
        count = trend["hashtag_views"]
        views = f" · {count:,} hashtag views over 7 days" if isinstance(count, int) else ""
        discovered.append(f"<li><b>#{E(trend['hashtag'].lstrip('#'))}</b> · {E(trend['market'])}{views}<br>{links}</li>")
    results = f"<ul>{''.join(discovered)}</ul>" if discovered else ""
    public_examples = []
    for source, status, examples in research.saved_sources():
        links = []
        for item in examples[:4]:
            value = item.get("views")
            likes = item.get("likes")
            metric = f" · {value:,} public views" if isinstance(value, int) else f" · {likes:,} likes" if isinstance(likes, int) else ""
            links.append(f"<li><a href='{safe_link(item['url'])}' target=_blank rel=noopener>{E(item['title'] or 'View example')}</a>{metric}</li>")
        scope = " · selected channels, including full-length uploads" if source == "YouTube public feeds" else ""
        public_examples.append(f"<h4>{E(source)}</h4><p>{E(status + scope)}</p>" + (f"<ul>{''.join(links)}</ul>" if links else ""))
    observations = "".join(f"<li>{E(item['source'])} · {E(item['opening'])} <strong>Payoff</strong> {E(item['payoff'])}</li>"
                           for item in research.visual_examples()[:4])
    patterns = f"<ul>{observations}</ul>" if observations else "<p>Waiting for Umbrel to review an available video sample.</p>"
    t = ct.ROOT.joinpath("learning.md")
    brief = t.read_text(encoding="utf-8") if t.is_file() else "Umbrel writes a brief after collecting post metrics or research examples."
    return page("Learning", f"<h2>Road to {growth.MILESTONES[-1]:,} views per post</h2>{feedback}{metrics}"
                f"<p>Each milestone takes 60% of the latest {growth.MILESTONE_SAMPLE} measured posts, with at least "
                f"{growth.MIN_SAMPLES} available. Each post uses its first Buffer snapshot at "
                f"{growth.CHECKPOINT}–{growth.CHECKPOINT_END} hours.</p>"
                f"<details><summary>View milestone ladder</summary><ul>{steps}</ul></details>"
                f"<p><small>{last_sync}</small></p>"
                "<div class=post-actions><form method=post action=/nightly><button>Sync and run learning</button></form></div>"
                f"<section class='panel growth-section'><h3>Best posts by latest views</h3>{leading}</section>"
                f"<section class='panel growth-section'><h3>Virality score experiments</h3>"
                "<p>Each post is compared at the same age. Predictions use prior posts in its score band once five have a day-three reading.</p>"
                f"{scoring_trials}</section>"
                f"<section class='panel growth-section'><h3>Format experiments</h3>{testing}{learning_results}</section>"
                "<section class='panel growth-section'><h3>Trend research</h3>"
                "<p>Public rankings and counts come from provider listings. Umbrel records openings and payoffs after inspecting available footage.</p>"
                f"<p>{E(discovery_status)} · <a href='/setup#research'>Discovery setup</a></p>{results}"
                f"{''.join(public_examples)}<h4>Observed openings and payoffs</h4>{patterns}"
                f"<p><a href='{growth.CREATIVE_CENTER}' target=_blank rel=noopener>TikTok trending videos</a> · "
                f"<a href='{growth.CREATOR_SEARCH}' target=_blank rel=noopener>Creator Search Insights</a> · "
                f"<a href='{growth.RECOMMENDATIONS}' target=_blank rel=noopener>What TikTok measures</a></p>"
                f"{f'<ul>{references}</ul>' if references else ''}<form class=growth-form method=post action=/growth/reference>"
                "<label>TikTok video URL<input type=url name=url maxlength=400 required></label>"
                "<label>Visible views (optional)<input type=number name=views min=0></label>"
                "<label>What does the opening show?<textarea name=opening maxlength=300 required></textarea></label>"
                "<button>Save example</button></form></section>"
                f"<section class='panel growth-section'><h3>Umbrel's testing brief</h3><pre style='white-space:pre-wrap;overflow-wrap:anywhere'>{E(brief)}</pre></section>"
                "<section class='panel growth-section'><h3>Creator Rewards</h3><p>The view target uses regular video views. "
                "Creator Rewards requires an eligible account, original videos over one minute, and qualified views. Check eligibility and actual earnings in TikTok Studio.</p>"
                f"<p><a href='{growth.REWARDS}' target=_blank rel=noopener>TikTok Creator Rewards requirements</a></p></section>", request)

@app.post("/nightly")
def nightly():
    def refresh():
        import writer
        growth.sync(force=True)
        growth.sync_discovery(force=True)
        research.refresh()
        writer.nightly()
    threading.Thread(target=refresh, daemon=True).start()
    return RedirectResponse("/learning?notice=Analytics+sync+and+learning+started.", 303)


@app.post("/growth/reference")
def growth_reference(url: str = Form(max_length=400), views: int | None = Form(None, ge=0, le=10**12),
                     opening: str = Form(min_length=1, max_length=300)):
    try:
        growth.add_reference(url.strip(), views, opening)
    except ValueError as exc:
        return RedirectResponse("/learning?" + urlencode({"error": str(exc)}), 303)
    return RedirectResponse("/learning?notice=Research+example+saved.", 303)

# ---------- watchlist / takedowns ----------
def creator_redirect(**message):
    return RedirectResponse("/creators" + ("?" + urlencode(message) if message else ""), 303)


@app.get("/creators")
def creator_channels(request: Request, notice: str = "", error: str = ""):
    rows = []
    excluded = set(ct.watchlist().get("exclude", ()))
    for channel, source in ct.creator_channels().items():
        kick = channel.startswith("kick:")
        slug = channel.removeprefix("kick:")
        platform, host = ("Kick", "kick.com") if kick else ("Twitch", "twitch.tv")
        blocked = channel in excluded or slug in excluded
        live = ct.LIVE.stream_info(channel, "creators") if source["enabled"] else {}
        status = "Excluded" if blocked else "Live" if live else "Enabled" if source["enabled"] else "Disabled"
        capture = moment_button(channel, "creators") if source["enabled"] and not blocked else ""
        action, label = (0, "Disable clipping") if source["enabled"] else (1, "Enable clipping")
        rows.append(f"<article class=panel><h3><a href='https://{host}/{E(slug)}' target=_blank rel=noopener>{E(slug)}</a></h3>"
                    f"<p>{platform} · {'Own channel' if source['own'] else 'Creator with permission'} · {status}</p>"
                    f"<p>{E(live.get('title', ''))}</p>{capture}"
                    "<form method=post action=/creators/enable>"
                    f"<input type=hidden name=channel value='{E(channel)}'><input type=hidden name=enabled value={action}>"
                    f"<button class=ghost>{label}</button></form></article>")
    feedback = f"<p role=alert>{E(error)}</p>" if error else f"<p role=status>{E(notice)}</p>" if notice else ""
    return page("Creator channels", f"<h2>Creator channels</h2>{feedback}"
                "<p>Add your own channel or a creator whose content you have permission to clip. "
                "Enable clipping, then start its live buffer and mark the moment when you want to capture it.</p>"
                "<form class=panel method=post action=/creators/add>"
                "<label>Platform <select name=platform><option value=twitch>Twitch</option><option value=kick>Kick</option></select></label> "
                "<label>Channel username or URL <input name=source maxlength=200 required></label> "
                "<label>Permission <select name=permission required><option value=''>Choose permission</option>"
                "<option value=own>My own channel</option><option value=permission>I have permission to clip this creator</option>"
                "</select></label> <button>Add channel</button></form>"
                f"<p><a href=/creators/library>Open Creator clips</a></p>"
                + ("<div class=grid>" + "".join(rows) + "</div>" if rows else "<p>Add a Twitch or Kick channel to get started.</p>"),
                request, workspace="creators")


@app.post("/creators/add")
def add_creator_channel(platform: str = Form(), source: str = Form(max_length=200), permission: str = Form()):
    if permission not in {"own", "permission"}:
        return creator_redirect(error="Choose whether you own the channel or have permission to clip it.")
    try:
        channel = ct.parse_creator_channel(platform, source)
    except ValueError as exc:
        return creator_redirect(error=str(exc))
    with posts.transaction() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO creator_channels(channel,own) VALUES(?,?)", (channel, int(permission == "own")))
    return creator_redirect(notice="Channel added. Enable clipping when you are ready." if cur.rowcount else "This channel is already listed.")


@app.post("/creators/enable")
def enable_creator_channel(channel: str = Form(max_length=48), enabled: bool = Form()):
    with posts.transaction() as conn:
        cur = conn.execute("UPDATE creator_channels SET enabled=? WHERE channel=?", (int(enabled), channel))
    if not cur.rowcount:
        return creator_redirect(error="Add this channel before enabling clipping.")
    if not enabled:
        ct.LIVE.stop_creator_buffer(channel)
    return creator_redirect(notice="Clipping enabled." if enabled else "Clipping disabled.")


@app.get("/creators/library")
def creator_library(request: Request, number: int = Query(1, alias="page", ge=1, le=100000)):
    with closing(ct.db()) as conn:
        rows, has_next = clip_page(conn, ct.CREATOR_LIBRARY, (), number)
    clips = "<div class=grid>" + "".join(card(row) for row in rows) + "</div>" if rows else "<p>No creator clips yet.</p>"
    return page("Creator clips", "<p>Review, export or prepare clips for manual posting.</p>"
                "<form class=panel method=post action=/vod><input name=workspace type=hidden value=creators>"
                "<label>Twitch VOD URL <input name=vod maxlength=50 required></label> "
                "<label>Clips <input name=n type=number min=1 max=15 value=5></label> "
                "<button>Import highlights</button></form>" + clips + clip_pager("/creators/library", number, has_next),
                request, workspace="creators")


@app.get("/watchlist")
def wl(request: Request):
    w = ct.watchlist()
    profile = ct.creator_profile()
    add = ("<form method=post action=/watchlist/add><label>Twitch or Kick channel URL <input name=url maxlength=200 required></label> <button>Add</button></form>"
           if profile is None else f"<p>Creator list on this PC · {E(', '.join(c['name'] for c in profile.values()))}</p>")
    readonly = " readonly" if profile is not None else ""
    return page("Watchlist", f"<h2>Watchlist</h2>{add}"
                f"<form method=post><p>Always include (Twitch logins, one per line)<br><textarea name=inc{readonly}>{E(chr(10).join(w['include']))}</textarea></p>"
                f"<p>Kick streamers to watch (chat spikes + recording), one per line<br><textarea name=kinc{readonly}>{E(chr(10).join(w.get('kick_include', [])))}</textarea></p>"
                f"<p><b>Chat watch words</b>: clip when chat says any of these at least <input name=wmin type=number min=2 max=100 value={int(w.get('chat_word_min', 4))} style='width:4em'> times within 10 seconds. A built-in hype list (clip it, lmao, no way, wtf, cops, chase, crash...) is always on; add your own here, one word or phrase per line (e.g. <code>clip it</code>, <code>no way</code>)<br><textarea name=words>{E(chr(10).join(w.get('chat_words', [])))}</textarea></p>"
                f"<p>Never include<br><textarea name=exc>{E(chr(10).join(w['exclude']))}</textarea></p>"
                "<p>Facecam boxes for the split layout, one per line: <code>login x y w h</code> as fractions of the stream picture "
                "(e.g. <code>kaicenat 0.70 0.00 0.30 0.30</code> = top-right 30%). Streamers without a box get the blurred layout.<br>"
                f"<textarea name=face>{E(chr(10).join(f'{k} ' + ' '.join(map(str, v)) for k, v in w.get('facecam', {}).items()))}</textarea></p><button>Save</button></form>", request)

@app.post("/watchlist/add")
def wl_add(url: str = Form("")):
    if ct.creator_profile() is not None:
        return RedirectResponse("/watchlist", 303)
    m = re.match(r"^(?:https?://)?(?:www\.|m\.)?(twitch\.tv|kick\.com)/([A-Za-z0-9_]{2,25})(?:[/?#].*)?$", url.strip())
    if not m: return page("Watchlist", "<p>That isn't a Twitch or Kick channel link. <a href=/watchlist>Back</a></p>")
    w = ct.watchlist(); k = "include" if m.group(1) == "twitch.tv" else "kick_include"; name = m.group(2).lower()
    if name not in w.setdefault(k, []): w[k].append(name)
    w["exclude"] = [x for x in w.get("exclude", []) if x != name]
    (ct.ROOT / "watchlist.json").write_text(json.dumps(w, indent=2), encoding="utf-8"); return RedirectResponse("/watchlist", 303)

@app.post("/watchlist")
def wl_save(inc: str = Form(""), exc: str = Form(""), face: str = Form(""), kinc: str = Form(""), words: str = Form(""), wmin: int = Form(4)):
    w = ct.stored_watchlist(); fc = {}
    for line in face.splitlines():
        p = line.split()
        try:
            if len(p) == 5 and all(0 <= float(x) <= 1 for x in p[1:]): fc[p[0].lower()] = [float(x) for x in p[1:]]
        except ValueError: pass
    w["chat_words"] = list(dict.fromkeys(" ".join(l.lower().split())[:40] for l in words.splitlines() if l.strip()))[:50]; w["chat_word_min"] = min(100, max(2, wmin))
    w["exclude"] = [s.lower() for s in exc.split()]
    if ct.creator_profile() is None:
        w["facecam"] = fc
        w["kick_include"] = [s.lower() for s in kinc.split()]
        w["include"] = [s.lower() for s in inc.split()]
    else:
        (ct.DATA / "facecams.json").write_text(json.dumps(fc, indent=2), encoding="utf-8")
    (ct.ROOT / "watchlist.json").write_text(json.dumps(w, indent=2), encoding="utf-8"); return RedirectResponse("/watchlist", 303)

@app.get("/takedowns")
def takedowns():
    rows = ct.db().execute("select * from takedowns order by id desc").fetchall()
    tr = "".join(f"<tr><td>{E(r['date'])}</td><td>{E(r['clip_id'])}</td><td>{E(r['who'])}</td><td>{E(r['reason'])}</td><td>{E(r['action'])}</td></tr>" for r in rows)
    return page("Takedowns", "<h2>Takedown log</h2><p>Record a clip or creator to block local access and queued uploads. Remove existing copies from Buffer and TikTok as well.</p>"
                "<form method=post><input name=clip_id placeholder='clip id or streamer'><input name=who placeholder='who asked'><input name=reason placeholder=reason>"
                f"<input name=action placeholder='what we did'><button>Log</button></form><table><tr><th>Date</th><th>Clip</th><th>Who</th><th>Reason</th><th>Action</th></tr>{tr}</table>")


@app.get("/rights")
def rights_page(request: Request, clip: str = ""):
    with closing(ct.db()) as conn:
        rows = conn.execute("SELECT * FROM creator_permissions ORDER BY id DESC LIMIT 100").fetchall()
    history = "".join(
        f"<li class=panel><strong>{E(row['subject'])}</strong> · {E(row['scope'])} · "
        f"{'Rewards allowed' if row['monetize'] else 'Publishing only'}"
        f"<p>Credit {E(row['credit'])}<br>Evidence reference {E(row['evidence'])}<br>"
        f"Valid through {E(row['expires'] or 'no recorded expiry')}</p>"
        + (f"<p>Revoked {E(row['revoked'])}. Check copies already sent to the provider.</p>" if row["revoked"] else
           f"<form method=post action='/rights/{row['id']}/revoke'><button class=ghost>Revoke permission</button></form>") + "</li>"
        for row in rows
    )
    return page("Creator rights", f"""<h2>Creator permissions</h2>
      <p>Record the permission you have to post a clip on TikTok. Include publisher and music rights where applicable.
      Only select rewards when the permission covers monetization.</p>
      <form class=panel method=post action=/rights>
      <label>Scope <select name=scope><option value=clip>Clip</option><option value=creator>Creator</option></select></label>
      <label>Clip ID or exact creator name <input name=subject value='{E(clip)}' maxlength=180 required></label>
      <label>Creator credit <input name=credit maxlength=120 required></label>
      <label>Evidence reference <input name=evidence maxlength=300 required></label>
      <label>Expiry, if any <input name=expires type=date></label>
      <label><input type=checkbox name=monetize value=1> Permission covers rewards and monetization</label>
      <button>Record permission</button></form>
      <p><a href=/takedowns>Record a takedown</a> if you need to block a clip or creator across every grant.</p>
      <ul class=post-queue>{history}</ul>""", request)


@app.post("/rights")
def save_rights(scope: str = Form(), subject: str = Form(max_length=180), credit: str = Form(max_length=120),
                evidence: str = Form(max_length=300), expires: str = Form("", max_length=10), monetize: bool = Form(False)):
    subject, credit, evidence = subject.strip(), " ".join(credit.split()), evidence.strip()
    if scope == "creator":
        subject = subject.removeprefix("@").strip().lower()
    if scope not in {"clip", "creator"} or not all((subject, credit, evidence)):
        return HTMLResponse("Choose the scope and enter the clip or creator, credit, and evidence reference.", 400)
    if expires:
        try:
            if datetime.strptime(expires, "%Y-%m-%d").date().isoformat() != expires:
                raise ValueError
        except ValueError:
            return HTMLResponse("Enter a valid expiry date.", 400)
    with posts.transaction() as conn:
        if scope == "clip" and not conn.execute("SELECT 1 FROM clips WHERE id=?", (subject,)).fetchone():
            return HTMLResponse("No matching clip. Copy the clip ID from Review.", 404)
        conn.execute("INSERT INTO creator_permissions(scope,subject,credit,evidence,monetize,expires) VALUES(?,?,?,?,?,?)",
                     (scope, subject, credit, evidence, int(monetize), expires))
    return RedirectResponse("/rights", 303)


@app.post("/rights/{permission_id}/revoke")
def revoke_rights(permission_id: int):
    with posts.transaction() as conn:
        conn.execute("UPDATE creator_permissions SET revoked=current_timestamp WHERE id=? AND revoked IS NULL",
                     (permission_id,))
    return RedirectResponse("/rights", 303)

@app.post("/takedowns")
def td_save(clip_id: str = Form(min_length=1, max_length=180), who: str = Form("", max_length=180),
            reason: str = Form("", max_length=1500), action: str = Form("", max_length=1500)):
    clip_id = clip_id.strip()
    if not clip_id:
        return HTMLResponse("Enter a clip ID or a creator name.", 400)
    names = ct.creator_names(ct.creator_profile())
    normalized = clip_id.removeprefix("@").lower()
    with posts.transaction() as conn:
        row = conn.execute("SELECT 1 FROM clips WHERE id=?", (clip_id,)).fetchone()
        targets = {clip_id} if row else {normalized, *(alias for alias, key in names.items() if key == names.get(normalized))}
        conn.executemany("INSERT INTO takedowns(clip_id,date,who,reason,action) VALUES(?,?,?,?,?)",
                         [(target, ct.today(), who, reason, action) for target in sorted(targets)])
        affected = f"SELECT c.id FROM clips c WHERE {ct.TAKEN_DOWN}"
        conn.execute(f"""UPDATE post_drafts SET reserved_day=NULL WHERE clip_id IN ({affected})""")
        conn.execute(f"""UPDATE tiktok_uploads SET state='failed',detail='Takedown recorded.',run_after=0
            WHERE state='uploading' AND clip_id IN ({affected})""")
    if not row:
        watchlist = ct.stored_watchlist()
        watchlist["exclude"] = sorted(set(watchlist["exclude"]) | targets)
        (ct.ROOT / "watchlist.json").write_text(json.dumps(watchlist, indent=2), encoding="utf-8")
    return RedirectResponse("/takedowns", 303)

# ---------- earnings ----------
def seed_campaigns():
    c = ct.db()
    for x in json.loads((ct.ROOT / "campaigns_seed.json").read_text(encoding="utf-8")):
        if not c.execute("select 1 from campaigns where name=?", (x["name"],)).fetchone():
            c.execute(f"insert into campaigns({','.join(x)}) values({','.join('?' * len(x))})", tuple(x.values()))
    c.commit()

def whop_panel():
    worker = whop_api.connected_worker()
    with closing(ct.db()) as conn:
        status = conn.execute("SELECT detail FROM whop_scan WHERE id=1").fetchone()
        bounties = conn.execute("""SELECT b.*,r.revision AS approved,r.worker FROM whop_bounties b
            LEFT JOIN whop_rules r ON r.bounty_id=b.id WHERE b.checked>=? ORDER BY b.checked DESC,b.id DESC LIMIT 30""",
                                (time.time() - 2 * earnings_worker.SCAN_INTERVAL,)).fetchall()
        entries = conn.execute("SELECT * FROM whop_entries ORDER BY attempted DESC LIMIT 30").fetchall()
        used = earnings_worker.daily_count(conn)
    sign_in = f"<a href='http://localhost:{ct.PORT}/setup#whop'>Connect Whop</a>"
    if worker:
        sign_in = ("<form method=post action=/whop/disconnect>"
                   "<button class=ghost>Disconnect Whop submissions</button></form>")
    opportunities = []
    for row in bounties:
        listing = json.loads(row["payload"])
        bid = E(row["id"])
        confirmed = worker and row["worker"] == worker and row["approved"] == row["revision"]
        ready = earnings_worker.eligible_bounty(listing, time.time())
        form = ""
        if confirmed:
            form = (f"<form method=post action='/whop/brief/{bid}/disable'>"
                    "<button class=ghost>Stop automatic submission for this brief</button></form>")
        elif worker and ready:
            form = (f"<form method=post class=growth-form action='/whop/brief/{bid}'>"
                    f"<input type=hidden name=revision value='{E(row['revision'])}'>"
                    "<label>Approved creator names, separated by commas <input name=creators required maxlength=240></label>"
                    "<label>Minimum published TikTok views <input type=number name=views min=0 required></label>"
                    "<label>Minimum seconds <input type=number name=min_sec min=7 max=180 required></label>"
                    "<label>Maximum seconds <input type=number name=max_sec min=7 max=180 required></label>"
                    "<label>Maximum post age in minutes, 0 for no limit <input type=number name=max_minutes min=0 max=43200 required></label>"
                    "<label>Required hashtags <input name=tags maxlength=180></label>"
                    "<label>Required disclosure phrase <input name=disclosure maxlength=180></label>"
                    "<label><input type=checkbox name=confirmed value=true required style='width:auto'> "
                    "I checked the brief and have rights to submit these creators’ footage. This bounty accepts these "
                    "AI-assisted edits and the requirements above cover the brief.</label>"
                    "<button>Enable this brief</button></form>")
        label = "Enabled" if confirmed else "Check the brief" if ready else "Eligibility needs review"
        opportunities.append(f"<details><summary>{E(listing.get('title') or bid)} · {E(earnings_worker.reward(listing))} · {label}</summary>"
                             f"<p style='white-space:pre-wrap'>{E(listing.get('description') or '')}</p>"
                             f"<p><a href='{E(earnings_worker.bounty_url(listing))}' target=_blank rel=noopener>Open Whop</a></p>{form}</details>")
    rows = "".join(
        f"<tr><td>{E(row['day'])}</td><td>{E(row['bounty_id'])}</td><td><a href='{safe_link(row['url'])}' target=_blank rel=noopener>TikTok clip</a></td>"
        f"<td>{E(row['reward'])}</td><td>{E(row['state'])} {E(row['detail'])}</td></tr>" for row in entries)
    receipts = (f"<table><tr><th>SAST date</th><th>Bounty</th><th>Post</th><th>Listed reward</th><th>Whop status</th></tr>{rows}</table>"
                if rows else "<p>Whop submissions will appear here after a qualifying clip is sent.</p>")
    return (f"<section class='panel growth-section'><h3>Whop automation</h3><p>{E(status['detail']) if status else 'Waiting for the first scan.'}</p>"
            f"<p>{used}/{earnings_worker.DAILY_MAX} submission attempts today (SAST)</p><div class=post-actions>{sign_in}"
            "<form method=post action=/whop><button>Scan Whop now</button></form></div>"
            f"<h4>Discovered bounties</h4>{''.join(opportunities) or '<p>No matching bounties are available from the current connection.</p>'}"
            f"<h4>Submission receipts</h4>{receipts}</section>")


@app.get("/earnings")
def earnings(request: Request, msg: str = ""):
    allowed, params = ct.creator_filter(ct.creator_profile())
    with closing(ct.db()) as c:
        camps = c.execute("select * from campaigns where active=1 order by platform, id desc").fetchall()
        subs = c.execute("select s.*, c.name from submissions s left join campaigns c on c.id=s.campaign_id order by s.id desc").fetchall()
        clips = c.execute(f"SELECT id,streamer,hook FROM clips c WHERE {allowed} ORDER BY created DESC LIMIT 50", params).fetchall()
    tr = "".join(f"<tr><td>{E(x['platform'])}</td><td><a href='{safe_link(x['url'])}' target=_blank>{E(x['name'])}</a></td><td>{E(x['rate'])}</td>"
                 f"<td>{E(x['aspect'])} {E(x['min_sec'])}-{E(x['max_sec'])}s {E(x['required_tags'])}</td><td>{E(x['gta_ok'])}</td><td><small>{E((x['notes'] or '')[:200])}</small></td>"
                 f"<td><form method=post action=/campaign/{x['id']}/off><button>Hide</button></form></td></tr>" for x in camps)
    st = "".join(f"<tr><td>{E(s['date'])}</td><td>{E(s['clip_id'])}</td><td>{E(s['name'])}</td><td><a href='{safe_link(s['link'])}'>{E(s['link'])}</a></td><td>{E(s['views'])}</td><td>{E(s['payout'])}</td><td>{E(s['status'])}</td></tr>" for s in subs)
    opts = "".join(f"<option value='{E(x['id'])}'>{E(x['name'])}</option>" for x in camps)
    copts = "".join(f"<option value='{E(x['id'])}'>{E(x['streamer'])}: {E(x['hook'])}</option>" for x in clips)
    return page("Earnings", f"<h2>Earnings</h2><p role=status>{E(msg)}</p>{whop_panel()}"
                f"<h3>Campaigns</h3><table><tr><th>Platform</th><th>Campaign</th><th>Pay</th><th>Rules</th><th>GTA?</th><th>Notes</th><th></th></tr>{tr}</table>"
                "<h4>Add campaign</h4><form method=post action=/campaign>" + "".join(f"<input name={n} placeholder={n}>" for n in
                ["platform", "name", "url", "rate", "min_sec", "max_sec", "aspect", "required_tags", "gta_ok", "notes"]) + "<button>Add</button></form>"
                f"<h3>My submissions</h3><form method=post action=/submission><select name=clip_id>{copts}</select><select name=campaign_id>{opts}</select>"
                "<input name=link placeholder='posted link'><input name=views placeholder=views><input name=payout placeholder='payout (from platform)'>"
                "<select name=status><option>submitted<option>approved<option>denied<option>paid</select><button>Log</button></form>"
                f"<table><tr><th>Date</th><th>Clip</th><th>Campaign</th><th>Link</th><th>Views</th><th>Payout</th><th>Status</th></tr>{st}</table>", request)


def earnings_redirect(message):
    return RedirectResponse(f"/earnings?{urlencode({'msg': message})}", 303)

@app.post("/whop")
def whop():
    if not earnings_worker.LOCK.acquire(blocking=False):
        return earnings_redirect("Whop is already checking for bounties or submissions.")
    try:
        return earnings_redirect(earnings_worker.scan(force=True))
    finally:
        earnings_worker.LOCK.release()


@app.post("/whop/connect")
def connect_whop(request: Request):
    if request.url.hostname != "localhost":
        return RedirectResponse(f"http://localhost:{ct.PORT}/setup#whop", 303)
    try:
        url, state = whop_api.begin()
    except whop_api.WhopError as exc:
        return earnings_redirect(str(exc))
    response = RedirectResponse(url, 303)
    response.set_cookie("whop_state", state, max_age=600, httponly=True, samesite="lax", path="/whop/oauth/callback")
    return response


@app.get("/whop/oauth/callback")
def whop_oauth(request: Request, code: str = "", state: str = ""):
    try:
        whop_api.finish(code, state, request.cookies.get("whop_state"))
        message = "Whop user connected. Scan Whop and enable a brief on Earnings."
    except whop_api.WhopError as exc:
        message = str(exc)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        message = "Whop sign-in could not be completed. Try connecting again."
    response = earnings_redirect(message)
    response.delete_cookie("whop_state", path="/whop/oauth/callback")
    return response


@app.post("/whop/disconnect")
def disconnect_whop():
    whop_api.disconnect()
    return earnings_redirect("Whop user disconnected. Automatic Whop submissions are paused.")


@app.post("/whop/brief/{bid}")
def whop_brief(bid: str, revision: str = Form(), creators: str = Form(max_length=240), views: int = Form(ge=0),
               min_sec: int = Form(ge=7, le=180), max_sec: int = Form(ge=7, le=180),
               max_minutes: int = Form(ge=0, le=43200), tags: str = Form("", max_length=180),
               disclosure: str = Form("", max_length=180), confirmed: bool = Form(False)):
    if not confirmed:
        return earnings_redirect("Confirm the campaign requirements and footage rights before enabling it.")
    try:
        earnings_worker.authorize_brief(bid, revision, {"creators": creators, "views": views, "min_sec": min_sec,
            "max_sec": max_sec, "max_minutes": max_minutes, "tags": tags, "disclosure": disclosure})
    except whop_api.WhopError as exc:
        return earnings_redirect(str(exc))
    return earnings_redirect("Brief enabled. Clip Desk will submit a qualifying TikTok post when one is available.")


@app.post("/whop/brief/{bid}/disable")
def disable_whop_brief(bid: str):
    with posts.transaction() as conn:
        conn.execute("DELETE FROM whop_rules WHERE bounty_id=?", (bid,))
    return earnings_redirect("Automatic submission stopped for this brief.")

@app.post("/campaign")
def camp_add(platform: str = Form(""), name: str = Form(), url: str = Form(""), rate: str = Form(""), min_sec: str = Form("0"), max_sec: str = Form("60"),
             aspect: str = Form("9:16"), required_tags: str = Form(""), gta_ok: str = Form(""), notes: str = Form("")):
    c = ct.db(); c.execute("insert into campaigns(platform,name,url,rate,min_sec,max_sec,aspect,required_tags,required_link,gta_ok,notes) values(?,?,?,?,?,?,?,?,?,?,?)",
                           (platform, name, url, rate, int(min_sec or 0), int(max_sec or 60), aspect, required_tags, "", gta_ok, notes)); c.commit()
    return RedirectResponse("/earnings", 303)

@app.post("/campaign/{cid}/off")
def camp_off(cid: int):
    c = ct.db(); c.execute("update campaigns set active=0 where id=?", (cid,)); c.commit(); return RedirectResponse("/earnings", 303)

@app.post("/submission")
def sub_add(clip_id: str = Form(), campaign_id: int = Form(), link: str = Form(""), views: str = Form(""), payout: str = Form(""), status: str = Form("submitted")):
    allowed, params = ct.creator_filter(ct.creator_profile())
    with posts.transaction() as c:
        if not c.execute(f"SELECT 1 FROM clips c WHERE c.id=? AND {allowed}", (clip_id, *params)).fetchone():
            return earnings_redirect("This clip is unavailable.")
        c.execute("insert into submissions(clip_id,campaign_id,date,link,views,payout,status) values(?,?,?,?,?,?,?)",
                  (clip_id, campaign_id, ct.today(), link, int(views) if views.isdigit() else None, payout, status))
    return RedirectResponse("/earnings", 303)

# ---------- background jobs ----------
def scheduler():  # backfill every 3h (needs Twitch keys); nightly learning once a day after 03:30
    last_bf, last_learn = 0, ""
    while True:
        try:
            if os.getenv("TWITCH_CLIENT_ID") and time.time() - last_bf > 3 * 3600: last_bf = time.time(); ct.backfill()
            if time.strftime("%H:%M") >= "03:30" and last_learn != ct.today():
                last_learn = ct.today(); import writer; writer.nightly()
        except Exception as e: ct.log("scheduler error", e)
        time.sleep(300)


# ---------- local Setup ----------
SETUP_KEYS = [("ntfy", "Phone alerts (ntfy)", [("NTFY_TOPIC", True), ("NTFY_SERVER", False)]),
              ("twitch", "Twitch API (optional)", [("TWITCH_CLIENT_ID", False), ("TWITCH_CLIENT_SECRET", True)]), ("kick", "Kick API (optional)", [("KICK_CLIENT_ID", False), ("KICK_CLIENT_SECRET", True)]),
              ("whop", "Whop", [("WHOP_API_KEY", True), ("WHOP_ACCOUNT_ID", False), ("WHOP_COUNTRY", False),
                                ("WHOP_CLIENT_ID", False), ("WHOP_CLIENT_SECRET", True)]),
              ("tiktok", "TikTok handle", [("TIKTOK_HANDLE", False)]),
              ("autopost", "Buffer and Cloudflare R2", [(name, name in {"BUFFER_API_KEY", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"})
                                                       for name in (*autopost.CONFIG_KEYS, autopost.DELIVERY_KEY)]),
              ("research", "Public trend research", [("TIKTOK_DISCOVERY_TOKEN", True), ("TIKTOK_ADVERTISER_ID", False),
                                                       ("TIKTOK_DISCOVERY_COUNTRY", False), ("IG_RESEARCH_TOKEN", True),
                                                       ("IG_RESEARCH_USER_ID", False), ("YT_RESEARCH_KEY", True)]),
              ("ai", "Umbrel Ollama and AI writer", [("OLLAMA_URL", False), ("OLLAMA_API_TOKEN", True), ("OLLAMA_VISION_MODEL", False), ("OLLAMA_EMBED_MODEL", False),
                                                        ("OLLAMA_QUALITY_MODEL", False),
                                                        ("AI_BASE_URL", False), ("AI_API_KEY", True), ("AI_MODEL", False)])]
def checks():
    g = lambda k: bool(secret_store.value(k).strip())
    return {"ntfy": g("NTFY_TOPIC"), "twitch": g("TWITCH_CLIENT_ID") and g("TWITCH_CLIENT_SECRET"), "kick": g("KICK_CLIENT_ID") and g("KICK_CLIENT_SECRET"),
            "whop": g("WHOP_API_KEY"), "tiktok": g("TIKTOK_HANDLE"), "autopost": autopost.configured(),
            "research": growth.discovery_configured(), "ai": bool(network_policy.ollama_base() or
            (network_policy.endpoint(secret_store.value("AI_BASE_URL")) and g("AI_API_KEY"))),
            "facecam": bool(ct.watchlist().get("facecam")), "gpu": ct.ENCODER in ("nvenc", "qsv") or "nvenc" in ct.ENCODER or "qsv" in ct.ENCODER}
def qr_svg(text):
    try:
        import segno, io as _io; b = _io.BytesIO(); segno.make(text, error="m").save(b, kind="svg", scale=5, dark="#fff", light=None); return b.getvalue().decode()
    except Exception: return "<small>(pip install segno for a QR code)</small>"
@app.get("/setup")
def setup_page(request: Request, saved: str = ""):
    ok = checks(); cards = ""
    for key, title, fields in SETUP_KEYS:
        inputs = ""
        for name, secret in fields:
            val = secret_store.value(name)
            shown = ("set ••••" if val else "missing") if secret else (val or "missing")
            value = ""
            placeholder = "Paste new value (blank = keep)"
            if key == "autopost":
                shown = "Saved" if val else "Missing"
                if secret:
                    placeholder = "Replace saved key (optional)" if val else "Paste key"
                else:
                    value = f" value='{E(val)}'"
                    placeholder = {"R2_ACCOUNT_ID": "Paste Cloudflare account ID", "R2_BUCKET": "Enter R2 bucket name",
                                   autopost.DELIVERY_KEY: "Paste the public HTTPS R2 URL"}[name]
            if name == "OLLAMA_VISION_MODEL": shown = posts.vision_model() or "missing"
            if name == "OLLAMA_EMBED_MODEL" and not val: shown = "optional"
            if name == "OLLAMA_QUALITY_MODEL" and not val: shown = posts.vision_model() or "missing"
            if name == "TIKTOK_DISCOVERY_COUNTRY" and not val: shown = "US (default)"
            inputs += (f"<label><small>{name}: <b>{E(shown)}</b></small><input name={name} "
                       f"type={'password' if secret else 'text'} autocomplete=off placeholder='{placeholder}'{value} style=width:100%></label>")
        extra = ""
        help_text = ""
        if key == "ntfy" and secret_store.value("NTFY_TOPIC"):
            extra = "<form method=post action=/setup/ntfy-qr><button class=ghost>Show private subscription QR</button></form>"
        st = ok.get(key, False)
        status = "CONFIGURED" if st else "MISSING"
        if key == "tiktok":
            status = "HANDLE SAVED" if st else "HANDLE OPTIONAL"
            extra = (f"<p><a href='{TIKTOK_STUDIO}' target=_blank rel=noopener>Open TikTok Studio</a> for manual uploads.</p>"
                     "<p><a href=/autopost>Set up automatic publishing</a>.</p>")
        if key == "whop":
            connected = bool(whop_api.connected_worker())
            status = "USER CONNECTED" if connected else "ACCOUNT KEY SAVED" if st else "SETUP REQUIRED"
            extra = (f"<p>Register this exact redirect URL under OAuth in your Whop app. <code>{E(whop_api.callback_url())}</code></p>"
                     f"<p>Use <a href='http://localhost:{ct.PORT}/setup#whop'>this localhost address</a> for the one-time sign-in.</p>"
                     "<form method=post action=/whop/connect><button>Connect Whop user</button></form>"
                     "<p><a href=/earnings>View Whop bounties and submissions</a></p>")
        if key == "autopost":
            settings = autopost.get_settings()
            status = "AUTOMATIC POSTING ON" if automation_enabled(settings) else "CREDENTIALS SAVED" if st else "SETUP REQUIRED"
            help_text = "<p>Saved keys stay hidden. Leave a key field blank to keep the saved key.</p>"
            extra = ("<p>Buffer reads each staged video at the public R2 URL. Use a dedicated bucket for this clipper.</p>"
                     "<p><a href=/autopost>Check automatic posting</a></p>")
        if key == "ai":
            extra = "<p>Use your Umbrel address on a trusted private network. Use a trusted HTTPS endpoint to send an Ollama token.</p>"
        if key == "research":
            extra = ("<p>TikTok needs an advertiser ID, approved Discovery token and a two-letter audience country. "
                     "Instagram Reels needs an approved Meta token and professional Instagram user ID. YouTube needs a Data API key.</p>"
                     f"<a href='{growth.DISCOVERY_DOCS}' target=_blank rel=noopener>TikTok Discovery</a> · "
                     "<a href='https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-hashtag-search' "
                     "target=_blank rel=noopener>Instagram public content access</a> · "
                     "<a href='https://developers.google.com/youtube/v3/getting-started' target=_blank rel=noopener>YouTube Data API</a>")
        remove = (f"<form method=post action=/setup/forget><input type=hidden name=section value={key}>"
                  "<label><input type=checkbox name=confirm value=1 required> Remove this provider's saved keys</label> "
                  "<button class=ghost>Remove keys</button></form>" if any(
                      secret and secret_store.value(name) for name, secret in fields) else "")
        cards += (f"<div class=card id={key}><span class='tag {'ok' if st else 'bad'}'>{status}</span><h3>{title}</h3>{help_text}"
                  f"<form method=post action=/setup/save><input type=hidden name=section value={key}>{inputs}<button>Save on this PC</button></form>{extra}{remove}</div>")
    msg = "<p class=panel role=status>Settings saved.</p>" if saved else ""
    if ct.SECRET_LOAD_FAILURES:
        msg += "<p class=panel role=alert>Some saved credentials could not be opened or migrated. Use the original Windows account or re-enter the affected keys.</p>"
    return page("Setup", f"<h2>Setup / Connections (this PC)</h2>{msg}<p>Windows protects saved keys and Whop tokens under your Windows account. The public site receives no worker settings.</p>"
                f"<p>Facecam boxes: <a href=/watchlist>Watchlist</a>. GPU encoder in use: <b>{E(ct.ENCODER)}</b>.</p><div class=grid>{cards}</div>", request)
@app.post("/setup/ntfy-qr")
def ntfy_qr(request: Request):
    base = network_policy.endpoint(os.getenv("NTFY_SERVER", "https://ntfy.sh"))
    topic = secret_store.value("NTFY_TOPIC")
    if not base or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        return HTMLResponse("Save a private topic and an HTTPS ntfy server first.", status_code=409)
    url = f"{base}/{topic}"
    return page("Phone alerts", f"<h2>Private subscription</h2><p>Scan with the ntfy app or enter <code>{E(url)}</code>.</p>"
                f"<div style=width:180px>{qr_svg(url)}</div><p><a href=/setup#ntfy>Back to Setup</a></p>", request)


@app.post("/setup/save")
async def setup_save(request: Request):
    form = await request.form()
    section = form.get("section")
    allowed = {name for key, _, fields in SETUP_KEYS if key == section for name, _ in fields}
    is_autopost = section == "autopost"
    public_url = str(form.get(autopost.DELIVERY_KEY, "")).strip()
    if is_autopost and public_url and not autopost.delivery_base(public_url):
        return autopost_redirect(error="Enter the public HTTPS R2 base URL from the bucket's settings.")
    updates = {k: v.strip() for k, v in form.items() if k in allowed and isinstance(v, str) and v.strip()}
    for k, v in updates.items():
        if any(ch in v for ch in ("\r", "\n", "\0")) or len(v) > (4096 if k in secret_store.KEYS else 300):
            return HTMLResponse("A setting was too long or contained a line break.", status_code=400)
    for name in {"AI_BASE_URL", "NTFY_SERVER"} & updates.keys():
        if not network_policy.endpoint(updates[name]):
            return HTMLResponse("Enter a valid HTTPS service address.", status_code=400)
    if section == "ai" and {"OLLAMA_URL", "OLLAMA_API_TOKEN"} & updates.keys():
        url = updates.get("OLLAMA_URL", secret_store.value("OLLAMA_URL"))
        token = updates.get("OLLAMA_API_TOKEN", secret_store.value("OLLAMA_API_TOKEN"))
        if not network_policy.endpoint(url, local_http=not token):
            return HTMLResponse("Enter a private Ollama address or use HTTPS for a token.", status_code=400)
    if "NTFY_TOPIC" in updates and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", updates["NTFY_TOPIC"]):
        return HTMLResponse("Enter an ntfy topic with up to 64 letters, digits, underscores or dashes.", status_code=400)
    for address, credential in (("AI_BASE_URL", "AI_API_KEY"), ("OLLAMA_URL", "OLLAMA_API_TOKEN"),
                                ("NTFY_SERVER", "NTFY_TOPIC")):
        if (address in updates and updates[address] != secret_store.value(address)
                and secret_store.value(credential) and credential not in updates):
            return HTMLResponse("Re-enter the provider key or topic when changing its endpoint.", status_code=400)
    try:
        secret_store.save(ct.ROOT / ".env", updates)
        for k in updates:
            if k in ct.SECRET_LOAD_FAILURES:
                ct.SECRET_LOAD_FAILURES.remove(k)
    except (OSError, secret_store.SecretError):
        return HTMLResponse("Windows could not save a setting. Check your local installation and try again.", status_code=503)
    if is_autopost:
        if autopost.configured() and autopost.delivery_base():
            return autopost_redirect(connect=1, notice="The credentials and delivery URL are saved on this PC.")
        return autopost_redirect(error="Fill in the credentials and the public delivery URL in Setup to connect TikTok.")
    return RedirectResponse("/setup?saved=1", 303)


@app.post("/setup/forget")
def setup_forget(section: str = Form(), confirm: bool = Form(False)):
    names = {name for key, _, fields in SETUP_KEYS if key == section for name, secret in fields if secret}
    if not confirm or not names:
        return HTMLResponse("Confirm which provider keys to remove.", status_code=400)
    try:
        if section == "whop":
            whop_api.disconnect()
        secret_store.forget(ct.ROOT / ".env", names)
    except (OSError, secret_store.SecretError):
        return HTMLResponse("Windows could not remove the saved credentials. Try again.", status_code=503)
    return RedirectResponse("/setup?saved=1", 303)
def start():
    try:
        whop_api.migrate_tokens()
    except (OSError, secret_store.SecretError):
        ct.log("Whop credentials could not be migrated. Reconnect from Setup.")
    seed_campaigns()
    threading.Thread(target=lambda: asyncio.run(ct.LIVE.run()), daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()


def open_clip_desk():
    import webbrowser
    url = f"http://127.0.0.1:{ct.PORT}{ct.TODAY_PATH}"
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(30):
            try:
                response = session.get(url, timeout=1)
                if response.ok and response.headers.get(WORKER_HEADER) == "local":
                    webbrowser.open(url)
                    return
            except requests.RequestException:
                pass
            time.sleep(0.5)
    ct.log(f"Could not reach the GTA6 clip desk at {url}.")


def serve(open_browser=False):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", ct.PORT))
        except OSError as exc:
            ct.log(f"Clip Desk could not bind loopback port {ct.PORT}: {exc}")
            if open_browser:
                open_clip_desk()
            return
        if open_browser:
            threading.Thread(target=open_clip_desk, daemon=True).start()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ct.PORT, log_level="warning",
                                               proxy_headers=False, access_log=False))
        server.run(sockets=[listener])


if __name__ == "__main__":
    import sys
    if sys.stdout is None:  # pythonw (scheduled task) has no console: send output to a log file
        sys.stdout = sys.stderr = open(ct.DATA / "site.log", "a", encoding="utf-8", buffering=1)
    serve("--open" in sys.argv)

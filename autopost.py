"""Publish finished clips through a connected Buffer TikTok channel."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import os
import re
import time
from urllib.parse import quote, urlsplit
import uuid

import requests
import secret_store
import creator_rights

import cliptool as ct
import posts
import virality


BUFFER_URL = "https://api.buffer.com"
POST_FIELDS = "id status sentAt externalLink schedulingType"
CONFIG_KEYS = ("BUFFER_API_KEY", "R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
DELIVERY_KEY = "R2_PUBLIC_BASE_URL"
OLD_MEDIA_ERROR = "Invalid post: Video could not be read from its URL."
STAGING_HEADERS = {"Content-Type": "video/mp4", "x-amz-storage-class": "STANDARD"}
MONTH = 30 * 86400
BUFFER_REQUEST_LIMIT = 850
R2_MONTH_BYTES = 8_000_000_000
R2_STAGED_BYTES = 1_000_000_000
WAITING_UPLOAD = """(u.clip_id IS NULL OR (u.state='review' AND u.asset_key='')
                   OR (u.state='failed' AND u.buffer_id IS NULL AND u.asset_key='' AND u.detail=?))"""


class RateLimited(Exception):
    def __init__(self, seconds, message="Buffer quota is low. Waiting for the next quota window."):
        super().__init__(message)
        self.seconds = seconds


class CannotPublish(posts.PostError):
    pass


def configured():
    return all(secret_store.value(key).strip() for key in CONFIG_KEYS)


def delivery_base(value=None):
    value = os.getenv(DELIVERY_KEY, "") if value is None else value
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname or ""
        if (parts.scheme == "https" and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,63}", host)
                and not host.endswith((".local", ".localhost", ".internal")) and parts.netloc == host
                and parts.path in ("", "/") and not parts.query and not parts.fragment):
            return f"https://{host}"
    except ValueError:
        pass
    return ""


def delivery_url(key):
    base = delivery_base()
    if not base:
        raise CannotPublish("Save the public R2 delivery URL in Setup.")
    return f"{base}/{quote(key, safe='/-_.~')}"


def verify_delivery(url, size):
    head = requests.head(url, timeout=(6, 15), allow_redirects=False)
    if (head.status_code != 200 or head.headers.get("Content-Length") != str(size)
            or head.headers.get("Content-Type", "").split(";", 1)[0] != "video/mp4"):
        raise CannotPublish("The R2 public URL did not return this video. Check the bucket's public delivery URL.")
    end = min(size, 32) - 1
    with requests.get(url, headers={"Range": f"bytes=0-{end}"}, stream=True, timeout=(6, 15), allow_redirects=False) as reply:
        if reply.status_code != 206 or reply.headers.get("Content-Range") != f"bytes 0-{end}/{size}":
            raise CannotPublish("The public R2 URL needs to support byte ranges for Buffer.")


def quota_usage(conn, now=None):
    since = (time.time() if now is None else now) - MONTH
    requests_used = conn.execute("SELECT count(*) FROM buffer_requests WHERE at>?", (since,)).fetchone()[0]
    staged = conn.execute("SELECT coalesce(sum(bytes),0) FROM r2_staging_usage WHERE at>?", (since,)).fetchone()[0]
    return requests_used, staged


def reserve_buffer_request():
    now = time.time()
    with posts.transaction() as conn:
        wait = conn.execute("SELECT until FROM buffer_cooldown WHERE id=1").fetchone()
        if wait and wait["until"] > now:
            raise RateLimited(max(30, int(wait["until"] - now) + 1))
        conn.execute("DELETE FROM buffer_requests WHERE at<=?", (now - MONTH,))
        count, oldest = conn.execute("SELECT count(*),min(at) FROM buffer_requests").fetchone()
        if count >= BUFFER_REQUEST_LIMIT:
            raise RateLimited(max(30, int(oldest + MONTH - now) + 1), "The clipper's 30-day Buffer request budget is full.")
        conn.execute("INSERT INTO buffer_requests VALUES(?)", (now,))


def remember_buffer_limit(response):
    pauses = [int(seconds) for remaining, seconds in re.findall(r";\s*r=(\d+)\s*;\s*t=(\d+)", response.headers.get("RateLimit", ""))
              if int(remaining) <= 25]
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "300")
        pauses.append(max(60, int(retry)) if retry.isdigit() else 300)
    if not pauses:
        return 0
    delay = max(pauses) + 5
    with posts.transaction() as conn:
        conn.execute("INSERT INTO buffer_cooldown VALUES(1,?) ON CONFLICT(id) DO UPDATE SET until=max(until,excluded.until)",
                     (time.time() + delay,))
    return delay


def reserve_storage(asset, size):
    now = time.time()
    with posts.transaction() as conn:
        conn.execute("DELETE FROM r2_staging_usage WHERE retained=0 AND at<=?", (now - MONTH,))
        monthly = quota_usage(conn, now)[1]
        held = conn.execute("""SELECT coalesce(sum(size),0) FROM (SELECT max(bytes) AS size FROM r2_staging_usage
                                 WHERE retained=1 AND asset_key<>? GROUP BY asset_key)""", (asset,)).fetchone()[0]
        if monthly + size > R2_MONTH_BYTES or held + size > R2_STAGED_BYTES:
            raise RateLimited(1800, "The clipper's free-tier R2 staging budget is full. Waiting for room.")
        conn.execute("INSERT INTO r2_staging_usage(asset_key,bytes,at) VALUES(?,?,?)", (asset, size, now))


def buffer_query(query, variables=None):
    key = secret_store.value("BUFFER_API_KEY").strip()
    if not key:
        raise CannotPublish("Save a Buffer API key in Setup.")
    reserve_buffer_request()
    response = requests.post(BUFFER_URL, headers={"Authorization": f"Bearer {key}"},
                             json={"query": query, "variables": variables or {}},
                             timeout=(6, 35), allow_redirects=False)
    delay = remember_buffer_limit(response)
    if response.status_code == 429:
        raise RateLimited(delay)
    if response.status_code in (401, 403):
        raise CannotPublish("Buffer declined the API key or channel access.")
    response.raise_for_status()
    if not response.ok or response.is_redirect:
        raise ValueError("Unexpected Buffer response")
    data = response.json()
    if data.get("errors") or "data" not in data:
        raise ValueError("Unexpected Buffer response")
    return data["data"]


def channels():
    organizations = buffer_query("query { account { organizations { id name } } }")["account"]["organizations"]
    result = []
    for org in organizations:
        data = buffer_query("""query Channels($input: ChannelsInput!) {
            channels(input:$input) {id name service isDisconnected isLocked}
        }""", {"input": {"organizationId": org["id"]}})
        result.extend({"id": channel["id"], "name": channel["name"], "organization": org["id"]}
                      for channel in data["channels"] if channel["service"] == "tiktok"
                      and not channel["isDisconnected"] and not channel["isLocked"])
    return result


def sign(key, message):
    return hmac.digest(key, message.encode("utf-8"), "sha256")


def storage_url(method, object_key, *, expires=900, now=None):
    """Sign a single object operation using Cloudflare R2's S3-compatible API."""
    account, bucket, access, secret = (secret_store.value(key).strip() for key in CONFIG_KEYS[1:])
    if not (re.fullmatch(r"[0-9a-f]{32}", account) and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
            and access and secret):
        raise CannotPublish("Check the R2 account ID, bucket name and access keys in Setup.")
    host = f"{account}.r2.cloudflarestorage.com"
    path = quote(f"/{bucket}/{object_key}", safe="/-_.~")
    date = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    scope = f"{date[:8]}/auto/s3/aws4_request"
    signed_headers = "host;x-amz-storage-class" if method == "PUT" else "host"
    headers = f"host:{host}\n" + ("x-amz-storage-class:STANDARD\n" if method == "PUT" else "")
    query = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{access}/{scope}",
             "X-Amz-Date": date, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": signed_headers}
    encoded = "&".join(f"{k}={quote(v, safe='-_.~')}" for k, v in sorted(query.items()))
    canonical = f"{method}\n{path}\n{encoded}\n{headers}\n{signed_headers}\nUNSIGNED-PAYLOAD"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    signing_key = sign(sign(sign(sign(("AWS4" + secret).encode(), date[:8]), "auto"), "s3"), "aws4_request")
    signature = sign(signing_key, f"AWS4-HMAC-SHA256\n{date}\n{scope}\n{digest}").hex()
    return f"https://{host}{path}?{encoded}&X-Amz-Signature={signature}"


def get_settings():
    with closing(ct.db()) as conn:
        return conn.execute("SELECT * FROM tiktok_automation WHERE id=1").fetchone()


def check_storage():
    probe = f"gta6cliptool/setup-{uuid.uuid4().hex}.mp4"
    data = b"GTA6cliptool storage check"
    timeout = (6, 15)
    response = requests.put(storage_url("PUT", probe), data=data, headers=STAGING_HEADERS,
                            timeout=timeout, allow_redirects=False)
    if not 200 <= response.status_code < 300:
        raise CannotPublish("R2 declined the test upload. Check the bucket name and Object Read & Write keys.")
    try:
        response = requests.get(storage_url("GET", probe), timeout=timeout, allow_redirects=False)
        if response.status_code != 200 or response.content != data:
            raise CannotPublish("The clipper couldn't download from R2. Check the bucket and keys.")
        verify_delivery(delivery_url(probe), len(data))
    finally:
        response = requests.delete(storage_url("DELETE", probe), timeout=timeout, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise CannotPublish("The clipper couldn't remove its test file. Check the R2 key's Object Read & Write permissions.")


def enable(channel_id):
    if not configured():
        raise CannotPublish("Save the Buffer and R2 settings first.")
    if not delivery_base():
        raise CannotPublish("Save the public R2 delivery URL in Setup.")
    channel = next((channel for channel in channels() if channel["id"] == channel_id), None)
    if not channel:
        raise CannotPublish("Choose a connected TikTok channel in Buffer.")
    check_storage()
    with posts.transaction() as conn:
        old = conn.execute("SELECT * FROM tiktok_automation WHERE id=1").fetchone()
        if old and old["channel"] != channel_id and conn.execute(
                f"SELECT 1 FROM tiktok_uploads WHERE state IN ({posts.ACTIVE_UPLOADS}) LIMIT 1").fetchone():
            raise CannotPublish("An upload is still in progress on the previous TikTok channel.")
        conn.execute("INSERT OR REPLACE INTO tiktok_automation VALUES(1,1,?,?,?,0)",
                     (channel["organization"], channel["id"], channel["name"]))


def pause():
    with posts.transaction() as conn:
        conn.execute("UPDATE tiktok_automation SET enabled=0 WHERE id=1")


def resolve(clip_id, confirm_absent=False):
    with posts.transaction() as conn:
        row = conn.execute("SELECT state, buffer_id FROM tiktok_uploads WHERE clip_id=?", (clip_id,)).fetchone()
        if not row or row["state"] != "confirmation":
            raise CannotPublish("This clip no longer needs a confirmation.")
        state = "failed" if confirm_absent else "buffer" if row["buffer_id"] else "submitting"
        detail = "You confirmed this video was not sent. A manual upload is available." if confirm_absent else "Checking Buffer again."
        conn.execute("UPDATE tiktok_uploads SET state=?,detail=?,run_after=0 WHERE clip_id=?", (state, detail, clip_id))


def claim():
    import growth

    now = time.time()
    allowed, params = ct.creator_filter(ct.creator_profile())
    allowed = f"({allowed}) AND {creator_rights.permitted()}"
    delivery = delivery_base()
    with posts.transaction() as conn:
        settings = conn.execute("SELECT * FROM tiktok_automation WHERE id=1").fetchone()
        if not settings:
            return None
        room = hourly_room(conn, now)
        active = conn.execute(f"""SELECT u.*,c.file,c.source,c.views,c.spike,q.stamp,q.state AS quality_state,
                                   r.caption AS publish_caption,coalesce(t.mode,'regular') AS post_mode,
                                   ({allowed}) AS creator_allowed
                                   FROM tiktok_uploads u JOIN clips c ON c.id=u.clip_id LEFT JOIN clip_quality q ON q.clip_id=c.id
                                   LEFT JOIN tiktok_caption_receipts r ON r.clip_id=u.clip_id AND r.channel=u.channel
                                   LEFT JOIN tiktok_post_trials t ON t.clip_id=u.clip_id AND t.channel=u.channel
                                   WHERE u.state IN ({posts.ACTIVE_UPLOADS}) LIMIT 1""", params).fetchone()
        if active:
            if active["state"] == "uploading" and not active["creator_allowed"]:
                conn.execute("UPDATE tiktok_uploads SET state='review',detail='Creator or publishing permission unavailable.',run_after=0 WHERE clip_id=?",
                             (active["clip_id"],))
                return None
            if active["state"] == "uploading" and (active["quality_state"] != "approved" or not active["publish_caption"]):
                conn.execute("UPDATE tiktok_uploads SET state='review',detail='Waiting for quality review and a clip-specific caption.',run_after=0 WHERE clip_id=?",
                             (active["clip_id"],))
                return None
            if (active["run_after"] > now or active["state"] == "confirmation"
                    or (active["state"] == "uploading" and (not settings["enabled"] or not delivery))):
                return None
            if active["state"] == "uploading" and (sum(posts.budget(conn, posts.posting_day())) > posts.DAILY_LIMIT
                                                   or not room[active["post_mode"]]):
                return None
            conn.execute("UPDATE tiktok_uploads SET run_after=? WHERE clip_id=?", (now + 600, active["clip_id"]))
            return active
        if (not settings["enabled"] or not delivery or sum(posts.budget(conn, posts.posting_day())) >= posts.DAILY_LIMIT
                or not any(room.values())):
            return None
        candidates = conn.execute(f"""SELECT c.id,c.game,c.streamer,c.url,k.caption,k.angle,v.score,v.event,
            CASE WHEN v.score>=? THEN 'regular' ELSE 'experiment' END AS post_mode,
            CASE WHEN json_valid(c.spike) THEN unixepoch(json_extract(c.spike,'$.at')) END AS event_time
            FROM clips c
            JOIN clip_quality q ON q.clip_id=c.id AND q.state='approved'
            JOIN clip_publish_captions k ON k.clip_id=c.id AND k.stamp=q.stamp AND k.caption<>''
            JOIN clip_virality v ON v.clip_id=c.id AND v.stamp=q.stamp AND v.state='verified'
            LEFT JOIN post_drafts p ON p.clip_id=c.id LEFT JOIN tiktok_uploads u ON u.clip_id=c.id
            WHERE {allowed} AND c.posted IS NULL AND c.file<>'' AND (p.reserved_day IS NULL OR p.reserved_day<>?)
              AND {WAITING_UPLOAD}
              AND NOT EXISTS (SELECT 1 FROM takedowns t WHERE t.clip_id=c.id)
              AND ((? AND v.score>=?) OR (? AND v.score>=? AND v.score<?))
            ORDER BY post_mode='regular' DESC,v.score+v.calibration DESC,v.score DESC,c.rowid DESC""",
                                  (virality.MIN_PUBLISH_SCORE, *params, posts.posting_day(), OLD_MEDIA_ERROR,
                                   room["regular"], virality.MIN_PUBLISH_SCORE, room["experiment"],
                                   virality.MIN_EXPERIMENT_SCORE, virality.MIN_PUBLISH_SCORE)).fetchall()
        channel = settings["channel"]
        for clip in candidates:
            clip_id = clip["id"]
            if similar_moment_posted(conn, clip):
                detail = "This creator's moment overlaps a clip already posted to TikTok."
            elif posts.duplicate(clip["caption"], None, posts.publish_history(conn, channel, clip_id)):
                detail = "The caption repeats a recent TikTok post. This clip needs a new caption."
            else:
                detail = ""
            if detail:
                conn.execute("UPDATE clip_quality SET state='held',detail=? WHERE clip_id=?",
                             (detail, clip_id))
                conn.execute("UPDATE clip_virality SET state='held' WHERE clip_id=?", (clip_id,))
                continue
            asset = f"gta6cliptool/{uuid.uuid4().hex}.mp4"
            conn.execute("""INSERT OR REPLACE INTO tiktok_uploads(clip_id,organization,channel,state,asset_key,created,run_after)
                             VALUES(?,?,?,'uploading',?,?,?)""",
                         (clip_id, settings["organization"], channel, asset, now, now + 600))
            caption = creator_rights.attributed(posts.post_text(clip["caption"], clip["game"]),
                                               creator_rights.credit(conn, clip_id))
            conn.execute("INSERT OR REPLACE INTO tiktok_caption_receipts VALUES(?,?,?,?,?)",
                         (clip_id, channel, caption, clip["angle"], now))
            expected, samples = growth.prior_score_baseline(conn, channel, clip["score"], now)
            conn.execute("INSERT OR REPLACE INTO tiktok_post_trials VALUES(?,?,?,?,?,?,?)",
                         (clip_id, channel, clip["post_mode"], clip["score"], clip["event"], expected, samples))
            return conn.execute("""SELECT u.*,c.file,c.source,c.views,c.spike,q.stamp,r.caption AS publish_caption,
                                   t.mode AS post_mode
                                   FROM tiktok_uploads u JOIN clips c ON c.id=u.clip_id
                                   JOIN clip_quality q ON q.clip_id=c.id JOIN tiktok_caption_receipts r ON r.clip_id=c.id
                                   JOIN tiktok_post_trials t ON t.clip_id=c.id
                                   WHERE u.clip_id=?""", (clip_id,)).fetchone()
        return None


def hourly_room(conn, now=None):
    regular, experiments = posts.recent_post_modes(conn, now)
    return {"regular": regular < posts.HOURLY_LIMIT, "experiment": experiments < posts.EXPERIMENT_HOURLY_LIMIT}


def similar_moment_posted(conn, clip):
    url = clip["url"] or ""
    url_path = urlsplit(url).path.casefold() if url.startswith("https://") else ""
    specific_link = url if ("/clip/" in url_path or "/videos/" in url_path or "://clips.twitch.tv/" in url.casefold()) else ""
    return conn.execute("""SELECT 1 FROM clips c WHERE c.posted IS NOT NULL AND lower(c.streamer)=lower(?)
        AND ((?<>'' AND c.url=?) OR CASE WHEN json_valid(c.spike) THEN
            abs(unixepoch(json_extract(c.spike,'$.at'))-?) < 90 ELSE 0 END) LIMIT 1""",
                        (clip["streamer"], specific_link, specific_link, clip["event_time"])).fetchone() is not None


def safe_detail(detail):
    return re.sub(r"https?://\S+", "[link]", str(detail)).replace("\n", " ")[:220]


def update(clip_id, state, detail="", *, delay=0, buffer_id=None):
    with posts.transaction() as conn:
        conn.execute("""UPDATE tiktok_uploads SET state=?, detail=?, run_after=?, buffer_id=coalesce(?,buffer_id)
                        WHERE clip_id=? AND NOT (state='failed' AND detail='Takedown recorded.')""",
                     (state, safe_detail(detail), time.time() + delay, buffer_id, clip_id))


def apply_status(job, result):
    clip_id = job["clip_id"]
    buffer_id = result["id"]
    # Hold the Buffer ID even if a status response is incomplete.
    update(clip_id, "buffer", "Checking publication status.", buffer_id=buffer_id, delay=90)
    status = result["status"]
    if status == "sent" and result.get("schedulingType") == "automatic" and result.get("sentAt"):
        import growth

        published = datetime.fromisoformat(result["sentAt"].replace("Z", "+00:00")).astimezone(posts.SAST)
        url = result.get("externalLink") or ""
        if urlsplit(url).scheme != "https":
            url = ""
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET posted=?, tiktok_url=? WHERE id=? AND posted IS NULL",
                         (published.isoformat(timespec="seconds"), url, clip_id))
            conn.execute("""UPDATE tiktok_uploads SET state='sent',buffer_id=?,detail='',run_after=0
                            WHERE clip_id=?""", (buffer_id, clip_id))
            receipt = conn.execute("SELECT caption FROM tiktok_caption_receipts WHERE clip_id=? AND channel=?",
                                   (clip_id, job["channel"])).fetchone()
            caption = receipt["caption"] if receipt else result.get("text") or posts.LEGACY_TIKTOK_CAPTION
            growth.monitor_post(conn, job["channel"], result | {"text": caption}, published.timestamp(), None, {})
    elif status in ("error", "draft", "needs_approval") or result.get("schedulingType") == "notification":
        update(clip_id, "confirmation", "Check the post in Buffer. Resolve or remove it there before uploading manually.", buffer_id=buffer_id)
    else:
        detail = f"Buffer status {status.replace('_', ' ')}."
        update(clip_id, "buffer", detail, buffer_id=buffer_id,
               delay=90 if status in ("sending", "sent") else 600)


def upload_and_publish(job):
    clip_id = job["clip_id"]
    allowed, params = ct.creator_filter(ct.creator_profile())
    allowed = f"({allowed}) AND {creator_rights.permitted()}"
    current = f"""SELECT 1 FROM tiktok_uploads u JOIN clips c ON c.id=u.clip_id
                  WHERE u.clip_id=? AND u.state='uploading' AND {allowed}"""
    with closing(ct.db()) as conn:
        if not conn.execute(current, (clip_id, *params)).fetchone():
            return
    if not posts.strong_audience(job):
        detail = "The moment needs a stronger audience response for automatic posting."
        with posts.transaction() as conn:
            conn.execute("UPDATE clip_quality SET state='held',score=NULL,detail=? WHERE clip_id=?", (detail, clip_id))
        update(clip_id, "review", detail)
        return
    asset_url = delivery_url(job["asset_key"])
    path = (ct.OUT / job["file"]).resolve()
    if not path.is_relative_to(ct.OUT.resolve()) or not path.is_file():
        update(clip_id, "failed", "This clip's finished MP4 is missing.")
        return
    info = ct.edit_metadata(path)
    if path.stat().st_mtime_ns != job["stamp"] or info.get("layout") != ct.BRAND_LAYOUT or not info.get("face_visible"):
        with posts.transaction() as conn:
            conn.execute("DELETE FROM clip_quality WHERE clip_id=?", (clip_id,))
        update(clip_id, "review", "The edit changed. Checking the facecam and quality again.")
        return
    size = path.stat().st_size
    if not job["asset_url"]:
        with closing(ct.db()) as conn:
            cooldown = conn.execute("SELECT until FROM buffer_cooldown WHERE id=1").fetchone()
            if (cooldown and cooldown["until"] > time.time()) or quota_usage(conn)[0] >= BUFFER_REQUEST_LIMIT:
                raise RateLimited(1800)
        reserve_storage(job["asset_key"], size)
        with path.open("rb") as body:
            response = requests.put(storage_url("PUT", job["asset_key"]), data=body,
                                    headers=STAGING_HEADERS, timeout=(8, 180), allow_redirects=False)
        if response.status_code in (401, 403):
            raise CannotPublish("R2 declined the upload. Check the bucket's Object Read & Write keys.")
        if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
            raise CannotPublish("R2 declined the upload. Check the bucket settings.")
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise ValueError("Unexpected R2 response")
    verify_delivery(asset_url, size)
    with posts.transaction() as conn:
        if not conn.execute(current, (clip_id, *params)).fetchone():
            conn.execute("""UPDATE tiktok_uploads SET state='review',detail='Clip or permission is unavailable.',run_after=0
                            WHERE clip_id=? AND state='uploading'""", (clip_id,))
            return
        settings = conn.execute("SELECT enabled FROM tiktok_automation WHERE id=1").fetchone()
        if (not settings or not settings["enabled"] or not hourly_room(conn)[job["post_mode"]]
                or sum(posts.budget(conn, posts.posting_day())) > posts.DAILY_LIMIT):
            conn.execute("UPDATE tiktok_uploads SET asset_url=?,run_after=0 WHERE clip_id=?", (asset_url, clip_id))
            return
        conn.execute("""UPDATE tiktok_uploads SET state='submitting',asset_url=?,submitted=?,run_after=?
                        WHERE clip_id=?""", (asset_url, time.time(), time.time() + 120, clip_id))
        caption = creator_rights.attributed(job["publish_caption"], creator_rights.credit(conn, clip_id))
        conn.execute("UPDATE tiktok_caption_receipts SET caption=? WHERE clip_id=? AND channel=?",
                     (caption, clip_id, job["channel"]))
    try:
        result = buffer_query(f"""mutation PublishVideo($input: CreatePostInput!) {{
          createPost(input:$input) {{
            ... on PostActionSuccess {{ post {{ {POST_FIELDS} }} }}
            ... on MutationError {{ message }}
          }}
        }}""", {"input": {"channelId": job["channel"], "text": caption, "schedulingType": "automatic",
                          "mode": "shareNow", "needsApproval": False, "assets": [{"video": {"url": asset_url}}]}})["createPost"]
    except RateLimited as exc:
        update(clip_id, "uploading", exc, delay=exc.seconds)
    except CannotPublish as exc:
        update(clip_id, "uploading", exc)
        pause()
    except (requests.RequestException, ValueError, KeyError):
        # The write may have succeeded. Reconcile by the unique media URL before taking any other action.
        update(clip_id, "submitting", "Checking whether Buffer received the video.", delay=90)
    else:
        if result.get("post"):
            apply_status(job, result["post"])
        elif result.get("message"):
            update(clip_id, "failed", f"Buffer rejected the upload: {result['message']}")
        else:
            update(clip_id, "submitting", "Waiting to confirm the Buffer post.", delay=90)


def reconcile(job):
    start = datetime.fromtimestamp(job["submitted"] - 60, timezone.utc).isoformat()
    data = buffer_query(f"""query MatchUpload($input: PostsInput!) {{
        posts(first:50,input:$input) {{ edges {{ node {{ {POST_FIELDS} assets {{ source }} }} }} }}
    }}""", {"input": {"organizationId": job["organization"],
                      "filter": {"channelIds": [job["channel"]], "createdAt": {"start": start}}}})
    expected = urlsplit(job["asset_url"])
    for edge in data["posts"]["edges"]:
        post = edge["node"]
        for asset in post["assets"]:
            actual = urlsplit(asset.get("source") or "")
            if (actual.hostname, actual.path) == (expected.hostname, expected.path):
                apply_status(job, post)
                return
    if time.time() - job["submitted"] >= 600:
        update(job["clip_id"], "confirmation", "Check Buffer for this video before retrying or posting it manually.")
    else:
        update(job["clip_id"], "submitting", "Checking whether Buffer received the video.", delay=120)


def cleanup_media():
    now = time.time()
    with closing(ct.db()) as conn:
        job = conn.execute("""SELECT clip_id,asset_key FROM tiktok_uploads
                              WHERE (state IN ('sent','failed','review') OR (state='confirmation' AND submitted<?))
                              AND asset_key<>'' AND run_after<=? LIMIT 1""", (now - 48 * 3600, now)).fetchone()
    if not job:
        return
    try:
        response = requests.delete(storage_url("DELETE", job["asset_key"]), timeout=(6, 15), allow_redirects=False)
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise ValueError("Unexpected R2 response")
    except (requests.RequestException, CannotPublish, ValueError):
        with posts.transaction() as conn:
            conn.execute("UPDATE tiktok_uploads SET run_after=? WHERE clip_id=? AND asset_key=?",
                         (time.time() + 1800, job["clip_id"], job["asset_key"]))
    else:
        with posts.transaction() as conn:
            conn.execute("UPDATE r2_staging_usage SET retained=0 WHERE asset_key=?", (job["asset_key"],))
            conn.execute("UPDATE tiktok_uploads SET asset_key='',asset_url=NULL WHERE clip_id=? AND asset_key=?",
                         (job["clip_id"], job["asset_key"]))


def process_next():
    if not configured():
        return False
    cleanup_media()
    job = claim()
    if not job:
        return False
    try:
        if job["state"] == "uploading":
            upload_and_publish(job)
        elif job["state"] == "submitting":
            reconcile(job)
        elif job["state"] == "buffer":
            data = buffer_query(f"query PostStatus($input:PostInput!) {{post(input:$input){{{POST_FIELDS}}}}}",
                                {"input": {"id": job["buffer_id"]}})
            apply_status(job, data["post"])
    except RateLimited as exc:
        update(job["clip_id"], job["state"], exc, delay=exc.seconds)
    except CannotPublish as exc:
        update(job["clip_id"], job["state"], exc, delay=1800)
        pause()
    except (requests.RequestException, OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        ct.log("TikTok automation", job["clip_id"], type(exc).__name__)
        with posts.transaction() as conn:
            conn.execute("""UPDATE tiktok_uploads SET detail=?,run_after=? WHERE clip_id=?
                            AND NOT (state='failed' AND detail='Takedown recorded.')""",
                         ("Connection interrupted. Waiting to retry.", time.time() + 300, job["clip_id"]))
    return True


def run(stop):
    while not stop.is_set():
        try:
            process_next()
        except Exception as exc:
            ct.log("TikTok automation worker", type(exc).__name__)
        stop.wait(10)

"""Discover Whop bounties and submit verified TikTok links for approved briefs."""
from contextlib import closing
import hashlib
import json
import math
import os
import re
import threading
import time
from urllib.parse import urlsplit

import requests
import secret_store
import creator_rights

import cliptool as ct
import growth
import posts
import research
import whop_api as whop


DAILY_MAX = 3
SCAN_INTERVAL = 45 * 60
LOCK = threading.Lock()


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def brief_revision(row):
    fields = ("id", "title", "description", "scheduled_publish_at", "submissions_closed_at",
              "accepted_deliverable_types", "allowed_country_codes", "gross_reward_amount", "net_reward_amount",
              "currency", "min_total_verified_duration_seconds", "business_goal_type", "accepted_submissions_per_user_limit")
    text = json.dumps({key: row.get(key) for key in fields}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def location_ok(row):
    allowed = row.get("allowed_country_codes")
    country = os.getenv("WHOP_COUNTRY", "").strip().upper()
    return isinstance(allowed, list) and (not allowed or re.fullmatch(r"[A-Z]{2}", country) and country in allowed)


def eligible_bounty(row, now):
    sponsor = (row.get("funding_account") or {}).get("id")
    deadline = row.get("submissions_closed_at")
    budget, paid = row.get("budget_amount"), row.get("gross_paid_out_amount")
    gross = row.get("gross_reward_amount")
    spots = row.get("spots_remaining")
    return (row.get("status") == "open" and row.get("business_goal_type") == "clipping"
            and "content_url" in (row.get("accepted_deliverable_types") or ())
            and location_ok(row)
            and bool(os.getenv("WHOP_ACCOUNT_ID", "").strip()) and bool(sponsor)
            and sponsor != os.getenv("WHOP_ACCOUNT_ID", "").strip()
            and (not deadline or growth.epoch(deadline) > now)
            and type(spots) is int and spots > 0 and positive(row.get("net_reward_amount"))
            and positive(budget) and positive(gross) and type(paid) in (int, float) and 0 <= paid <= budget - gross
            and not row.get("viewer_accepted_submissions_count")
            and not row.get("min_total_verified_duration_seconds"))


def bounty_url(row):
    route = (row.get("hosting_account") or {}).get("route") or ""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", route):
        return f"https://whop.com/{route}/"
    return "https://whop.com/"


def reward(row):
    amount, currency = row.get("net_reward_amount"), row.get("currency")
    return f"{amount:g} {currency.upper()}" if positive(amount) and isinstance(currency, str) else "Unconfirmed"


def scan(*, force=False):
    with closing(ct.db()) as conn:
        prior = conn.execute("SELECT checked,detail FROM whop_scan WHERE id=1").fetchone()
    now = time.time()
    if not force and prior and now - prior["checked"] < SCAN_INTERVAL:
        return prior["detail"]
    key = secret_store.value("WHOP_API_KEY").strip()
    account = os.getenv("WHOP_ACCOUNT_ID", "").strip()
    batch = []
    try:
        user = whop.user_token()
        if user:
            token, scope, label = user[1], {}, "user sign-in"
        elif key and whop.valid_id(account, "biz"):
            token, scope, label = key, {"account_id": account}, "account key"
        else:
            token = ""
        if token:
            cursor = None
            for _ in range(3):
                params = {"business_goal_type": "clipping", "status": "open", "first": 50, **scope}
                if cursor:
                    params["after"] = cursor
                response = whop.call("bounties", token, params=params)
                batch.extend(row for row in response["data"] if isinstance(row, dict) and whop.valid_id(row.get("id"), "bnty"))
                page = response.get("page_info") or {}
                cursor = page.get("end_cursor")
                if not (page.get("has_next_page") and cursor):
                    break
            batch = [row for row in batch if re.search(r"\b(?:gta(?:\s*(?:6|vi))?|grand theft auto|gaming|twitch|streamer)\b",
                                                      f"{row.get('title', '')} {row.get('description', '')}", re.I)]
            message = f"{len(batch)} matching open Whop bounties from your {label}."
        else:
            message = "Connect Whop to scan bounties."
    except whop.WhopError as exc:
        message, batch = str(exc), []
    except (requests.RequestException, ValueError, KeyError, TypeError):
        message, batch = "Whop scan unavailable. Clip Desk will retry.", []
    with posts.transaction() as conn:
        for row in batch:
            conn.execute("INSERT OR REPLACE INTO whop_bounties VALUES(?,?,?,?)",
                         (row["id"], now, brief_revision(row), json.dumps(row, ensure_ascii=False)))
        conn.execute("INSERT OR REPLACE INTO whop_scan VALUES(1,?,?)", (now, message))
    return message


def authorize_brief(bounty_id, revision, rules):
    worker = whop.connected_worker()
    if not worker:
        raise whop.WhopError("Connect your Whop user account before enabling a brief.")
    creators = sorted({value.strip().lstrip("@").casefold() for value in rules["creators"].split(",") if value.strip()})
    tags = sorted(set(re.findall(r"#[\w]+", rules["tags"].lower())))
    if not creators or any(not re.fullmatch(r"[A-Za-z0-9_ .-]{1,45}", name) for name in creators):
        raise whop.WhopError("Enter the approved creator names, separated by commas.")
    if not (0 <= rules["views"] and 7 <= rules["min_sec"] <= rules["max_sec"] <= 180 and 0 <= rules["max_minutes"] <= 43200):
        raise whop.WhopError("Check the duration, age and view requirements.")
    rules = rules | {"creators": creators, "tags": tags, "disclosure": rules["disclosure"].strip().casefold()}
    with posts.transaction() as conn:
        row = conn.execute("SELECT revision FROM whop_bounties WHERE id=?", (bounty_id,)).fetchone()
        if not row or row["revision"] != revision:
            raise whop.WhopError("This brief changed. Reload Earnings and review it again.")
        conn.execute("INSERT OR REPLACE INTO whop_rules VALUES(?,?,?,?)",
                     (bounty_id, revision, worker, json.dumps(rules, ensure_ascii=False)))


def candidate_matches(row, rules, bounty, now):
    posted = growth.epoch(row["posted"])
    opened = growth.epoch(bounty.get("scheduled_publish_at") or bounty.get("created_at"))
    if (not posted or not opened or not opened <= posted <= now
            or rules["max_minutes"] and now - posted > rules["max_minutes"] * 60
            or row["streamer"].strip().casefold() not in rules["creators"]
            or not research.GAME.search(row["game"] or "")
            or (row["views"] or 0) < rules["views"]):
        return False
    url = urlsplit(row["tiktok_url"] or "")
    if (url.scheme != "https" or url.netloc not in ("www.tiktok.com", "tiktok.com")
            or not re.fullmatch(r"/@[\w.-]+/video/\d+/?", url.path)):
        return False
    caption = (row["publish_caption"] or "").casefold()
    if not set(rules["tags"]).issubset(re.findall(r"#[\w]+", caption)) or rules["disclosure"] not in caption:
        return False
    try:
        path = (ct.OUT / row["file"]).resolve()
        if not path.is_relative_to(ct.OUT.resolve()) or not path.is_file():
            return False
        edit = ct.edit_metadata(path)
        return bool(edit.get("layout") == ct.BRAND_LAYOUT and edit.get("face_visible")
                    and rules["min_sec"] <= float(edit["dur"]) <= rules["max_sec"])
    except (ValueError, KeyError, OSError, TypeError):
        return False


def publishable(worker, now):
    allowed, args = ct.creator_filter(ct.creator_profile())
    allowed = f"({allowed}) AND {creator_rights.permitted(monetize=True)}"
    with closing(ct.db()) as conn:
        campaigns = conn.execute("""SELECT b.*,r.rules FROM whop_bounties b JOIN whop_rules r
            ON r.bounty_id=b.id AND r.revision=b.revision AND r.worker=?
            WHERE b.checked>=? AND NOT EXISTS (SELECT 1 FROM whop_entries a WHERE a.bounty_id=b.id)
            ORDER BY b.checked DESC,b.id DESC""", (worker, now - 2 * SCAN_INTERVAL)).fetchall()
        clips = conn.execute(f"""SELECT c.id,c.file,c.streamer,c.game,c.posted,c.tiktok_url,r.caption AS publish_caption,g.views
            FROM clips c JOIN tiktok_uploads u ON u.clip_id=c.id AND u.state='sent'
            JOIN tiktok_automation a ON a.id=1 AND a.channel=u.channel
            JOIN tiktok_caption_receipts r ON r.clip_id=c.id AND r.channel=u.channel
            LEFT JOIN growth_posts g ON g.channel=u.channel AND g.buffer_id=u.buffer_id
            WHERE {allowed} AND c.posted IS NOT NULL AND c.tiktok_url<>''
              AND NOT EXISTS (SELECT 1 FROM takedowns t WHERE t.clip_id=c.id)
              AND NOT EXISTS (SELECT 1 FROM whop_entries j WHERE j.clip_id=c.id OR j.url=c.tiktok_url)
              AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.clip_id=c.id OR s.link=c.tiktok_url)
            ORDER BY g.views DESC,c.posted DESC LIMIT 500""", args).fetchall()
    choices = [(campaign, json.loads(campaign["payload"]), json.loads(campaign["rules"])) for campaign in campaigns]
    choices.sort(key=lambda x: (x[1].get("currency") != "usd", -x[1]["net_reward_amount"] if positive(
        x[1].get("net_reward_amount")) and x[1].get("currency") == "usd" else 0, x[0]["id"]))
    for campaign, bounty, rules in choices:
        if eligible_bounty(bounty, now):
            for clip in clips:
                if candidate_matches(clip, rules, bounty, now):
                    yield campaign, clip, rules


def daily_count(conn):
    return conn.execute("SELECT count(*) FROM whop_entries WHERE day=?", (posts.posting_day(),)).fetchone()[0]


def submit_next(worker, token):
    with closing(ct.db()) as conn:
        if daily_count(conn) >= DAILY_MAX:
            return
    selection = next(publishable(worker, time.time()), None)
    if not selection:
        return
    campaign, clip, rules = selection
    bounty_id = campaign["id"]
    latest = whop.call(f"bounties/{bounty_id}", token)
    now = time.time()
    if (latest.get("id") != bounty_id or brief_revision(latest) != campaign["revision"]
            or not eligible_bounty(latest, now) or not candidate_matches(clip, rules, latest, now)):
        if latest.get("id") == bounty_id:
            with posts.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO whop_bounties VALUES(?,?,?,?)",
                             (bounty_id, now, brief_revision(latest), json.dumps(latest, ensure_ascii=False)))
        return
    prior = whop.call("bounty_submissions", token, params={"bounty_id": bounty_id, "first": 25})
    if any((row.get("worker") or {}).get("id") == worker for row in prior["data"]):
        return
    allowed, params = ct.creator_filter(ct.creator_profile())
    allowed = f"({allowed}) AND {creator_rights.permitted(monetize=True)}"
    with posts.transaction() as conn:
        enabled = conn.execute("SELECT 1 FROM whop_rules WHERE bounty_id=? AND revision=? AND worker=?",
                               (bounty_id, campaign["revision"], worker)).fetchone()
        if (not enabled or daily_count(conn) >= DAILY_MAX
                or not conn.execute(f"SELECT 1 FROM clips c WHERE c.id=? AND {allowed}", (clip["id"], *params)).fetchone()):
            return
        inserted = conn.execute("""INSERT OR IGNORE INTO whop_entries(bounty_id,clip_id,worker,url,day,attempted,state,views,reward,checked)
            VALUES(?,?,?,?,?,?,'sending',?,?,?)""", (bounty_id, clip["id"], worker, clip["tiktok_url"],
            posts.posting_day(), now, clip["views"] or 0, reward(latest), now))
        if not inserted.rowcount:
            return
    key = hashlib.sha256(f"{worker}|{bounty_id}|{clip['tiktok_url']}".encode()).hexdigest()
    try:
        result = whop.call("bounty_submissions", token, key=key, body={
            "bounty_id": bounty_id, "deliverable": {"type": "content_url", "urls": [clip["tiktok_url"]]},
        })
        update_receipt(bounty_id, worker, result)
    except (whop.WhopError, requests.RequestException, ValueError, KeyError, TypeError) as exc:
        definite = isinstance(exc, whop.WhopError) and exc.status in (400, 401, 403, 404, 422, 429)
        with posts.transaction() as conn:
            conn.execute("UPDATE whop_entries SET state=?,detail=?,checked=? WHERE bounty_id=?",
                         ("rejected" if definite else "confirm", str(exc) if definite else "Check Whop for a submission receipt.",
                          time.time(), bounty_id))


def update_receipt(bounty, worker, result):
    remote = result.get("id")
    if (not whop.valid_id(remote, "btys") or result.get("bounty_id") != bounty
            or (result.get("worker") or {}).get("id") != worker):
        raise whop.WhopError("Whop returned an incomplete submission receipt.")
    state = result.get("status")
    if state not in {"in_progress", "submitted", "approved", "denied"}:
        state = "confirm"
    with posts.transaction() as conn:
        conn.execute("UPDATE whop_entries SET state=?,remote_id=?,checked=?,detail=? WHERE bounty_id=? AND worker=?",
                     (state, remote, time.time(), str(result.get("denial_reason") or "")[:240], bounty, worker))


def check_receipts(worker, token):
    with closing(ct.db()) as conn:
        rows = conn.execute("""SELECT bounty_id,remote_id,url FROM whop_entries WHERE worker=?
            AND state IN ('in_progress','submitted','sending','confirm') AND checked<? ORDER BY checked LIMIT 10""",
                            (worker, time.time() - 3600)).fetchall()
    for row in rows:
        if row["remote_id"]:
            update_receipt(row["bounty_id"], worker, whop.call(f"bounty_submissions/{row['remote_id']}", token))
            continue
        related = whop.call("bounty_submissions", token, params={"bounty_id": row["bounty_id"], "first": 25})
        match = next((item for item in related["data"] if (item.get("worker") or {}).get("id") == worker
                      and row["url"] in (item.get("deliverable_urls") or [])), None)
        if match:
            update_receipt(row["bounty_id"], worker, match)
        else:
            with posts.transaction() as conn:
                conn.execute("UPDATE whop_entries SET state='confirm',checked=?,detail=? WHERE bounty_id=?",
                             (time.time(), "Waiting for Whop to confirm this submission.", row["bounty_id"]))


def run(stop):
    while not stop.is_set():
        if LOCK.acquire(blocking=False):
            try:
                scan()
                with whop.LOCK:
                    user = whop.user_token()
                    if user:
                        check_receipts(*user)
                        submit_next(*user)
            except (whop.WhopError, requests.RequestException, ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
                ct.log("Whop earnings worker", type(exc).__name__)
            finally:
                LOCK.release()
        stop.wait(180)

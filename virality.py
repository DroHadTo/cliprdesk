"""Rank saved clips cheaply, then score footage-backed results for the publisher."""
from contextlib import closing
import math

import cliptool as ct
import posts


MIN_PUBLISH_SCORE = 86
MIN_EXPERIMENT_SCORE = 82


def audience_score(clip):
    if clip["source"] == "daily-top":
        views = max(0, clip["views"] or 0)
        return round(72 * views / 1000) if views < 1000 else min(100, round(72 + 12 * math.log2(views / 1000)))
    signal = posts.chat_response(clip)
    if signal is None:
        return 0
    count, baseline, reactions = signal
    strength = count / max(25, baseline * 2.5)
    return min(100, round(48 * min(1.4, strength) + 8 * min(reactions, 5)))


def record_preliminary(conn, clip, path, stamp):
    audience = audience_score(clip)
    score = 0
    if stamp:
        score = 14 + round(.50 * audience)
        try:
            if ct.edit_metadata(path).get("face_visible"):
                score += 20
            if posts.transcript_path(path, clip["pick"]).is_file():
                score += 10
        except OSError:
            pass  # Older synced files may have unavailable sidecars.
    conn.execute("""INSERT INTO clip_virality(clip_id,stamp,preliminary,score,state,audience)
        VALUES(?,?,?,?,?,?) ON CONFLICT(clip_id) DO UPDATE SET stamp=excluded.stamp,
        preliminary=excluded.preliminary, score=excluded.score, state=excluded.state, audience=excluded.audience,
        event='',calibration=0 WHERE clip_virality.stamp<>excluded.stamp""",
                 (clip["id"], stamp, score, score, "preliminary" if stamp else "unavailable", audience))


def index_library():
    root = ct.OUT.resolve()
    with closing(ct.db()) as conn:
        rows = conn.execute("""SELECT c.id,c.file,c.source,c.views,c.spike,c.pick,c.posted,v.stamp
            FROM clips c LEFT JOIN clip_virality v ON v.clip_id=c.id
            WHERE c.workspace='gta6' AND c.file<>'' AND NOT EXISTS (SELECT 1 FROM takedowns t WHERE t.clip_id=c.id)""").fetchall()
    updated = []
    for row in rows:
        try:
            path = (root / row["file"]).resolve()
            stamp = path.stat().st_mtime_ns if path.is_relative_to(root) and path.is_file() else 0
        except OSError:
            path, stamp = root, 0
        if row["stamp"] != stamp and (not row["posted"] or row["stamp"] is None):
            updated.append((row, path, stamp))
    if updated:
        with posts.transaction() as conn:
            for row, path, stamp in updated:
                record_preliminary(conn, row, path, stamp)


def final_score(clip, visual_score):
    return round(.80 * visual_score + .20 * audience_score(clip))


def record_result(conn, clip_id, stamp, state, score, event="", calibration=0):
    conn.execute("""UPDATE clip_virality SET state=?,score=coalesce(?,preliminary),event=?,calibration=?
        WHERE clip_id=? AND stamp=?""", (state, score, event, calibration, clip_id, stamp))

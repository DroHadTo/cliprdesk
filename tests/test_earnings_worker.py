from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import requests

import cliptool as ct
import earnings_worker as earnings
import posts


class EarningsTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        for patcher in (patch.object(ct, "DB", path / "test.db"), patch.object(ct, "OUT", path),
                        patch.object(ct, "DATA", path),
                        patch.dict("os.environ", {"WHOP_ACCOUNT_ID": "biz_mine", "WHOP_COUNTRY": "ZA"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        (path / "clip.mp4").touch()
        (path / "clip.json").write_text(json.dumps({"layout": ct.BRAND_LAYOUT, "face_visible": True, "dur": 60}))
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.bounty = {"id": "bnty_abc", "title": "GTA VI clips", "description": "GTA VI footage",
                       "status": "open", "business_goal_type": "clipping", "accepted_deliverable_types": ["content_url"],
                       "allowed_country_codes": ["ZA"], "funding_account": {"id": "biz_sponsor"},
                       "net_reward_amount": 8, "gross_reward_amount": 10, "budget_amount": 100,
                       "gross_paid_out_amount": 20, "spots_remaining": 4, "currency": "usd",
                       "created_at": (now - timedelta(days=1)).isoformat()}
        self.clip = {"id": "clip", "file": "clip.mp4", "streamer": "xQc", "game": "Grand Theft Auto VI",
                     "posted": now.isoformat(), "tiktok_url": "https://www.tiktok.com/@creator/video/123",
                     "publish_caption": "The taxi got stuck. #GTAVI #ad", "views": 1800}
        self.worker = "user_creator"
        self.rules = {"creators": ["xqc"], "views": 1000, "min_sec": 45, "max_sec": 75,
                      "max_minutes": 60, "tags": ["#gtavi"], "disclosure": "#ad"}
        self.revision = earnings.brief_revision(self.bounty)

    def test_clip_checks_real_duration_creator_and_caption(self):
        now = earnings.time.time()
        self.assertTrue(earnings.eligible_bounty(self.bounty, now))
        self.assertTrue(earnings.candidate_matches(self.clip, self.rules, self.bounty, now))
        for invalid in ({"views": 800}, {"streamer": "another"}, {"publish_caption": "#GTAVI"},
                        {"tiktok_url": "https://other.test/@creator/video/123"}, {"game": "Grand Theft Auto V"}):
            self.assertFalse(earnings.candidate_matches(self.clip | invalid, self.rules, self.bounty, now))
        self.assertFalse(earnings.eligible_bounty(self.bounty | {"allowed_country_codes": ["US"]}, now))
        self.assertFalse(earnings.eligible_bounty(self.bounty | {"funding_account": {"id": "biz_mine"}}, now))
        self.assertFalse(earnings.eligible_bounty(self.bounty | {"gross_paid_out_amount": 99}, now))

    def test_timeout_reserves_one_attempt_and_never_resends(self):
        now = earnings.time.time()
        with posts.transaction() as conn:
            conn.execute("INSERT INTO whop_bounties VALUES(?,?,?,?)",
                         (self.bounty["id"], now, self.revision, json.dumps(self.bounty)))
            conn.execute("INSERT INTO whop_rules VALUES(?,?,?,?)",
                         (self.bounty["id"], self.revision, self.worker, json.dumps(self.rules)))
            conn.execute("INSERT INTO tiktok_automation VALUES(1,1,'org','channel','TikTok',0)")
            conn.execute("""INSERT INTO clips(id,file,streamer,game,posted,tiktok_url) VALUES(?,?,?,?,?,?)""",
                         tuple(self.clip[k] for k in ("id", "file", "streamer", "game", "posted", "tiktok_url")))
            conn.execute("""INSERT INTO creator_permissions(scope,subject,credit,evidence,monetize,expires)
                            VALUES('clip',?,'Creator','Fixture permission',1,'')""", (self.clip["id"],))
            conn.execute("""INSERT INTO tiktok_uploads(clip_id,organization,channel,state,asset_key,buffer_id,created,run_after)
                            VALUES ('clip','org','channel','sent','key','buffer-id',?,0)""", (now,))
            conn.execute("INSERT INTO tiktok_caption_receipts VALUES('clip','channel',?,'action',?)",
                         (self.clip["publish_caption"], now))
            conn.execute("INSERT INTO growth_posts VALUES('channel','buffer-id',?,?,'','',1800,NULL,NULL,NULL,NULL)", (now, now))
        with patch("whop_api.call", side_effect=[self.bounty, {"data": []}, requests.Timeout()]) as api:
            earnings.submit_next(self.worker, "user-token")
            earnings.submit_next(self.worker, "user-token")
        self.assertEqual(api.call_count, 3)
        self.assertEqual(api.call_args.kwargs["body"]["deliverable"]["urls"], [self.clip["tiktok_url"]])
        self.assertTrue(api.call_args.kwargs["key"])
        with closing(ct.db()) as conn:
            self.assertEqual(earnings.daily_count(conn), 1)
            self.assertEqual(conn.execute("SELECT state FROM whop_entries").fetchone()[0], "confirm")

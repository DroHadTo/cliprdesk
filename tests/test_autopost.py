from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import unittest
from unittest.mock import Mock, patch

from dotenv import dotenv_values
from fastapi.testclient import TestClient
import requests

import app
import autopost
import cliptool as ct
import posts


class AutomaticPostingTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        for patcher in (patch.object(ct, "DB", path / "test.db"), patch.object(ct, "OUT", path), patch.object(ct, "DATA", path),
                        patch.object(autopost, "verify_delivery"),
                        patch.dict(os.environ, {autopost.DELIVERY_KEY: "https://pub-test.r2.dev"}),
                        patch.dict(os.environ, dict(zip(autopost.CONFIG_KEYS, (
                            "fake-buffer-key", "a" * 32, "test-bucket", "fake-access-key", "fake-secret-key"))))):
            patcher.start()
            self.addCleanup(patcher.stop)
        (path / "finished.mp4").write_bytes(b"finished clip")
        (path / "finished.json").write_text(json.dumps({"layout": ct.BRAND_LAYOUT, "face_visible": True}), encoding="utf-8")

    def add_clip(self, clip_id, caption="That rail sent the getaway car straight into a side quest."):
        with posts.transaction() as conn:
            conn.execute("INSERT INTO clips(id,file,streamer,source,views) VALUES(?, 'finished.mp4', 'streamer', 'daily-top', 1200)", (clip_id,))
            conn.execute("""INSERT INTO creator_permissions(scope,subject,credit,evidence,monetize,expires)
                            VALUES('clip',?,'Streamer on Twitch','Fixture permission',0,'')""", (clip_id,))
            posts.enqueue(conn, clip_id)
            stamp = (ct.OUT / "finished.mp4").stat().st_mtime_ns
            conn.execute("INSERT INTO clip_quality VALUES(?,?,'approved',90,'Clear action',0,0)", (clip_id, stamp))
            conn.execute("""INSERT INTO clip_virality(clip_id,stamp,preliminary,score,state,audience)
                VALUES(?,?,82,90,'verified',83)""", (clip_id, stamp))
            if caption:
                conn.execute("INSERT INTO clip_publish_captions VALUES(?,?,?,'action')", (clip_id, stamp, caption))

    def connect(self):
        with patch.object(autopost, "channels", return_value=[{"id": "channel", "name": "TikTok", "organization": "org"}]), \
             patch.object(autopost, "check_storage"):
            autopost.enable("channel")

    def job(self):
        with closing(ct.db()) as conn:
            return conn.execute("SELECT * FROM tiktok_uploads").fetchone()

    def make_due(self):
        with posts.transaction() as conn:
            conn.execute("UPDATE tiktok_uploads SET run_after=0")

    def sent_post(self):
        return {"id": "buffer-post", "status": "sent", "schedulingType": "automatic",
                "sentAt": datetime.now(timezone.utc).isoformat(), "externalLink": "https://www.tiktok.com/@channel/video/1"}

    def test_full_library_is_ranked_and_daily_cap_is_shared(self):
        for index in range(50):
            clip_id = f"older-{index}"
            self.add_clip(clip_id)
            posts.reserve(clip_id)
        self.connect()
        self.add_clip("new")
        with posts.transaction() as conn:
            conn.execute("UPDATE tiktok_automation SET after_clip=500")
            conn.execute("UPDATE clip_virality SET score=98 WHERE clip_id='older-1'")
        self.assertIsNone(autopost.claim())
        posts.release("older-1")
        self.assertEqual(autopost.claim()["clip_id"], "older-1")
        self.assertIsNone(autopost.claim())
        with self.assertRaises(posts.PostError):
            posts.reserve("new")
        with self.assertRaises(posts.PostError):
            posts.reserve("older-1")

    def test_rendered_video_uses_grounded_caption_and_records_delivery(self):
        self.connect()
        self.add_clip("new")
        scheduled = self.sent_post() | {"status": "scheduled", "sentAt": None}
        with patch.object(autopost.requests, "put", return_value=Mock(status_code=200)) as put, \
             patch.object(autopost, "buffer_query", side_effect=[{"createPost": {"post": scheduled}}, {"post": self.sent_post()}]) as api:
            self.assertTrue(autopost.process_next())
            body = api.call_args.args[1]["input"]
            self.assertEqual(body["text"], "That rail sent the getaway car straight into a side quest.\n#GTA #GTAClips\nCredit: Streamer on Twitch")
            self.assertEqual((body["channelId"], body["schedulingType"], body["mode"]), ("channel", "automatic", "shareNow"))
            self.assertTrue(body["assets"][0]["video"]["url"].startswith("https://pub-test.r2.dev/gta6cliptool/"))
            self.assertNotIn("?", body["assets"][0]["video"]["url"])
            self.assertEqual(self.job()["state"], "buffer")
            with closing(ct.db()) as conn:
                self.assertIsNone(conn.execute("SELECT posted FROM clips").fetchone()[0])
            self.make_due()
            autopost.pause()
            self.assertTrue(autopost.process_next())
            self.assertEqual(api.call_args.args[1], {"input": {"id": "buffer-post"}})
            put.assert_called_once()
        self.assertEqual(self.job()["state"], "sent")
        with closing(ct.db()) as conn:
            self.assertEqual(posts.budget(conn, posts.posting_day()), (1, 0))
            self.assertEqual(conn.execute("SELECT tiktok_url FROM clips").fetchone()[0], self.sent_post()["externalLink"])
            self.assertEqual(conn.execute("SELECT caption FROM monitored_posts").fetchone()[0], body["text"])
        with patch.object(autopost.requests, "delete", return_value=Mock(status_code=204)) as delete:
            self.assertFalse(autopost.process_next())
            delete.assert_called_once()
        self.assertIsNone(self.job()["asset_url"])

    def test_candidates_need_a_current_unique_caption(self):
        self.connect()
        self.add_clip("published")
        published = autopost.claim()
        autopost.apply_status(published, self.sent_post())
        self.add_clip("repeat")
        self.add_clip("legacy", caption=None)
        self.add_clip("good", caption="The motorbike just became a much less reliable escape plan.")
        self.add_clip("stale", caption="That taxi stopped at a very unexpected destination.")
        self.add_clip("below", caption="The race countdown started without anyone on the grid.")
        self.add_clip("unverified", caption="This driver decided the marina needed another boat ramp.")
        with posts.transaction() as conn:
            conn.execute("UPDATE clip_virality SET score=99 WHERE clip_id IN ('repeat','legacy','stale','unverified')")
            conn.execute("UPDATE clip_virality SET score=85 WHERE clip_id='below'")
            conn.execute("UPDATE clip_virality SET state='preliminary' WHERE clip_id='unverified'")
            conn.execute("UPDATE clip_publish_captions SET stamp=stamp-1 WHERE clip_id='stale'")
        self.assertEqual(autopost.claim()["clip_id"], "good")
        with closing(ct.db()) as conn:
            self.assertEqual(conn.execute("SELECT state FROM clip_quality WHERE clip_id='repeat'").fetchone()[0], "held")

    def test_create_timeout_reconciles_without_duplicate_post(self):
        self.connect()
        self.add_clip("new")
        with patch.object(autopost.requests, "put", return_value=Mock(status_code=200)) as put, \
             patch.object(autopost, "buffer_query", side_effect=requests.Timeout) as api:
            self.assertTrue(autopost.process_next())
            api.assert_called_once()
            put.assert_called_once()
        job = self.job()
        self.assertEqual(job["state"], "submitting")
        match = self.sent_post() | {"assets": [{"source": job["asset_url"]}]}
        self.make_due()
        with patch.object(autopost, "buffer_query", return_value={"posts": {"edges": [{"node": match}]}}) as api:
            self.assertTrue(autopost.process_next())
            self.assertIn("query MatchUpload", api.call_args.args[0])
        self.assertEqual(self.job()["state"], "sent")

    def test_incomplete_create_response_never_retries_mutation(self):
        self.connect()
        self.add_clip("new")
        incomplete = {"createPost": {"post": {"id": "existing-post"}}}
        with patch.object(autopost.requests, "put", return_value=Mock(status_code=200)), \
             patch.object(autopost, "buffer_query", return_value=incomplete):
            self.assertTrue(autopost.process_next())
        self.assertEqual((self.job()["state"], self.job()["buffer_id"]), ("buffer", "existing-post"))

    def test_retries_signed_url_failure_for_approved_creator_only(self):
        self.connect()
        self.add_clip("blocked")
        self.add_clip("approved")
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET streamer='IShowSpeed' WHERE id='approved'")
            for clip_id in ("blocked", "approved"):
                conn.execute("""INSERT INTO tiktok_uploads(clip_id,organization,channel,state,asset_key,created,run_after,detail)
                                VALUES(?, 'org', 'channel', 'failed', '', 0, 0, ?)""", (clip_id, autopost.OLD_MEDIA_ERROR))
        profile = {"ishowspeed": {"name": "IShowSpeed", "twitch": ["ishowspeed"]}}
        with patch.object(ct, "creator_profile", return_value=profile):
            self.assertEqual(autopost.claim()["clip_id"], "approved")
        self.assertEqual(autopost.delivery_base("http://localhost:8788"), "")

    def test_rolling_hour_and_quality_order(self):
        self.connect()
        for cid in ("recent-1", "recent-2", "recent-3", "lower", "best", "hold"):
            self.add_clip(cid)
        now = datetime.now(timezone.utc)
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET posted=? WHERE id LIKE 'recent-%'", ((now - timedelta(minutes=28)).isoformat(),))
            conn.execute("UPDATE clip_virality SET score=96 WHERE clip_id='best'")
            conn.execute("UPDATE clip_quality SET state='held',score=99 WHERE clip_id='hold'")
        self.assertIsNone(autopost.claim())
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET posted=? WHERE id='recent-1'", ((now - timedelta(minutes=61)).isoformat(),))
        self.assertEqual(autopost.claim()["clip_id"], "best")

    def test_fourth_hourly_slot_is_only_for_one_verified_experiment(self):
        self.connect()
        for clip_id in ("regular-1", "regular-2", "regular-3", "extra-regular", "trial", "second-trial", "too-low"):
            self.add_clip(clip_id, f"The getaway car took a shortcut during the {clip_id} moment.")
        now = datetime.now(timezone.utc)
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET posted=? WHERE id IN ('regular-1','regular-2','regular-3')",
                         ((now - timedelta(minutes=20)).isoformat(),))
            conn.execute("""UPDATE clip_virality SET score=CASE clip_id WHEN 'trial' THEN 85
                         WHEN 'second-trial' THEN 83 ELSE 81 END WHERE clip_id IN ('trial','second-trial','too-low')""")
        job = autopost.claim()
        self.assertEqual((job["clip_id"], job["post_mode"]), ("trial", "experiment"))
        autopost.apply_status(job, self.sent_post())
        self.assertIsNone(autopost.claim())
        with posts.transaction() as conn:
            self.assertEqual(posts.recent_post_modes(conn), (3, 1))
            receipt = conn.execute("SELECT * FROM tiktok_post_trials WHERE clip_id='trial'").fetchone()
            self.assertEqual((receipt["mode"], receipt["score"], receipt["baseline_samples"]), ("experiment", 85, 0))
            conn.execute("UPDATE clips SET posted=? WHERE id='trial'", ((now - timedelta(minutes=61)).isoformat(),))
        # A distinct caption is required for the next experiment.
        with posts.transaction() as conn:
            conn.execute("UPDATE clip_publish_captions SET caption=? WHERE clip_id='second-trial'",
                         ("That forklift ruined the mission before anyone could leave the garage.",))
        self.assertEqual(autopost.claim()["clip_id"], "second-trial")

    def test_stale_quality_approval_with_a_weak_audience_never_stages(self):
        self.connect()
        self.add_clip("weak")
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET source='live-buffer' WHERE id='weak'")
        with patch.object(autopost.requests, "put") as upload:
            self.assertTrue(autopost.process_next())
        upload.assert_not_called()
        self.assertEqual(self.job()["state"], "review")

    def test_quiet_hour_uses_highest_eligible_fallback_and_consumes_one_regular_slot(self):
        self.connect()
        for cid in ("lower", "highest", "no-grant", "preliminary"):
            self.add_clip(cid, f"The {cid} taxi suddenly found a new route across the plaza.")
        with posts.transaction() as conn:
            conn.execute("""UPDATE clip_virality SET state=CASE WHEN clip_id='preliminary' THEN 'preliminary' ELSE 'fallback' END,
                         score=CASE clip_id WHEN 'highest' THEN 80 WHEN 'lower' THEN 76 ELSE 99 END,
                         calibration=CASE WHEN clip_id='lower' THEN 9 ELSE 0 END""")
            conn.execute("UPDATE clips SET source='live-buffer'")
            conn.execute("DELETE FROM creator_permissions WHERE subject='no-grant'")
        job = autopost.claim()
        self.assertEqual((job["clip_id"], job["post_mode"]), ("highest", "fallback"))
        with (
            patch.object(autopost.requests, "put", return_value=Mock(status_code=200)),
            patch.object(autopost, "buffer_query", return_value={"createPost": {"post": self.sent_post()}}),
        ):
            autopost.upload_and_publish(job)
        self.assertIsNone(autopost.claim())
        with closing(ct.db()) as conn:
            self.assertEqual(posts.recent_post_modes(conn), (1, 0))

    def test_priority_posts_preempt_fallback_and_manual_post_can_close_quiet_hour(self):
        self.connect()
        self.add_clip("fallback")
        self.add_clip("priority", "The boat just outran every car on the upper bridge.")
        with posts.transaction() as conn:
            conn.execute("UPDATE clip_virality SET state='fallback',score=81 WHERE clip_id='fallback'")
        self.assertEqual(autopost.claim()["clip_id"], "priority")
        with posts.transaction() as conn:
            conn.execute("UPDATE tiktok_uploads SET state='failed' WHERE clip_id='priority'")
        job = autopost.claim()
        self.assertEqual(job["post_mode"], "fallback")
        self.make_due()
        with posts.transaction() as conn:
            conn.execute("INSERT INTO clips(id,posted) VALUES('manual',?)", (datetime.now(timezone.utc).isoformat(),))
        self.assertIsNone(autopost.claim())
        with closing(ct.db()) as conn:
            self.assertEqual(conn.execute("SELECT state FROM tiktok_uploads WHERE clip_id='fallback'").fetchone()[0], "review")

    def test_request_and_storage_budgets_reject_before_network(self):
        with patch.object(autopost, "BUFFER_REQUEST_LIMIT", 1):
            autopost.reserve_buffer_request()
            with self.assertRaises(autopost.RateLimited):
                autopost.reserve_buffer_request()
        with patch.object(autopost, "R2_MONTH_BYTES", 100):
            autopost.reserve_storage("first", 70)
            with self.assertRaises(autopost.RateLimited):
                autopost.reserve_storage("second", 31)

    def test_setup_links_channel_without_displaying_credentials(self):
        url = f"http://localhost:{ct.PORT}"
        channel = {"id": "channel", "name": "My TikTok", "organization": "org"}
        with patch.object(autopost, "channels", return_value=[channel]), \
             patch.object(autopost, "check_storage"):
            client = TestClient(app.app, base_url=url)
            self.assertIn("My TikTok", client.get("/autopost?connect=1").text)
            response = client.post("/autopost/enable", data={"channel_id": "channel"}, headers={"Origin": url})
            self.assertIn("On for My TikTok", response.text)
            for secret in ("fake-buffer-key", "fake-access-key", "fake-secret-key"):
                self.assertNotIn(secret, client.get("/setup").text)
            self.assertIn("Paused for My TikTok", client.post("/autopost/pause", headers={"Origin": url}).text)

    def test_saving_credentials_opens_channel_selection_and_shows_saved_fields(self):
        url = f"http://localhost:{ct.PORT}"
        values = dict(zip(autopost.CONFIG_KEYS, ("saved-buffer-key", "b" * 32, "gta6-tiktok-clips", "saved-access", "saved-secret")))
        with patch.object(ct, "ROOT", ct.OUT), patch("secret_store._keys", {}), \
             patch("secret_store._crypt", side_effect=lambda data, encrypt: data[::-1]), patch.object(ct, "watchlist", return_value={}):
            client = TestClient(app.app, base_url=url)
            client.get("/setup")
            response = client.post("/setup/save", data={"section": "autopost", **values},
                                   headers={"Origin": url}, follow_redirects=False)
            self.assertTrue(response.headers["location"].startswith("/autopost?connect=1"))
            stored = dotenv_values(ct.ROOT / ".env")
            for name, value in values.items():
                if name in app.secret_store.KEYS:
                    self.assertTrue(stored[name].startswith(app.secret_store.PREFIX))
                    self.assertNotEqual(stored[name], value)
                    self.assertEqual(app.secret_store.unprotect(stored[name]), value)
                    self.assertNotIn(name, os.environ)
                else:
                    self.assertEqual(stored[name], value)
            setup = client.get("/setup").text
            self.assertIn("Saved keys stay hidden", setup)
            self.assertIn("value='gta6-tiktok-clips'", setup)
            self.assertIn("BUFFER_API_KEY: <b>Saved</b>", setup)
            self.assertNotIn(values["BUFFER_API_KEY"], setup)

    def test_permission_revoked_after_claim_blocks_preflight_upload(self):
        self.connect()
        self.add_clip("revoked")
        job = autopost.claim()
        with posts.transaction() as conn:
            conn.execute("UPDATE creator_permissions SET revoked=current_timestamp")
        with patch.object(autopost.requests, "put") as upload, patch.object(autopost, "buffer_query") as provider:
            autopost.upload_and_publish(job)
            upload.assert_not_called()
            provider.assert_not_called()

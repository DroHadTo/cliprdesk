from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import time
from unittest.mock import patch

import cliptool as ct
import growth
import posts


class GrowthTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = patch.object(ct, "DB", Path(directory.name) / "test.db")
        db.start()
        self.addCleanup(db.stop)

    def test_first_day_three_result_stays_comparable_after_views_grow(self):
        sent = datetime(2026, 11, 18, tzinfo=timezone.utc)

        def observe(conn, buffer_id, age, views):
            growth.observe(conn, "channel", {"id": buffer_id, "status": "sent", "sentAt": sent.isoformat(),
                           "metricsUpdatedAt": (sent + timedelta(hours=age)).isoformat(),
                           "metrics": [{"type": "views", "value": views}]})

        with posts.transaction() as conn:
            observe(conn, "normal", 24, 500)
            observe(conn, "normal", 73, 1180)
            observe(conn, "normal", 90, 1320)
            observe(conn, "normal", 120, 5500)
            observe(conn, "too-old", 120, 6000)
            observe(conn, "normal", 74, 1190)
        with closing(ct.db()) as conn:
            results = {r["buffer_id"]: (r["views"], r["checkpoint_views"]) for r in conn.execute("SELECT * FROM growth_posts")}
        self.assertEqual(results, {"normal": (5500, 1180), "too-old": (6000, None)})

    def test_format_needs_comparable_samples_and_a_clear_lead(self):
        longer = {"choice": 75, "n": 5, "hits": 4, "median": 1400}
        shorter = {"choice": 45, "n": 5, "hits": 3, "median": 1000}
        self.assertIsNone(growth.preferred([longer]))
        self.assertIsNone(growth.preferred([longer | {"n": 4}, shorter]))
        self.assertIsNone(growth.preferred([longer, shorter | {"hits": 4}]))
        self.assertEqual(growth.preferred([longer, shorter]), 75)

    def test_experiments_compare_equal_age_results_and_need_a_real_sample(self):
        regular = [{"post_mode": "regular", "checkpoint_views": 800, "expected_views": None} for _ in range(5)]
        trials = [{"post_mode": "experiment", "checkpoint_views": 1200, "expected_views": 950} for _ in range(5)]
        waiting = [{"post_mode": "experiment", "checkpoint_views": None, "expected_views": 950}]
        summary = growth.trial_results(regular + trials + waiting, 1000)
        self.assertEqual((summary["experiment"]["samples"], summary["experiment"]["beat_forecast"]), (5, 5))
        self.assertEqual(growth.trial_calibration(summary), 3)
        self.assertEqual(growth.trial_calibration(growth.trial_results(regular + trials[:2] + waiting, 1000)), 0)

    def test_milestone_advances_when_recent_results_repeat(self):
        now = time.time()
        with posts.transaction() as conn:
            conn.execute("INSERT INTO tiktok_automation VALUES (1,1,'org','channel','Creator',0)")
            for index, views in enumerate((3100, 2800, 1500, 500, 200)):
                sent = datetime.fromtimestamp(now - (80 + index) * 3600, tz=timezone.utc)
                growth.observe(conn, "channel", {"id": str(index), "status": "sent", "sentAt": sent.isoformat(),
                               "metricsUpdatedAt": (sent + timedelta(hours=76)).isoformat(),
                               "metrics": [{"type": "views", "value": views}]})
                growth.advance_milestones(conn, "channel", now)
                count = conn.execute("SELECT count(*) FROM growth_milestones").fetchone()[0]
                self.assertEqual(count, int(index == 4))
        _, _, progress = growth.report(milestones=True)
        self.assertEqual(progress["target"], 2500)
        self.assertEqual(progress["hits"], 2)
        self.assertEqual(progress["reached"][1000]["samples"], 5)

    def test_monitor_detects_before_metrics_and_deduplicates_tiktok_posts(self):
        sent = datetime(2026, 11, 20, tzinfo=timezone.utc)
        link = "https://www.tiktok.com/@creator/video/123?lang=en"
        post = {"status": "sent", "id": "buffer-a", "sentAt": sent.isoformat(), "externalLink": link}
        with posts.transaction() as conn:
            conn.execute("INSERT INTO tiktok_automation VALUES (1,1,'org','account','Creator',0)")
            growth.observe(conn, "account", post)
            growth.observe(conn, "account", post | {"id": "buffer-b"})
        _, _, rows = growth.monitored()
        self.assertEqual([(r["post_id"],r["snapshots"],r["metrics"]) for r in rows], [("123", 0, {})])
        one = post | {"metricsUpdatedAt": (sent + timedelta(hours=1)).isoformat(),
                      "metrics": [{"type": "comments", "value": 0}, {"type": "views", "value": 12}]}
        two = one | {"metricsUpdatedAt": (sent + timedelta(hours=2)).isoformat(),
                     "metrics": [{"type": "comments", "value": 1}, {"type": "views", "value": 19}]}
        with posts.transaction() as conn:
            growth.observe(conn, "account", one)
            growth.observe(conn, "account", two)
            growth.observe(conn, "account", one)
        _, _, rows = growth.monitored()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["snapshots"], 2)
        self.assertEqual(rows[0]["metrics"], {"comments": 1, "views": 19})
        self.assertEqual(rows[0]["url"], link.split("?")[0])

    def test_reply_inbox_persists_review_and_keeps_accounts_isolated(self):
        sent = datetime(2026, 11, 20, tzinfo=timezone.utc)
        with posts.transaction() as conn:
            growth.observe(conn, "account", {"status": "sent", "id": "buffer-a", "sentAt": sent.isoformat(),
                "externalLink": "https://www.tiktok.com/@creator/video/123", "text": "Car chase"})
        growth.queue_comment("account", "123", "Where does this chase start?")
        growth.queue_comment("account", "123", "Where does this chase start?")
        [comment] = growth.reply_inbox("account")
        self.assertEqual(comment["state"], "pending")
        with self.assertRaises(posts.PostError):
            growth.review_reply("other", comment["id"], "approve", "Reply")
        with patch.object(posts, "ollama_url", return_value="http://umbrel.local:11434"), \
             patch.object(posts, "ask_ollama", return_value={"reply": "The clip opens during the chase.", "review": False}) as ai:
            growth.review_reply("account", comment["id"], "suggest")
        self.assertIn("Where does this chase start?", ai.call_args.args[1])
        growth.review_reply("account", comment["id"], "approve", "The video picks up mid-chase.")
        [approved] = growth.reply_inbox("account")
        self.assertEqual((approved["state"], approved["reply"]), ("approved", "The video picks up mid-chase."))

    def test_research_references_keep_canonical_tiktok_link(self):
        url = "https://www.tiktok.com/@gamer/video/12345?lang=en"
        growth.add_reference(url, 3200, "Opens on a car landing mid-chase.")
        self.assertEqual(growth.references()[0]["url"], url.split("?")[0])
        self.assertEqual(growth.references()[0]["views"], 3200)
        with self.assertRaises(ValueError):
            growth.add_reference("https://example.net/@gamer/video/12345", 10, "Incorrect source")

    def test_discovery_uses_ranked_gta_videos_in_a_supported_market(self):
        hashtags = [{"hashtag_name": "cooking"},
                    {"hashtag_name": "gta6", "hashtag_id": "555", "views": 1234, "top_country_list": ["GB"]}]
        videos = [{"top_video_list": [{"video_id": "123456"}, {"video_id": "invalid"}]}]
        with patch.object(growth, "discovery_get", side_effect=[hashtags, videos]) as api:
            result = growth.gta_trends("token", "42", "US")
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args.kwargs["country_code"], "GB")
        self.assertEqual(result, [{"hashtag": "gta6", "market": "GB", "hashtag_views": None,
                                   "videos": ["https://www.tiktok.com/player/v1/123456"]}])

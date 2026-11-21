import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import app
import cliptool as ct
import posts


class PostTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        for patcher in (patch.object(ct, "DB", self.path / "test.db"), patch.object(ct, "OUT", self.path), patch.object(ct, "DATA", self.path),
                        patch.object(posts, "ollama_url", return_value="http://ollama"),
                        patch.object(posts, "embedding_model", return_value="")):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.path / "video.mp4").touch()

    def add_clip(self, cid, caption=None):
        with posts.transaction() as conn:
            conn.execute("INSERT INTO clips(id, file, created, credit, game) VALUES(?, 'video.mp4', ?, 'Alice on Twitch', 'Grand Theft Auto VI')", (cid, cid))
            conn.execute("""INSERT INTO creator_permissions(scope,subject,credit,evidence,monetize,expires)
                            VALUES('clip',?,'Alice on Twitch','Fixture permission',0,'')""", (cid,))
            posts.enqueue(conn, cid)
            if caption:
                conn.execute("UPDATE post_drafts SET state='ready', caption=? WHERE clip_id=?", (caption, cid))

    def test_creator_channels_start_disabled_and_use_a_separate_library(self):
        origin = f"http://127.0.0.1:{ct.PORT}"
        client = TestClient(app.app, base_url=origin)
        client.get("/creators")
        headers = {"Origin": origin}
        form = {"platform": "kick", "source": "https://kick.com/own-channel", "permission": "own"}
        self.assertEqual(client.post("/creators/add", data=form, headers=headers).status_code, 200)
        self.assertIn("Own channel", client.get("/creators").text)
        self.assertFalse(ct.creator_enabled("kick:own-channel"))
        with patch.object(ct, "Recorder") as recorder:
            response = client.post("/live/clip", data={"channel": "kick:own-channel", "workspace": "creators"}, headers=headers)
            self.assertIn("Enable this channel", response.text)
            recorder.assert_not_called()
            client.post("/creators/enable", data={"channel": "kick:own-channel", "enabled": 1}, headers=headers)
            self.assertTrue(ct.creator_enabled("kick:own-channel"))
        client.post("/creators/enable", data={"channel": "kick:own-channel", "enabled": 0}, headers=headers)
        self.assertFalse(ct.creator_enabled("kick:own-channel"))
        self.add_clip("studio")
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET workspace='creators',hook='Studio highlight' WHERE id='studio'")
        self.assertIn("Studio highlight", client.get("/creators/library").text)
        self.assertNotIn("Studio highlight", client.get("/all").text)
        self.assertIn("Studio highlight", client.get("/latest-activity?workspace=creators").text)
        self.assertEqual(client.get("/review/studio").status_code, 200)
        self.assertEqual(posts.post_text("Studio highlight", "GTA VI", "creators"), "Studio highlight")

    def test_daily_slots_shared_across_concurrent_requests(self):
        for i in range(54):
            self.add_clip(str(i), f"A clip-specific caption for video {i}.")

        def reserve(i):
            try:
                posts.reserve(str(i))
                return str(i)
            except posts.PostError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(54)))
        reserved = [clip for clip in results if clip is not None]
        self.assertEqual(len(reserved), 50)
        posts.mark_posted(reserved[0], "")
        posts.mark_posted(reserved[0], "")
        with closing(ct.db()) as conn:
            self.assertEqual(posts.budget(conn, posts.posting_day()), (1, 49))
        posts.release(reserved[1])
        posts.reserve(str(results.index(None)))

    def test_reject_duplicate_and_keep_other_grounded_candidate(self):
        self.add_clip("first", "The orange car spins into the storefront.")
        self.add_clip("second")
        observed = "An orange car hits a storefront next to a bus."
        with patch.object(posts, "generate", return_value=(observed, [
            "The orange car spins into the storefront!", "The bus stops beside the damaged storefront.",
        ])):
            self.assertTrue(posts.process_next())
        self.assertEqual(posts.get_draft("second")["caption"], "The bus stops beside the damaged storefront.")
        with self.assertRaises(posts.PostError):
            posts.save_caption("second", 1, "The orange car spins into the storefront.")

    def test_regenerating_during_ai_run_drops_stale_caption(self):
        self.add_clip("changed")

        def generate(*_, **_kwargs):
            with posts.transaction() as conn:
                posts.enqueue(conn, "changed")
            return "The truck lands.", ["The red truck lands on the sidewalk."]

        with patch.object(posts, "generate", generate):
            self.assertTrue(posts.process_next())
        row = posts.get_draft("changed")
        self.assertEqual((row["revision"], row["state"], row["caption"]), (2, "pending", ""))

    def test_caption_prefers_gta_joke_anchored_in_scene(self):
        scene = "A white sedan stops across two lanes."
        captions = ["The sedan decided two lanes was one parking spot.",
                    "A white sedan stops across two lanes.",
                    "That sedan parked like the minimap was a suggestion.",
                    "Only in GTA would a sedan stop here."]
        self.assertEqual(posts.grounded_options(scene, captions), [captions[2], captions[0]])

    def test_clean_source_frames_use_the_original_cut_offsets(self):
        video = self.path / "video.mp4"
        raw = self.path / "raw.mp4"
        raw.touch()
        video.with_name("meta.json").write_text(json.dumps({"raw": str(raw), "start": 14, "dur": 30}),
                                                encoding="utf-8")
        with patch.object(ct, "probe_duration", return_value=30), \
             patch.object(posts.subprocess, "run", return_value=Mock(stdout=b"jpeg")) as ffmpeg:
            posts.contact_sheet(video, original_cut=True)
            raw_args = ffmpeg.call_args.args[0]
            posts.contact_sheet(video, original_cut=False)
        self.assertIn("19.400", raw_args)
        self.assertEqual(raw_args.count(str(raw)), 4)
        self.assertEqual(ffmpeg.call_args.args[0].count(str(video)), 4)

    def test_refresh_updates_old_caption_but_leaves_reserved_draft(self):
        old = "The sedan is parked in the middle of the road."
        joke = "That sedan parked like the minimap was a suggestion."
        self.add_clip("old", old)
        self.add_clip("reserved", "A truck drives through the dirt beside a building.")
        with posts.transaction() as conn:
            conn.execute("UPDATE post_drafts SET scene='A white sedan stops across two lanes.'")
        posts.reserve("reserved")
        with patch.object(posts, "generate", return_value=("A white sedan stops across two lanes.", [joke])) as writer:
            self.assertTrue(posts.refresh_ready())
            self.assertTrue(writer.call_args.kwargs["original_cut"])
        self.assertEqual(posts.get_draft("old")["caption"], joke)
        self.assertEqual(posts.get_draft("old")["state"], "ready")
        self.assertEqual(posts.get_draft("reserved")["reserved_day"], posts.posting_day())
        self.assertEqual(posts.get_draft("reserved")["caption_style"], 0)

    def test_due_retries_keep_their_place_ahead_of_new_clips(self):
        self.add_clip("a-old")
        self.add_clip("b-new")
        with posts.transaction() as conn:
            conn.execute("UPDATE post_drafts SET attempts=1,run_after=1,scene='A helicopter lands.',caption_style=? WHERE clip_id='a-old'",
                         (posts.CAPTION_STYLE,))
        with patch.object(posts, "generate", return_value=("A helicopter lands.", ["That helicopter skips the parking tutorial."])) as writer:
            self.assertTrue(posts.process_next())
            self.assertEqual(writer.call_args.args[-1], "A helicopter lands.")
        self.assertEqual(posts.get_draft("a-old")["state"], "ready")
        self.assertEqual(posts.get_draft("b-new")["state"], "pending")

    def test_http_reservation_and_download_without_tiktok_login(self):
        self.add_clip("one", "The truck lands across two lanes.")
        url = f"http://127.0.0.1:{ct.PORT}"
        client = TestClient(app.app, base_url=url)
        self.assertIn("Ready to post", client.get("/posts").text)
        self.assertIn("Post to TikTok", client.get("/posts").text)
        self.assertIn("Post to TikTok", client.get("/all").text)
        self.assertEqual(client.get("/post/one/video", follow_redirects=False).status_code, 303)
        response = client.post("/post/one/reserve", headers={"Origin": url})
        self.assertIn("Open TikTok Studio", response.text)
        self.assertIn("Continue TikTok upload", client.get("/posts").text)
        self.assertEqual(client.get("/post/one/video").headers["content-type"], "video/mp4")
        self.assertIn("Posted", client.post("/posted/one", data={"url": ""}, headers={"Origin": url}).text)

    def test_today_link_uses_its_own_route_and_legacy_home_redirects(self):
        url = f"http://localhost:{ct.PORT}"
        client = TestClient(app.app, base_url=url)
        self.assertIn('<a href="/today">Today</a>', client.get("/posts").text)
        home = client.get("/", follow_redirects=False)
        self.assertEqual(home.headers["location"], "/today")
        today = client.get(home.headers["location"])
        self.assertIn("<title>Today &middot; Clip Desk</title>", today.text)
        self.assertEqual(today.headers["cache-control"], "no-store")
        self.assertEqual(today.headers[app.WORKER_HEADER], "local")

    def test_finished_previews_and_local_authority_after_gate_removal(self):
        self.add_clip("authorised")
        (self.path / "video.mp4").write_bytes(b"finished")
        (self.path / "transcript.txt").write_text("private", encoding="utf-8")
        (self.path / "raw.mp4").write_bytes(b"source")
        self.add_clip("restricted")
        (self.path / "removed.mp4").write_bytes(b"finished")
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET file='removed.mp4',streamer='Creator' WHERE id='restricted'")
            conn.execute("INSERT INTO takedowns(clip_id) VALUES('creator')")
        origin = f"http://127.0.0.1:{ct.PORT}"
        client = TestClient(app.app, base_url=origin)
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/pair", follow_redirects=False).headers["location"], ct.TODAY_PATH)
        self.assertEqual(client.get("/today", headers={"Host": "example.test"}).status_code, 403)
        self.assertEqual(client.post("/post/authorised/reserve", headers={"Origin": origin}).status_code, 403)
        page = client.get("/today")
        self.assertIn("samesite=strict", page.headers["set-cookie"].lower())
        self.assertEqual(client.post("/post/authorised/reserve", headers={"Origin": "http://example.test"}).status_code, 403)
        video = client.get("/media/video.mp4", headers={"Range": "bytes=0-0"})
        self.assertEqual(video.status_code, 206)
        self.assertEqual(video.headers["cross-origin-resource-policy"], "same-origin")
        for name in ("transcript.txt", "raw.mp4", "removed.mp4"):
            self.assertEqual(client.get(f"/media/{name}").status_code, 404)
        self.assertEqual(client.get("/review/restricted/package").status_code, 404)

    def test_shared_activity_shows_current_videos_and_posts_on_every_tab(self):
        for clip_id in ("latest", "older", "published", "removed"):
            self.add_clip(clip_id)
        with posts.transaction() as conn:
            conn.executemany("UPDATE clips SET created=?, hook=?, streamer='Streamer' WHERE id=?", (
                ("2026-11-20T09:00:00", "Older video", "older"),
                ("2026-11-20T10:00:00", "Newest video", "latest"),
                ("2026-11-19T10:00:00", "Published clip", "published"),
                ("2026-11-20T11:00:00", "Removed clip", "removed"),
            ))
            conn.execute("UPDATE clips SET posted='2026-11-20T10:01:00+02:00', tiktok_url=? WHERE id='published'",
                         ("https://www.tiktok.com/@clipper/video/123",))
            conn.execute("INSERT INTO takedowns(clip_id) VALUES('removed')")
        client = TestClient(app.app, base_url=f"http://localhost:{ct.PORT}")
        fragment = client.get("/latest-activity").text
        videos, published = fragment.split("Latest TikTok posts", 1)
        self.assertLess(videos.index("Newest video"), videos.index("Older video"))
        self.assertNotIn("Removed clip", fragment)
        self.assertIn("Published clip", published)
        self.assertIn("https://www.tiktok.com/@clipper/video/123", published)
        for path, tab in (("/today", "Today,"), ("/posts", "TikTok queue"), ("/setup", "Setup / Connections")):
            response = client.get(path)
            self.assertIn(fragment, response.text)
            self.assertLess(response.text.index("Latest videos and posts"), response.text.index(f"<h2>{tab}"))
        self.add_clip("arrived")
        with posts.transaction() as conn:
            conn.execute("UPDATE clips SET created='2026-11-20T12:00:00', hook='Just finished' WHERE id='arrived'")
        self.assertIn("Just finished", client.get("/latest-activity").text)

    def test_creator_tabs_reorder_on_new_clip_and_hide_other_creators(self):
        profile = {"xqc": {"name": "xQc", "twitch": ["xqc"], "kick": ["xqc"]},
                   "kai": {"name": "Kai Cenat", "twitch": ["kaicenat"]}}
        for cid in ("xclip", "kclip", "outside"):
            self.add_clip(cid)
        with posts.transaction() as conn:
            conn.executemany("UPDATE clips SET streamer=?,hook=?,created=? WHERE id=?", (
                ("xQc", "xQc clip", "2026-11-20T08:00:00", "xclip"),
                ("KaiCenat", "Kai clip", "2026-11-20T09:00:00", "kclip"),
                ("SXB", "Outside the roster", "2026-11-20T10:00:00", "outside"),
            ))
        with patch.object(ct, "creator_profile", return_value=profile):
            client = TestClient(app.app, base_url=f"http://localhost:{ct.PORT}")
            route = "/creator-tabs?view=%2Fall"
            order = client.get(route).text
            self.assertLess(order.index("Kai Cenat"), order.index("xQc"))
            feed = client.get("/all?creator=kai").text.split("<nav class=creator-tabs", 1)[1]
            self.assertIn("Kai clip", feed)
            self.assertNotIn("xQc clip", feed)
            self.assertNotIn("Outside the roster", client.get("/all").text)
            self.assertNotIn("Outside the roster", client.get("/latest-activity").text)
            with self.assertRaises(ValueError):
                ct.Live().clip_moment("kick:sxb", 0)
            with posts.transaction() as conn:
                conn.execute("UPDATE clips SET created='2026-11-20T11:00:00' WHERE id='xclip'")
            order = client.get(route).text
            self.assertLess(order.index("xQc"), order.index("Kai Cenat"))

    def test_launcher_waits_for_clipper_instead_of_opening_another_service(self):
        url = f"http://127.0.0.1:{ct.PORT}/today"
        with patch.object(app.requests, "Session") as make_session, patch.object(app.time, "sleep"), \
             patch("webbrowser.open") as open_page:
            session = make_session.return_value.__enter__.return_value

            def responses():
                yield Mock(ok=True, headers={})
                open_page.assert_not_called()
                yield Mock(ok=True, headers={app.WORKER_HEADER: "local"})

            session.get.side_effect = responses()
            app.open_clip_desk()
            self.assertEqual(session.get.call_count, 2)
            open_page.assert_called_once_with(url)

    def test_apply_branding_preserves_the_ready_post_caption(self):
        self.add_clip("old", "That sedan parked like the minimap was a suggestion.")
        raw = self.path / "raw.mp4"
        raw.touch()
        (self.path / "meta.json").write_text(json.dumps({"raw": str(raw), "start": 12, "dur": 60, "peak": 40}),
                                            encoding="utf-8")
        previous = posts.get_draft("old")

        def render(src, dst, credit, **options):
            self.assertEqual(src, raw)
            self.assertEqual((options["start"], options["dur"]), (12, 60))
            dst.write_bytes(b"branded-video")
            dst.with_suffix(".json").write_text(json.dumps({"layout": ct.BRAND_LAYOUT}), encoding="utf-8")

        url = f"http://localhost:{ct.PORT}"
        with patch.object(ct, "render", side_effect=render), \
             patch.object(ct, "probe_duration", return_value=60), patch.object(ct, "captions", return_value=(None, "0")), \
             patch.object(ct, "qa", return_value=""):
            client = TestClient(app.app, base_url=url)
            self.assertIn("Apply GTA VI layout", client.get("/review/old").text)
            page = client.post("/review/old/branding", headers={"Origin": url})
            self.assertIn("GTA VI layout saved.", page.text)
            self.assertNotIn("Apply GTA VI layout", page.text)
        self.assertEqual((self.path / "video.mp4").read_bytes(), b"branded-video")
        self.assertEqual(dict(posts.get_draft("old")), dict(previous))

    def test_manual_caption_allows_upload_during_ai_draft(self):
        self.add_clip("queued")
        url = f"http://localhost:{ct.PORT}"
        caption = "That taxi found a parking space on the wrong side of the curb."
        client = TestClient(app.app, base_url=url)
        self.assertIn("Post to TikTok", client.get("/posts").text)
        client.get("/review/queued")
        blocked = client.post("/post/queued/reserve", headers={"Origin": url})
        self.assertIn("Write and save a caption", blocked.text)
        with patch.object(posts, "embed", return_value=[None]):
            ready = client.post("/post/queued/caption", headers={"Origin": url},
                                data={"caption": caption, "revision": 1, "intent": "upload"})
        self.assertIn("Open TikTok Studio", ready.text)
        self.assertIn(caption, ready.text)
        self.assertIn("#GTAVI #GTA6", ready.text)
        self.assertEqual(client.get("/post/queued/video").status_code, 200)

    def test_click_uses_recording_and_enqueues_moment_job(self):
        live = ct.Live()
        recorder = Mock(started=70, dir=self.path)
        (self.path / "seg000.ts").touch()
        live.rec["kick:streamer"] = recorder
        with patch.object(ct.time, "time", return_value=100), patch.object(ct, "watchlist", return_value={"exclude": []}), patch.object(ct.threading, "Thread") as thread:
            self.assertIn("Capturing", live.clip_moment("kick:streamer", 100))
            self.assertIn("already", live.clip_moment("kick:streamer", 101))
        args = thread.call_args.kwargs["args"]
        self.assertEqual(args[:3], ("kick:streamer", 100, recorder))
        self.assertEqual((args[3]["dur"], args[3]["peak"], args[3]["platform"]), (45, 30, "Kick"))
        recorder.has_lead.assert_called_once_with(100, 30)

    def test_mix_rotates_without_consuming_a_length_during_warmup(self):
        live = ct.Live()
        recorder = Mock(dir=self.path)
        live.rec["streamer"] = recorder
        with patch.object(ct, "watchlist", return_value={"exclude": []}), patch.object(ct.threading, "Thread") as thread:
            recorder.has_lead.return_value = False
            self.assertIn("30 seconds of lead-in", live.clip_moment("streamer", 100))
            recorder.has_lead.return_value = True
            lengths = []
            for moment in range(100, 104):
                live.manual.clear()
                live.clip_moment("streamer", moment)
                lengths.append(thread.call_args.kwargs["args"][3]["dur"])
            self.assertEqual(lengths, [45, 60, 75, 45])
            live.manual.clear()
            self.assertIn("75-second", live.clip_moment("streamer", 110, 75))
            self.assertEqual(thread.call_args.kwargs["args"][3]["peak"], 50)

    def test_automatic_live_capture_cycles_through_longer_windows(self):
        live = ct.Live()
        recorder = Mock(started=0)
        live.rec["streamer"] = recorder
        with patch.object(ct.time, "time", return_value=100), patch.object(ct.asyncio, "sleep", new_callable=AsyncMock), \
             patch.object(ct.asyncio, "to_thread", new_callable=AsyncMock) as worker:
            for _ in ct.CLIP_LENGTHS:
                asyncio.run(live.capture("streamer", 100, 20, 10, {}))
        self.assertEqual([call.args[-3:-1] for call in worker.call_args_list], [(45, 8), (60, 8), (75, 8)])

    def test_all_live_recording_covers_both_workspaces_without_duplicate_or_excluded_buffers(self):
        live = ct.Live()
        live.streams = dict.fromkeys(("one", "two", "excluded"), {})
        live.kick = {"three": {}}
        live.creator_streams = dict.fromkeys(("one", "four", "kick:five", "disabled"), {})
        sources = {channel: {"enabled": channel != "disabled"} for channel in live.creator_streams}
        with (
            patch.object(ct, "creator_profile", return_value=None),
            patch.object(ct, "creator_channels", return_value=sources),
            patch.object(ct, "stored_watchlist", return_value={"exclude": ["twitch:excluded"]}),
            patch.object(ct, "Recorder", side_effect=lambda *_: Mock()) as recorder,
        ):
            live.sync_recorders()
            self.assertEqual(set(live.rec), {"one", "two", "kick:three", "four", "kick:five"})
            self.assertEqual(live.creator_buffers, {"four", "kick:five"})
            self.assertEqual(recorder.call_count, 5)
            live.sync_recorders()
            self.assertEqual(recorder.call_count, 5)
            old = live.rec["four"]
            sources["four"]["enabled"] = False
            live.sync_recorders()
            old.stop.assert_called_once()
            self.assertNotIn("four", live.rec)

    def test_automatic_creator_capture_stays_in_its_workspace_and_checks_enrollment(self):
        live = ct.Live()
        channel = "kick:creator"
        live.creator_streams[channel] = {"name": "Creator", "game": "Other game", "title": "Other title", "viewers": 1}
        recorder = Mock(dir=self.path)
        recorder.cut.return_value = 3
        live.rec[channel] = recorder
        with patch.object(ct, "make_clip", return_value="saved") as render:
            with patch.object(ct, "creator_enabled", return_value=False):
                self.assertIsNone(live._capture(channel, 100, 50, 15, {}, 45, 8, recorder, workspace="creators"))
                render.assert_not_called()
            with patch.object(ct, "creator_enabled", return_value=True):
                self.assertEqual(live._capture(channel, 100, 50, 15, {}, 45, 8, recorder, workspace="creators"), "saved")
        item = render.call_args.args[0]
        self.assertEqual((item["workspace"], item["channel"], item["id"]), ("creators", channel, "creator-live-kick_creator-100"))

    def test_twitch_highlight_uses_longer_vod_with_native_fallback(self):
        clip = {"id": "highlight", "duration": 30, "video_id": "123", "vod_offset": 195}
        with patch.object(ct, "make_clip", side_effect=[OSError, "clip-highlight"]) as render:
            self.assertEqual(ct.make_twitch_clip(clip, {"id": "clip-highlight"}, 75), "clip-highlight")
        vod, native = [call.args[0] for call in render.call_args_list]
        self.assertEqual((vod["begin"], vod["end"], vod["dur"]), (160, 235, 75))
        self.assertEqual(native["clip_id"], "highlight")
        self.assertEqual(ct.vod_window(10, 75, 100), (0, 75))
        self.assertEqual(ct.vod_window(95, 75, 100), (25, 100))


if __name__ == "__main__":
    unittest.main()

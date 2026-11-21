from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app as desktop
import cliptool as ct


class DesktopTests(TestCase):
    def setUp(self):
        workspace = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.multiple(ct, DB=workspace / "cliptool.db", DATA=workspace, OUT=workspace))
        self.client = TestClient(desktop.app, base_url=f"http://127.0.0.1:{ct.PORT}")
        self.addCleanup(self.client.close)

    def test_library_pages_are_bounded_newest_first_and_use_on_demand_playback(self):
        with closing(ct.db()) as conn, conn:
            for index in range(26):
                conn.execute(
                    "INSERT INTO clips(id,created,hook,file,streamer) VALUES (?,?,?,?,?)",
                    (f"clip_{index}", f"2026-11-01T12:00:{index:02d}", f"Clip {index:02d}", f"clip_{index}.mp4", "streamer"),
                )
        first = self.client.get("/all")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.text.count("preload=none"), 24)
        self.assertLess(first.text.index("<h3>Clip 25</h3>"), first.text.index("<h3>Clip 24</h3>"))
        self.assertNotIn("<h3>Clip 00</h3>", first.text)
        self.assertIn('href="/all?page=2"', first.text)
        second = self.client.get("/all?page=2")
        self.assertIn("<h3>Clip 00</h3>", second.text)
        self.assertNotIn("<h3>Clip 25</h3>", second.text)
        self.assertIn("Latest videos and posts", second.text)

    def test_legacy_schema_migrates_once_and_preserves_data(self):
        legacy = ct.SCHEMA.replace(", workspace text not null default 'gta6'", "").replace(",'fallback'", "")
        with closing(sqlite3.connect(ct.DB)) as conn, conn:
            conn.executescript(legacy)
            conn.execute("INSERT INTO clips(id) VALUES('legacy')")
            conn.execute("INSERT INTO tiktok_post_trials VALUES('legacy','channel','experiment',84,'chase',1000,5)")

        def lookup(_):
            with closing(ct.db()) as conn:
                return tuple(conn.execute("SELECT workspace,swap,flags,pick FROM clips").fetchone())

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lookup, range(2))), [("gta6", None, None, None)] * 2)
        with patch.object(ct, "migrate_schema", side_effect=AssertionError("schema already initialized")):
            self.assertEqual(lookup(0)[0], "gta6")
        with closing(ct.db()) as conn, conn:
            self.assertEqual(conn.execute("SELECT * FROM tiktok_post_trials").fetchone()["mode"], "experiment")
            conn.execute("UPDATE tiktok_post_trials SET mode='fallback' WHERE clip_id='legacy'")

    def test_recorders_never_reuse_segment_files_and_failed_launch_stops_streamlink(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": str(ct.DATA)}):
            writer = Mock()
            writer.poll.return_value = None
            with patch.object(ct.subprocess, "Popen", side_effect=[writer, FileNotFoundError()]):
                with self.assertRaisesRegex(ValueError, "FFmpeg could not start"):
                    ct.Recorder("streamer")
            writer.kill.assert_called_once()
            writer.stdout.close.assert_called_once()
            with patch.object(ct.subprocess, "Popen", side_effect=[Mock(), Mock(), Mock(), Mock()]):
                first = ct.Recorder("streamer")
                second = ct.Recorder("streamer")
                self.addCleanup(first.buffer.cleanup)
                self.addCleanup(second.buffer.cleanup)
            self.assertNotEqual(first.dir, second.dir)
            self.assertTrue(first.dir.is_dir() and second.dir.is_dir())

    def test_desktop_policies_keep_scripts_external_and_writes_local(self):
        page = self.client.get("/creators")
        self.assertIn("script-src 'self';", page.headers["content-security-policy"])
        self.assertIn("src=/desk.js?v=2", page.text)
        self.assertEqual(self.client.get("/desk.js").status_code, 200)
        self.assertEqual(self.client.post("/autopost/pause", headers={"Origin": "https://example.com"}).status_code, 403)
        self.assertEqual(self.client.get("/healthz", headers={"Host": "example.com"}).status_code, 403)

    def test_live_picker_excludes_selected_channels_and_enrollment_is_explicit(self):
        origin = f"http://127.0.0.1:{ct.PORT}"
        with closing(ct.db()) as conn, conn:
            conn.execute("INSERT INTO creator_channels(channel,own) VALUES('shroud',1)")
        listings = {slug: {"user_name": slug, "title": "Live stream", "game_name": "Gaming"} for slug in ("shroud", "lirik")}
        with (
            patch.object(ct, "watchlist", return_value={"include":[],"exclude":[]}),
            patch.dict("os.environ", {"TWITCH_CLIENT_ID":""}),
            patch.object(ct, "twitch_live_nokey", side_effect=listings.get),
        ):
            self.client.get("/creators")
            result = self.client.post("/creators/discover", data={"platform":"twitch"}, headers={"Origin":origin})
            self.assertIn("value='lirik'", result.text)
            self.assertNotIn("type=checkbox name=channels value='shroud'", result.text)
            self.assertNotIn("lirik", ct.creator_channels())
            self.client.post("/creators/enroll", data={"platform":"twitch","channels":["lirik"],"permission":"permission","enabled":"true"},
                             headers={"Origin":origin})
            self.assertTrue(ct.creator_enabled("lirik"))
            self.assertFalse(ct.creator_enabled("shroud"))

    def test_ai_setup_does_not_leak_or_reuse_a_key_when_switching_providers(self):
        origin = f"http://127.0.0.1:{ct.PORT}"
        with patch.dict("os.environ", {"AI_PROVIDER":"openai","AI_API_KEY":"private-fixture-key","AI_MODEL":"fixture-text"}):
            page = self.client.get("/setup")
            self.assertIn("Anthropic (Claude)",page.text)
            self.assertIn("Custom OpenAI-compatible API",page.text)
            self.assertNotIn("private-fixture-key",page.text)
            reply = self.client.post("/setup/save", data={"section":"ai","AI_PROVIDER":"gemini"}, headers={"Origin":origin})
            self.assertEqual(reply.status_code,400)
            self.assertIn("Enter the key for the new provider",reply.text)

from hashlib import sha256
from io import BytesIO
import json
from unittest import TestCase
from zipfile import ZipFile

from fastapi.testclient import TestClient

from web import app as site


class PublicSiteTests(TestCase):
    def setUp(self):
        self.client = TestClient(site.app)

    def test_anonymous_download_and_archive_integrity(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        response = self.client.get("/downloads")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("set-cookie", response.headers)
        archive = self.client.get("/downloads/worker.zip")
        self.assertEqual(archive.status_code, 200)
        self.assertEqual(self.client.get("/downloads/worker.sha256").text.split()[0], sha256(archive.content).hexdigest())
        with ZipFile(BytesIO(archive.content)) as bundle:
            root = "GTA6cliptool/"
            manifest = json.loads(bundle.read(root + "release-manifest.json"))
            self.assertEqual(set(bundle.namelist()), {root + name for name in (*manifest, "release-manifest.json")})
            for name, digest in manifest.items():
                self.assertEqual(sha256(bundle.read(root + name)).hexdigest(), digest)
            self.assertTrue({"setup.bat", "autopost.py", "earnings_worker.py", "quality.py", "web/static/clipdesk.css"} <= manifest.keys())
            self.assertFalse({".env", "HANDOFF.md", "data/cliptool.db"} & manifest.keys())
            watchlist = json.loads(bundle.read(root + "watchlist.json"))
            self.assertEqual(watchlist["include"], [])
            self.assertEqual(watchlist["kick_include"], [])

    def test_legacy_account_and_pairing_routes_retired(self):
        self.assertEqual(self.client.get("/login", follow_redirects=False).headers["location"], "/")
        for url in ("/signup", "/tokens/new", "/api/worker/heartbeat"):
            self.assertEqual(self.client.post(url).status_code, 404)

    def test_public_style_and_security_headers(self):
        response = self.client.get("/")
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        stylesheet = self.client.get("/clipdesk.css")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("text/css", stylesheet.headers["content-type"])
        self.assertEqual(self.client.get("/preview.webp").headers["content-type"], "image/webp")

    def test_public_home_and_gta_workspace(self):
        home = self.client.get("/")
        self.assertIn("127.0.0.1:8788/creators", home.text)
        self.assertIn('href="https://github.com/DroHadTo/cliprdesk">View on GitHub', home.text)
        self.assertIn('href="https://github.com/DroHadTo/cliprdesk/blob/main/LICENSE"', home.text)
        self.assertIn('&copy; 2026 <a href="https://x.com/DroMeta">DroMeta</a>', home.text)
        self.assertNotIn(">GTA VI</div>", home.text)
        gta = self.client.get("/gta6")
        self.assertEqual(gta.status_code, 200)
        self.assertIn("127.0.0.1:8788/gta6", gta.text)

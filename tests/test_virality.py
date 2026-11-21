from contextlib import closing
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cliptool as ct
import posts
import virality


class ViralityTests(unittest.TestCase):
    def test_library_indexes_available_media_and_regrades_changed_cuts(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "saved.mp4"
            video.write_bytes(b"first version")
            with patch.object(ct, "DB", root / "test.db"), patch.object(ct, "OUT", root):
                with posts.transaction() as conn:
                    conn.execute("""INSERT INTO clips(id,file,source,views) VALUES
                        ('library','saved.mp4','daily-top',1300),('missing','deleted.mp4','daily-top',9000)""")
                virality.index_library()
                stamp = video.stat().st_mtime_ns
                with posts.transaction() as conn:
                    rows = {r["clip_id"]: r for r in conn.execute("SELECT * FROM clip_virality")}
                    self.assertEqual(rows["library"]["state"], "preliminary")
                    self.assertEqual(rows["missing"]["state"], "unavailable")
                    virality.record_result(conn, "library", stamp, "verified", 93, "crash", 3)
                virality.index_library()
                with closing(ct.db()) as conn:
                    self.assertEqual(conn.execute("SELECT score FROM clip_virality WHERE clip_id='library'").fetchone()[0], 93)
                fresh = root / "new.mp4"
                fresh.write_bytes(b"new cut")
                os.utime(fresh, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
                fresh.replace(video)
                virality.index_library()
                with closing(ct.db()) as conn:
                    edit = conn.execute("SELECT * FROM clip_virality WHERE clip_id='library'").fetchone()
                    self.assertEqual((edit["state"], edit["event"], edit["calibration"]), ("preliminary", "", 0))

    def test_combined_score_uses_visually_verified_quality_and_audience(self):
        marginal = {"source": "daily-top", "views": 1000}
        breakout = {"source": "daily-top", "views": 6000}
        self.assertLess(virality.final_score(marginal, 88), virality.MIN_PUBLISH_SCORE)
        self.assertGreaterEqual(virality.final_score(breakout, 88), virality.MIN_PUBLISH_SCORE)

    def test_inaccessible_sidecar_keeps_a_preliminary_score(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            clip = {"id": "old", "source": "daily-top", "views": 1500, "pick": "split"}
            with patch.object(ct, "DB", root / "test.db"), patch.object(ct, "edit_metadata", side_effect=PermissionError):
                with posts.transaction() as conn:
                    virality.record_preliminary(conn, clip, root / "old.mp4", 1)
                    row = conn.execute("SELECT * FROM clip_virality").fetchone()
                    self.assertEqual(row["state"], "preliminary")
                    self.assertGreater(row["score"], 0)

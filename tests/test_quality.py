import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import posts
import quality
import research


class QualityTests(unittest.TestCase):
    def test_audience_signal_requires_an_unusual_clip_specific_response(self):
        clip = {"source": "live-buffer", "views": 10000, "spike": json.dumps({
            "score": 129, "baseline": 83.7, "keywords": {"wtf": 1, "w": 10}})}
        self.assertFalse(posts.strong_audience(clip))
        clip["spike"] = json.dumps({"score": 85, "baseline": 30, "keywords": {"wtf": 2, "lmao": 1}})
        self.assertTrue(posts.strong_audience(clip))
        clip["spike"] = "{}"
        self.assertFalse(posts.strong_audience(clip))
        self.assertTrue(posts.strong_audience(clip | {"source": "daily-top", "views": 1000}))

    def test_clear_camera_and_strong_opening_are_both_required(self):
        strong = {"opening": 5, "payoff": 4, "clarity": 4, "facecam": True, "reason": "crash"}
        self.assertEqual(quality.interpret(strong)[:2], ("approved", 88))
        self.assertEqual(quality.interpret(strong | {"facecam": False})[0], "held")
        self.assertEqual(quality.interpret(strong | {"opening": 2, "payoff": 5, "clarity": 5})[0], "held")
        self.assertEqual(quality.interpret(strong | {"opening": 4})[0], "held")
        self.assertEqual(quality.interpret(strong | {"reason": "weak"})[0], "held")
        with self.assertRaises(ValueError):
            quality.interpret(strong | {"facecam": "yes"})

    def test_structured_vision_review_reads_the_chat_answer(self):
        answer = {"opening": 5, "payoff": 4, "clarity": 4, "facecam": True, "reason": "crash"}
        with patch("posts.ollama_url", return_value="http://umbrel.local:11434"), patch("network_policy.ollama_request") as request:
            request.return_value.json.return_value = {"message": {"content": json.dumps(answer)}}
            self.assertEqual(posts.ask_ollama("vision", "Review", image="frames", response_format=quality.REVIEW_SCHEMA), answer)
        self.assertEqual(request.call_args.args[:2], ("http://umbrel.local:11434", "chat"))
        body = request.call_args.args[2]
        self.assertEqual(body["messages"], [{"role": "user", "content": "Review", "images": ["frames"]}])
        self.assertEqual(body["format"], quality.REVIEW_SCHEMA)
        self.assertFalse(body["think"])

    def test_publish_caption_has_specific_evidence_and_no_fake_game_title(self):
        scene = "A motorcycle gets stuck in a garage doorway."
        caption = "The motorcycle just unlocked a garage with no exit."
        self.assertEqual(quality.grounded_caption({"scene": scene, "caption": caption}), caption)
        for bad in ("The plane just turned into the world's worst upgrade.", "Only in GTA and you really have to see it.",
                    "The motorcycle unlocked a secret garage in GTA VI.", "A motorcycle gets stuck in a garage doorway."):
            self.assertEqual(quality.grounded_caption({"scene": scene, "caption": bad}), "")

    def test_positive_score_needs_separate_evidence(self):
        review = {"reason": "crash", "scene": "A motorcycle hits a parked taxi."}
        evidence = {"opening": "active", "event": "crash", "scene": "The motorcycle hits the taxi."}
        self.assertTrue(quality.confirms(review, evidence))
        self.assertFalse(quality.confirms(review, evidence | {"opening": "ordinary"}))
        self.assertFalse(quality.confirms(review, evidence | {"event": "none"}))
        self.assertFalse(quality.confirms(review, evidence | {"scene": "A truck rams a train."}))

    def test_rewrites_use_verified_scene_and_skip_unsupported_options(self):
        scene = "A motorcycle hits a parked taxi."
        valid = "That motorcycle just turned the taxi into an optional side quest."
        evidence = {"scene": scene, "caption": "Only in GTA VI. Follow for more!"}
        with patch("posts.ask_ollama", return_value={"captions": [
            "The helicopter picked the worst spot to drop that boat.", valid,
        ]}) as model:
            self.assertEqual(quality.publish_caption("vision", evidence, []), valid)
        self.assertNotIn("image", model.call_args.kwargs)
        self.assertIn(scene, model.call_args.args[1])

    def test_research_rejects_repeated_or_uncertain_scene_notes(self):
        opening = "A motorcycle speeds toward a parked taxi."
        self.assertTrue(research.valid_observation(opening, "The motorcycle hits the taxi and tips over."))
        self.assertFalse(research.valid_observation(opening, opening))
        self.assertFalse(research.valid_observation(opening, "The rider possibly hits a second car."))

    def test_experiment_range_still_needs_second_confirmation(self):
        row = {"id": "clip", "source": "daily-top", "views": 1000, "channel": "channel", "pick": "split"}
        scene = "A motorcycle hits a parked taxi."
        review = {"opening": 4, "payoff": 4, "clarity": 5, "facecam": True, "reason": "crash", "scene": scene}
        good = {"opening": "active", "event": "crash", "scene": scene,
                "caption": "That motorcycle just turned the taxi into an optional side quest."}
        with TemporaryDirectory() as temp:
            path = Path(temp) / "clip.mp4"
            path.touch()
            stamp = path.stat().st_mtime_ns
            for evidence, expected in ((good, "approved"), (good | {"opening": "ordinary"}, "held")):
                with (
                    patch.object(quality, "source_edit", return_value=stamp),
                    patch.object(quality, "save") as save,
                    patch("quality.ct.db"),
                    patch("posts.contact_sheet", return_value="frames"),
                    patch("posts.publish_history", return_value=[]),
                    patch("posts.ask_ollama", side_effect=[review, evidence]) as model,
                    patch("growth.choose_caption_angle", return_value="action"),
                    patch("growth.virality_calibration", return_value=0),
                    patch("research.hypothesis", return_value=""),
                ):
                    quality.evaluate(row, path, stamp)
                self.assertEqual(model.call_count, 2)
                self.assertEqual((save.call_args.args[2], save.call_args.kwargs["viral_score"]), (expected, 82))

    def test_qualified_footage_can_pass_quiet_hour_without_strong_audience(self):
        row = {"id": "clip", "source": "daily-top", "views": 500, "channel": "channel", "pick": "split"}
        scene = "A motorcycle hits a parked taxi."
        review = {"opening": 4, "payoff": 4, "clarity": 5, "facecam": True, "reason": "crash", "scene": scene}
        evidence = {"opening": "active", "event": "crash", "scene": scene,
                    "caption": "That motorcycle just turned the taxi into an optional side quest."}
        with TemporaryDirectory() as temp:
            path = Path(temp) / "clip.mp4"
            path.touch()
            stamp = path.stat().st_mtime_ns
            with (
                patch.object(quality, "source_edit", return_value=stamp),
                patch.object(quality, "save") as save,
                patch("quality.ct.db"),
                patch("posts.contact_sheet", return_value="frames"),
                patch("posts.publish_history", return_value=[]),
                patch("posts.ask_ollama", side_effect=[review, evidence]) as model,
                patch("growth.choose_caption_angle", return_value="action"),
                patch("growth.virality_calibration", return_value=0),
                patch("research.hypothesis", return_value=""),
            ):
                quality.evaluate(row, path, stamp, allow_fallback=True)
            self.assertEqual(model.call_count, 2)
            self.assertEqual(save.call_args.args[2], "approved")
            self.assertTrue(save.call_args.kwargs["fallback"])
            self.assertLess(save.call_args.kwargs["viral_score"], 82)

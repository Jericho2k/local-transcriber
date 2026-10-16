import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch
import json

spec = importlib.util.spec_from_file_location("local_transcriber", Path(__file__).with_name("app.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class TranscriberTests(unittest.TestCase):
    def test_rejects_non_youtube_and_insecure_sources(self):
        for source in ["http://youtube.com/watch?v=abcdefghijk", "https://youtube.com.evil.test/watch?v=abcdefghijk",
                       "https://user:password@youtube.com/watch?v=abcdefghijk", "https://localhost/private",
                       "https://youtube.com/playlist?list=abcdefghijk"]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                app.youtube_source(source)

    def test_canonicalizes_source_without_tracking(self):
        self.assertEqual(app.youtube_source("https://youtu.be/abcdefghijk?si=tracking")[0],
                         "https://www.youtube.com/watch?v=abcdefghijk")

    def test_overlap_keeps_boundary_words_in_only_one_part(self):
        words = [Obj(start=1.7, end=1.9, word=" previous"), Obj(start=2.1, end=2.3, word=" current")]
        segment = Obj(words=words)
        got = app.owned_segments([segment], 598, 600, 1200, False)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["text"], "current")
        self.assertGreaterEqual(got[0]["start"], 600)

    def test_exports_partial_then_complete_without_duplicates(self):
        first = {"segments": [{"start": 1.125, "end": 2.25, "text": "First phrase."}]}
        second = {"segments": [{"start": 601.0, "end": 602.0, "text": "Second phrase."}]}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            app.export_results(folder, {"complete": False}, [first])
            self.assertNotIn("Second", (folder / "transcript.txt").read_text())
            app.export_results(folder, {"complete": True}, [first, second])
            self.assertEqual((folder / "transcript.txt").read_text().count("First"), 1)
            self.assertIn("00:00:01,125 --> 00:00:02,250", (folder / "transcript.srt").read_text())

    def test_last_chunk_window_is_clamped(self):
        self.assertEqual(app.chunk_window(2, 600, 1250), (1200, 1250, 1198, 1250))

    def test_lock_prevents_two_writers_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            with app.job_lock(Path(temp)):
                with self.assertRaises(ValueError):
                    with app.job_lock(Path(temp)):
                        pass
            with app.job_lock(Path(temp)):
                pass

    def test_interrupted_job_resumes_after_completed_checkpoint(self):
        class InterruptedModel:
            calls = 0

            def transcribe(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise KeyboardInterrupt()
                return iter([Obj(words=None, start=3, end=4, text="First saved phrase.")]), Obj(language="en")

        class ResumedModel:
            calls = 0

            def transcribe(self, *args, **kwargs):
                self.calls += 1
                return iter([Obj(words=None, start=3, end=4, text=f"Resumed phrase {self.calls}.")]), Obj(language="en")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "original.wav"
            source.write_bytes(b"test-media-fixture")
            args = Obj(source=str(source), language="en", chunk_seconds=30, beam_size=5,
                       output=str(root / "results"), device="cpu")
            config = {"model_sha256": "test-model-hash", "ffmpeg": "not-executed"}
            first = InterruptedModel()
            with patch.object(app, "duration_of", return_value=65), \
                 patch.object(app, "load_recognizer", return_value=(first, "cpu")), \
                 patch.object(app.subprocess, "run", return_value=Obj(returncode=0)):
                with self.assertRaises(KeyboardInterrupt):
                    app.transcribe(args, config)
            folder = next((root / "results").iterdir())
            status = json.loads((folder / "status.json").read_text())
            self.assertEqual(status["completed_parts"], 1)
            self.assertFalse(status["complete"])
            resumed = ResumedModel()
            with patch.object(app, "duration_of", return_value=65), \
                 patch.object(app, "load_recognizer", return_value=(resumed, "cpu")), \
                 patch.object(app.subprocess, "run", return_value=Obj(returncode=0)):
                app.transcribe(args, config)
            self.assertEqual(resumed.calls, 2)
            self.assertEqual((folder / "transcript.txt").read_text().count("First saved phrase."), 1)
            self.assertTrue(json.loads((folder / "status.json").read_text())["complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

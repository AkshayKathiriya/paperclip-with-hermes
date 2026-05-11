"""
test_local.py
-------------
Local integration test for the YouTube worker Flask API.

Patches the heavy pipeline modules (Pexels, Piper TTS, Whisper, MoviePy/FFmpeg)
with lightweight mocks so tests run without any external tools or API keys for
the pipeline. The Claude/Anthropic calls ARE real — set ANTHROPIC_API_KEY.

Usage:
    # run all tests
    python test_local.py

    # run a specific test
    python test_local.py TestScriptwriter

Environment variables needed:
    ANTHROPIC_API_KEY=sk-ant-...   (required for agent tests)
    PEXELS_API_KEY=...             (optional — pipeline is mocked)
"""

import os
import sys
import json
import time
import unittest
import threading
from unittest.mock import patch, MagicMock

# ── make sure we can import main without pipeline heavy deps ──────────────────

def _mock_pipeline_modules():
    """Installs lightweight stubs for faster_whisper so importing main.py
    doesn't blow up when it isn't installed locally."""
    import types

    # faster_whisper stub
    fw_stub = types.ModuleType("faster_whisper")
    class _FakeSeg:
        start = 0.0; end = 2.0; text = "Hello world."
    fw_stub.WhisperModel = lambda *a, **kw: MagicMock(
        transcribe=lambda *a, **kw: ([_FakeSeg()], MagicMock())
    )
    sys.modules.setdefault("faster_whisper", fw_stub)

_mock_pipeline_modules()


# ── patch pipeline callables before importing main ────────────────────────────
with patch("pipeline.fetch_pexels.fetch_pexels_videos", return_value=[None] * 6), \
     patch("pipeline.tts.generate_voiceover", return_value="/tmp/narration.wav"), \
     patch("pipeline.subtitles.generate_subtitles", return_value="/tmp/subtitles.srt"), \
     patch("pipeline.assemble.assemble_video", return_value="/tmp/video.mp4"):
    import main as worker_app


# ── test client ───────────────────────────────────────────────────────────────
client = worker_app.app.test_client()
client.testing = True


# ─────────────────────────────────────────────────────────────────────────────
# Test suites
# ─────────────────────────────────────────────────────────────────────────────

class TestHealth(unittest.TestCase):
    def test_health_returns_ok(self):
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("active_jobs", body)
        self.assertIn("total_jobs", body)
        print(f"  /health → {body}")


class TestHelpers(unittest.TestCase):
    """Unit tests for internal helpers — no network needed."""

    def test_parse_json_clean(self):
        result = worker_app._parse_json('{"a": 1}')
        self.assertEqual(result, {"a": 1})

    def test_parse_json_strips_fences(self):
        text = '```json\n{"a": 1}\n```'
        result = worker_app._parse_json(text)
        self.assertEqual(result, {"a": 1})

    def test_parse_json_strips_fence_no_lang(self):
        text = '```\n{"b": 2}\n```'
        result = worker_app._parse_json(text)
        self.assertEqual(result, {"b": 2})

    def test_extract_field_found(self):
        desc = "Topic: Black holes\nStyle: dramatic"
        self.assertEqual(worker_app._extract_field(desc, "topic"), "Black holes")
        self.assertEqual(worker_app._extract_field(desc, "style"), "dramatic")

    def test_extract_field_case_insensitive(self):
        desc = "TOPIC: Quantum physics"
        self.assertEqual(worker_app._extract_field(desc, "topic"), "Quantum physics")

    def test_extract_field_missing_returns_default(self):
        self.assertEqual(worker_app._extract_field("nothing here", "topic", "fallback"), "fallback")


class TestScriptwriter(unittest.TestCase):
    """Tests Agent 1 — requires ANTHROPIC_API_KEY."""

    def test_missing_topic_returns_400(self):
        r = client.post("/agent/scriptwriter",
                        json={"task": {"title": "", "description": ""}})
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn("error", body)

    def test_topic_from_title(self):
        r = client.post("/agent/scriptwriter",
                        json={"task": {"title": "The James Webb Telescope", "description": ""}})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "done")
        self.assertIn("result", body)
        result = body["result"]
        self.assertIn("script", result)
        self.assertIn("hook", result)
        self.assertIn("sections", result)
        self.assertIsInstance(result["sections"], list)
        print(f"  Scriptwriter title: '{result.get('title_suggestion','?')}'")
        print(f"  Word count: {result.get('word_count','?')}")

    def test_topic_from_description_field(self):
        desc = "topic: Ocean deep sea creatures\nstyle: entertaining"
        r = client.post("/agent/scriptwriter",
                        json={"task": {"title": "", "description": desc}})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "done")
        self.assertIn("script", body["result"])


class TestSceneDirector(unittest.TestCase):
    """Tests Agent 2 — requires ANTHROPIC_API_KEY."""

    SAMPLE_SCRIPT = (
        "Did you know that 95% of the ocean remains unexplored? Today we're diving deep. "
        "[SECTION: The Abyss] The hadal zone sits more than 6000 metres below the surface. "
        "Pressure there is 600 times atmospheric. [SECTION: Creatures] Anglerfish lure prey "
        "with bioluminescent lights. Giant squids battle sperm whales in the dark. "
        "[SECTION: Technology] ROVs equipped with HD cameras now let us see the abyss live. "
        "[SECTION: Discoveries] New species are found on almost every deep-sea expedition. "
        "[SECTION: CTA] Subscribe for more ocean science every week."
    )

    def test_missing_script_returns_400(self):
        r = client.post("/agent/scene-director",
                        json={"task": {"title": "Test"}, "context": {}})
        self.assertEqual(r.status_code, 400)

    def test_scenes_from_context(self):
        r = client.post("/agent/scene-director", json={
            "task": {"title": "Deep Sea Creatures"},
            "context": {"script": {"script": self.SAMPLE_SCRIPT}}
        })
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "done")
        scenes = body["result"].get("scenes", [])
        self.assertEqual(len(scenes), 6, f"Expected 6 scenes, got {len(scenes)}")
        for s in scenes:
            self.assertIn("pexels_search_query", s)
            self.assertIn("duration_seconds", s)
        print(f"  Scene queries: {[s['pexels_search_query'] for s in scenes]}")

    def test_script_as_string_in_context(self):
        r = client.post("/agent/scene-director", json={
            "task": {"title": "Deep Sea"},
            "context": {"script": self.SAMPLE_SCRIPT}
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "done")


class TestProductionManager(unittest.TestCase):
    """Tests Agent 3 — requires ANTHROPIC_API_KEY. Pipeline calls are mocked."""

    SAMPLE_SCENES = [
        {"num": "01", "pexels_search_query": "ocean deep sea", "duration_seconds": 20},
        {"num": "02", "pexels_search_query": "anglerfish bioluminescent", "duration_seconds": 45},
        {"num": "03", "pexels_search_query": "underwater ROV camera", "duration_seconds": 45},
        {"num": "04", "pexels_search_query": "new species discovery", "duration_seconds": 45},
        {"num": "05", "pexels_search_query": "deep sea expedition", "duration_seconds": 45},
        {"num": "06", "pexels_search_query": "subscribe ocean science", "duration_seconds": 30},
    ]

    def test_missing_script_returns_400(self):
        r = client.post("/agent/production-manager",
                        json={"task": {"title": "Test"}, "context": {}})
        self.assertEqual(r.status_code, 400)

    def test_seo_pack_and_job_queued(self):
        with patch("pipeline.fetch_pexels.fetch_pexels_videos", return_value=[None]*6), \
             patch("pipeline.tts.generate_voiceover", return_value="/tmp/narration.wav"), \
             patch("pipeline.subtitles.generate_subtitles", return_value="/tmp/subs.srt"), \
             patch("pipeline.assemble.assemble_video", return_value="/tmp/video.mp4"):

            r = client.post("/agent/production-manager", json={
                "task": {"title": "Deep Sea Creatures"},
                "context": {
                    "script": {"script": "Narration text about the deep sea."},
                    "scenes": {"scenes": self.SAMPLE_SCENES}
                }
            })

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "done")
        result = body["result"]
        seo = result["seo_pack"]
        self.assertIn("youtube_title", seo)
        self.assertIn("tags", seo)
        self.assertIsInstance(seo["tags"], list)
        job_id = result["assembly_job_id"]
        self.assertTrue(len(job_id) > 0)
        print(f"  YouTube title: '{seo.get('youtube_title','?')}'")
        print(f"  Tags: {seo.get('tags', [])[:3]}...")
        print(f"  Assembly job ID: {job_id}")
        return job_id


class TestAssembleVideoEndpoint(unittest.TestCase):
    """Tests the /assemble-video endpoint called directly by Paperclip."""

    SCENES = [
        {"num": "01", "pexels_search_query": "ocean deep sea", "duration_seconds": 30},
        {"num": "02", "pexels_search_query": "bioluminescent fish", "duration_seconds": 45},
    ]

    def _post(self, body):
        with patch("pipeline.fetch_pexels.fetch_pexels_videos", return_value=[None]*len(self.SCENES)), \
             patch("pipeline.tts.generate_voiceover", return_value="/tmp/narration.wav"), \
             patch("pipeline.subtitles.generate_subtitles", return_value="/tmp/subs.srt"), \
             patch("pipeline.assemble.assemble_video", return_value="/tmp/video.mp4"):
            return client.post("/assemble-video", json=body)

    def test_missing_script_returns_400(self):
        r = self._post({"scenes": self.SCENES})
        self.assertEqual(r.status_code, 400)
        self.assertIn("script", r.get_json()["error"])

    def test_missing_scenes_returns_400(self):
        r = self._post({"script": "Some narration text."})
        self.assertEqual(r.status_code, 400)
        self.assertIn("scenes", r.get_json()["error"])

    def test_flat_payload_queues_job(self):
        r = self._post({"script": "Narration text.", "scenes": self.SCENES})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["status"], "queued")
        self.assertIn("job_id", body)
        self.assertIn("poll_url", body)
        print(f"  /assemble-video job_id: {body['job_id']}")

    def test_paperclip_context_payload_queues_job(self):
        """Paperclip-style nested payload also works."""
        r = self._post({
            "task": {"title": "Deep Sea"},
            "context": {
                "script": {"script": "Narration text."},
                "scenes": {"scenes": self.SCENES}
            }
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "queued")


class TestStatusEndpoint(unittest.TestCase):
    """Tests /status/<job_id> endpoint."""

    def test_unknown_job_returns_404(self):
        r = client.get("/status/doesnotexist")
        self.assertEqual(r.status_code, 404)

    def test_known_job_returns_status(self):
        job_id = "testjob1"
        worker_app.JOBS[job_id] = {"status": "assembling_video", "progress": 75,
                                   "result": None, "error": None}
        r = client.get(f"/status/{job_id}")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["status"], "assembling_video")
        self.assertEqual(body["progress"], 75)

    def test_assembly_thread_updates_job(self):
        """Verifies the background assembly thread runs and transitions to done/error."""
        with patch("pipeline.fetch_pexels.fetch_pexels_videos", return_value=[None]*2), \
             patch("pipeline.tts.generate_voiceover", return_value="/tmp/narration.wav"), \
             patch("pipeline.subtitles.generate_subtitles", return_value="/tmp/subs.srt"), \
             patch("pipeline.assemble.assemble_video", return_value="/tmp/video.mp4"):

            job_id = "threadtest1"
            worker_app.JOBS[job_id] = {"status": "queued", "progress": 0,
                                       "result": None, "error": None}
            t = threading.Thread(
                target=worker_app._run_assembly,
                args=(job_id, "Test script", [{"num": "01", "pexels_search_query": "nature"}]*2),
                daemon=True
            )
            t.start()
            t.join(timeout=10)

        final = worker_app.JOBS.get(job_id, {})
        self.assertIn(final.get("status"), ("done", "error"),
                      f"Expected done/error, got: {final}")
        print(f"  Assembly thread final status: {final['status']}")


# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("YouTube Worker — Local Test Suite")
    print("=" * 60)

    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        print("\n⚠️  ANTHROPIC_API_KEY not set — Claude API tests will fail.")
        print("   Set it in your shell or copy .env.example → .env and source it.\n")
    else:
        print(f"\n✅ ANTHROPIC_API_KEY detected ({key[:12]}...)\n")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Always run these (no API key needed)
    suite.addTests(loader.loadTestsFromTestCase(TestHealth))
    suite.addTests(loader.loadTestsFromTestCase(TestHelpers))
    suite.addTests(loader.loadTestsFromTestCase(TestAssembleVideoEndpoint))
    suite.addTests(loader.loadTestsFromTestCase(TestStatusEndpoint))

    # Need ANTHROPIC_API_KEY
    if key:
        suite.addTests(loader.loadTestsFromTestCase(TestScriptwriter))
        suite.addTests(loader.loadTestsFromTestCase(TestSceneDirector))
        suite.addTests(loader.loadTestsFromTestCase(TestProductionManager))
    else:
        print("Skipping agent tests (TestScriptwriter, TestSceneDirector, TestProductionManager).\n")

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if target:
        suite = loader.loadTestsFromName(target, module=sys.modules[__name__])

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

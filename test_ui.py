import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("transcriber_ui", Path(__file__).with_name("ui.py"))
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class LocalAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = ui.Jobs(Path(self.temp.name))
        self.server = ui.make_server(jobs=self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, authenticated=True, extra=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Local-Token": self.server.token} if authenticated else {}
        if extra:
            headers.update(extra)
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        status, output_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, payload, output_headers

    def test_api_requires_session_token_and_rejects_query_token(self):
        self.assertEqual(self.request("/api/state", authenticated=False)[0], 403)
        self.assertEqual(self.request("/api/state?token=" + self.server.token, authenticated=False)[0], 403)
        self.assertEqual(self.request("/api/state")[0], 200)

    def test_dns_rebinding_and_foreign_origin_are_rejected(self):
        for headers in [{"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}]:
            self.assertEqual(self.request("/api/state", extra=headers)[0], 403)

    def test_private_config_and_path_traversal_are_not_served(self):
        for path in ["/config.json", "/windows-trust.pem", "/../config.json", "/%2e%2e/config.json"]:
            self.assertEqual(self.request(path)[0], 404)

    def test_only_one_user_initiated_job_can_start(self):
        source = Path(self.temp.name) / "source.wav"
        source.write_bytes(b"media-fixture")
        with patch.object(self.manager, "ready", return_value={"ready": True}), \
             patch.object(self.manager, "run", return_value=None):
            result = self.request("/api/jobs", "POST", {"source": str(source), "language": "en"})
            self.assertEqual(result[0], 202)
            self.assertEqual(self.request("/api/jobs", "POST", {"source": str(source)})[0], 409)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_export_is_restricted_to_generated_output_folders(self):
        self.manager.jobs["a" * 12] = {"id": "a" * 12, "output_dir": self.temp.name}
        status = self.request("/api/download?id=" + "a" * 12 + "&format=txt")[0]
        self.assertEqual(status, 404)
        folder = self.manager.output / "job_fixture"
        folder.mkdir(parents=True)
        (folder / "transcript.txt").write_text("Original synthetic test.", encoding="utf-8")
        self.manager.jobs["a" * 12]["output_dir"] = str(folder)
        result = self.request("/api/download?id=" + "a" * 12 + "&format=txt")
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1], b"Original synthetic test.")
        self.assertIn("attachment", result[2]["Content-Disposition"])

    def test_security_headers_and_loopback_binding(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        headers = self.request("/api/state")[2]
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_invalid_json_shape_and_large_body_are_rejected(self):
        self.assertEqual(self.request("/api/jobs", "POST", ["not-an-object"])[0], 400)
        self.assertEqual(self.request("/api/jobs", "POST", {"source": "x" * 70000})[0], 413)


if __name__ == "__main__":
    unittest.main(verbosity=2)

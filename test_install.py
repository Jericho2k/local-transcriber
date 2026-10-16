import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("transcriber_install", Path(__file__).with_name("install.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_pip_uses_isolation_hashes_binary_wheels_and_verified_tls(self):
        with tempfile.TemporaryDirectory() as temporary:
            cert = Path(temporary) / "trusted.pem"
            cert.write_text("existing-trust-store")
            with patch.object(installer, "TRUST_PEM", cert), \
                 patch.object(installer.subprocess, "run", return_value=Obj(returncode=0)) as run:
                installer.pip_install(Path("python.exe"), Path("requirements.lock"))
            command = run.call_args.args[0]
            for option in ["-I", "--isolated", "--only-binary=:all:", "--require-hashes", "--no-deps", "--cert"]:
                self.assertIn(option, command)
            self.assertNotIn("--trusted-host", command)
            self.assertEqual(command[command.index("--index-url") + 1], "https://pypi.org/simple")

    def test_cpu_filter_does_not_modify_source_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "requirements.lock"
            original = "faster-whisper==1.2.1 --hash=sha256:example\nnvidia-cublas-cu12==12.9.2.10 --hash=sha256:example\n"
            lock.write_text(original)
            with patch.object(installer, "LOCK", lock), patch.object(installer, "VENV", root):
                target = installer.filtered_lock("cpu")
            self.assertEqual(lock.read_text(), original)
            self.assertNotIn("nvidia", target.read_text())
            self.assertIn("faster-whisper", target.read_text())

    def test_model_weights_are_rejected_after_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in installer.MODEL_FILES:
                (root / name).write_bytes(b"test-data")
            expected = hashlib.sha256(b"test-data").hexdigest()
            with patch.object(installer, "MODEL_DIR", root), patch.object(installer, "BASE", root), \
                 patch.object(installer, "MODEL_SHA256", expected):
                self.assertTrue(installer.model_status()[0])
                (root / "model.bin").write_bytes(b"modified")
                self.assertFalse(installer.model_status()[0])

    def test_cpu_preference_is_written_to_local_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            with patch.object(installer, "CONFIG", config):
                installer.write_config({"ffmpeg": "ffmpeg.exe", "ffprobe": "ffprobe.exe", "node": None}, "cpu")
            self.assertIn('"default_device": "cpu"', config.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)

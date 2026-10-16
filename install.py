#!/usr/bin/env python3
"""Local Transcriber - bootstrap installer.

Standard library only. This file must stay importable by any Python 3 on PATH so
it can explain a version problem instead of crashing on a missing dependency.

What it does, in order:
  1. verifies Python 3.11 (64-bit) and that FFmpeg/FFprobe are on PATH,
  2. creates or reuses a private .venv next to this file,
  3. installs the pinned, hash-verified wheels from requirements.lock,
  4. fetches only the model data files into models/large-v3-turbo,
  5. writes config.json for the current machine.

It never edits requirements.lock, never changes system certificates, never
disables TLS verification, and never installs anything outside .venv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV = BASE / ".venv"
LOCK = BASE / "requirements.lock"
CONFIG = BASE / "config.json"
TRUST_PEM = BASE / "windows-trust.pem"
MODEL_DIR = BASE / "models" / "large-v3-turbo"

MODEL_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
MODEL_REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
MODEL_SHA256 = "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"
MODEL_FILES = ("model.bin", "config.json", "preprocessor_config.json",
               "tokenizer.json", "vocabulary.json")
PYPI_INDEX = "https://pypi.org/simple"
REQUIRED_PYTHON = (3, 11)


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str, code: int = 1) -> "None":
    print("\nERROR: " + message, file=sys.stderr, flush=True)
    raise SystemExit(code)


def check_python() -> None:
    if os.name != "nt":
        fail("This installer supports Windows only. Other systems are untested.")
    if sys.version_info[:2] != REQUIRED_PYTHON:
        fail(
            "Python 3.11 is required, found {}.{}.{}.\n"
            "Install 64-bit Python 3.11 from python.org and run INSTALL.cmd again.".format(
                *sys.version_info[:3]
            )
        )
    if platform.architecture()[0] != "64bit" or sys.maxsize <= 2 ** 32:
        fail("64-bit Python 3.11 is required; a 32-bit interpreter was found.")


def venv_python() -> Path:
    return VENV / "Scripts" / "python.exe"


def which_tools() -> dict:
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "node": shutil.which("node"),
    }


def check_tools() -> dict:
    tools = which_tools()
    missing = [name for name in ("ffmpeg", "ffprobe") if not tools.get(name)]
    if missing:
        fail(
            "Not found on PATH: {}. Install FFmpeg (which includes ffprobe), "
            "open a new terminal so PATH refreshes, and run INSTALL.cmd again.\n"
            "Nothing has been downloaded yet.".format(", ".join(missing))
        )
    if not tools.get("node"):
        log("Note: Node.js was not found. Everything works without it; only "
            "YouTube downloads need it.")
    return tools


def detect_gpu() -> str:
    """Return 'cuda' when an NVIDIA driver responds, otherwise 'cpu'."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return "cpu"
    try:
        result = subprocess.run([smi, "--query-gpu=name", "--format=csv,noheader"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "cpu"
    return "cuda" if result.returncode == 0 and result.stdout.strip() else "cpu"


def ensure_venv() -> Path:
    python = venv_python()
    if python.is_file():
        log("Reusing existing virtual environment: " + str(VENV))
        return python
    log("Creating private virtual environment: " + str(VENV))
    result = subprocess.run([sys.executable, "-m", "venv", str(VENV)])
    if result.returncode or not python.is_file():
        fail("Could not create .venv. Check that venv is available for this Python.")
    return python


def read_lock_lines() -> list:
    if not LOCK.is_file():
        fail("requirements.lock is missing next to install.py.")
    lines = []
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    if not lines:
        fail("requirements.lock contains no pinned packages.")
    return lines


def filtered_lock(device: str) -> Path:
    """Write a temporary install list. The source lock is never modified."""
    lines = read_lock_lines()
    if device == "cpu":
        kept = [line for line in lines if not line.lower().startswith("nvidia-")]
        removed = len(lines) - len(kept)
        log("CPU build: skipping {} NVIDIA wheel(s) from the lock.".format(removed))
        lines = kept
    target = VENV / ("lock-cpu.txt" if device == "cpu" else "lock-cuda.txt")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def pip_install(python: Path, lock_file: Path) -> None:
    log("Installing {} pinned wheels into .venv ...".format(
        "CPU" if "cpu" in lock_file.name else "GPU"))
    command = [
        str(python), "-I", "-m", "pip", "--isolated", "install",
        "--disable-pip-version-check",
        "--no-input",
        "--only-binary=:all:",
        "--require-hashes",
        "--no-deps",
        "--index-url", PYPI_INDEX,
        "-r", str(lock_file),
    ]
    if TRUST_PEM.is_file():
        command.extend(["--cert", str(TRUST_PEM)])
    result = subprocess.run(command)
    if result.returncode:
        fail("pip failed. Nothing outside .venv was changed. Read the messages above; "
             "a corporate proxy or an offline network is the usual cause.")


def ensure_trust_pem() -> None:
    """Export the roots Windows already trusts, only when the file is absent.

    This reads the existing ROOT store. It never adds, removes or installs a
    certificate, and it never disables verification.
    """
    if TRUST_PEM.is_file():
        log("Reusing existing windows-trust.pem.")
        return
    if os.name != "nt":
        return
    try:
        certificates = []
        for store in ("ROOT", "CA"):
            certificates.extend(ssl.enum_certificates(store) or [])
    except (AttributeError, OSError) as error:
        log("Could not read the Windows certificate store ({}); "
            "the system trust store will be used as usual.".format(error))
        return
    chunks = []
    for der, encoding, trust in certificates:
        if encoding == "x509_asn" and (trust is True or ssl.Purpose.SERVER_AUTH.oid in trust):
            try:
                chunks.append(ssl.DER_cert_to_PEM_cert(der))
            except ValueError:
                continue
    if not chunks:
        log("No trusted roots were exported; the system trust store will be used.")
        return
    TRUST_PEM.write_text("".join(dict.fromkeys(chunks)), encoding="ascii")
    log("Wrote windows-trust.pem from the already-trusted Windows roots.")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_status() -> tuple:
    """Return (ok, message) without touching the network."""
    missing = [name for name in MODEL_FILES if not (MODEL_DIR / name).is_file()]
    if missing:
        return False, "missing files: " + ", ".join(missing)
    actual = sha256_of(MODEL_DIR / "model.bin")
    if actual != MODEL_SHA256:
        return False, "model.bin hash mismatch (expected {}, got {})".format(
            MODEL_SHA256[:16] + "...", actual[:16] + "...")
    manifest_path = BASE / "model-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for row in manifest["files"]:
            if row["file"] == "model.bin":
                continue
            raw = (MODEL_DIR / row["file"]).read_bytes()
            value = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if value != row["digest"]:
                return False, row["file"] + " hash mismatch"
    return True, "model files present and hashes match"


def fetch_model(python: Path) -> None:
    log("Downloading model data files into models/large-v3-turbo ...")
    script = (
        "from huggingface_hub import snapshot_download\n"
        "path = snapshot_download(repo_id={repo!r}, revision={rev!r},\n"
        "    allow_patterns={files!r}, local_dir={dest!r}, token=False, force_download={force!r})\n"
        "print('MODEL_PATH:' + str(path))\n"
    ).format(repo=MODEL_REPO, rev=MODEL_REVISION,
             files=list(MODEL_FILES), dest=str(MODEL_DIR),
             force=all((MODEL_DIR / name).is_file() for name in MODEL_FILES))
    environment = dict(os.environ)
    environment["HF_HUB_DISABLE_TELEMETRY"] = "1"
    environment["DO_NOT_TRACK"] = "1"
    environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    if TRUST_PEM.is_file():
        environment["SSL_CERT_FILE"] = str(TRUST_PEM)
        environment["REQUESTS_CA_BUNDLE"] = str(TRUST_PEM)
    result = subprocess.run([str(python), "-I", "-c", script], env=environment)
    if result.returncode:
        fail("Model download failed. Partial files stay in models/large-v3-turbo and "
             "a rerun continues from there.")
    ok, message = model_status()
    if not ok:
        fail("Downloaded model failed verification: " + message)


def write_config(tools: dict, device: str = "auto") -> None:
    config = {
        "model_dir": "models/large-v3-turbo",
        "model_sha256": MODEL_SHA256,
        "model_repository": MODEL_REPO,
        "model_revision": MODEL_REVISION,
        "default_device": "cpu" if device == "cpu" else "auto",
        "ffmpeg": str(Path(tools["ffmpeg"]).resolve()) if tools.get("ffmpeg") else "",
        "ffprobe": str(Path(tools["ffprobe"]).resolve()) if tools.get("ffprobe") else "",
        "node": str(Path(tools["node"]).resolve()) if tools.get("node") else None,
    }
    CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    log("Wrote config.json for this machine.")


def verify() -> int:
    """Read-only verification of the current setup. No network, no writes."""
    problems = []

    def report(label: str, ok: bool, detail: str = "") -> None:
        log("  [{}] {}{}".format("ok" if ok else "!!", label,
                                 "" if not detail else " - " + detail))
        if not ok:
            problems.append(label)

    log("Local Transcriber - setup verification (read-only, no network)")
    report("Python 3.11 x64", sys.version_info[:2] == REQUIRED_PYTHON
           and platform.architecture()[0] == "64bit",
           platform.python_version() + " " + platform.architecture()[0])

    tools = which_tools()
    for name in ("ffmpeg", "ffprobe"):
        report(name + " on PATH", bool(tools.get(name)), tools.get(name) or "not found")
    report("node on PATH (optional)", True, tools.get("node") or "absent - YouTube disabled, local files fine")

    python = venv_python()
    report(".venv interpreter", python.is_file(), str(python))

    if python.is_file():
        probe = subprocess.run(
            [str(python), "-c",
             "import faster_whisper, ctranslate2, yt_dlp, huggingface_hub; print('ok')"],
            capture_output=True, text=True)
        detail = probe.stdout.strip() or (probe.stderr.strip().splitlines() or [""])[-1]
        report("packages import in .venv", probe.returncode == 0, detail)

    if CONFIG.is_file():
        try:
            config = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
            report("config.json parses", True, "keys: " + ", ".join(sorted(config)))
            for key in ("ffmpeg", "ffprobe"):
                value = config.get(key) or ""
                report("config.json {}".format(key), bool(value) and Path(value).is_file(),
                       value or "empty")
        except (OSError, ValueError) as error:
            report("config.json parses", False, str(error))
    else:
        report("config.json exists", False, "run INSTALL.cmd first")

    ok, message = model_status()
    report("model files and model.bin hash", ok, message)

    log("")
    if problems:
        log("RESULT: {} check(s) need attention: {}".format(
            len(problems), "; ".join(problems)))
        return 1
    log("RESULT: setup looks complete. Start the browser UI with START.cmd.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Local Transcriber installer: private .venv, hash-pinned wheels, "
                    "local model data, machine-specific config.json.")
    parser.add_argument("--cpu", action="store_true",
                        help="install the CPU build; NVIDIA wheels from the lock are skipped")
    parser.add_argument("--skip-model", action="store_true",
                        help="do not download model data even when it is missing")
    parser.add_argument("--verify", action="store_true",
                        help="read-only check of the current setup; no network, no writes")
    args = parser.parse_args()

    if args.verify:
        return verify()

    check_python()
    if not LOCK.is_file():
        fail("requirements.lock is missing next to install.py.")
    tools = check_tools()
    device = "cpu" if args.cpu else detect_gpu()
    log("Build: {} ({})".format(
        "CPU" if device == "cpu" else "NVIDIA CUDA",
        "--cpu was given" if args.cpu else "detected via nvidia-smi"))

    python = ensure_venv()
    ensure_trust_pem()
    pip_install(python, filtered_lock(device))

    if args.skip_model:
        log("Skipping model download because --skip-model was given.")
    else:
        ok, message = model_status()
        if ok:
            log("Existing model reused: " + message)
        else:
            log("Model not ready (" + message + "); downloading.")
            fetch_model(python)

    write_config(tools, device)
    log("")
    log("Done. Next: double-click START.cmd for the browser UI, "
        "or TRANSCRIBE.cmd for the command line.")
    log("Re-check any time with: .venv\\Scripts\\python.exe install.py --verify")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped. Rerun INSTALL.cmd to continue.", file=sys.stderr)
        raise SystemExit(130)

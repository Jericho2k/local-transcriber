"""Loopback-only browser UI. Media processing starts only after a local user action."""
from __future__ import annotations

import argparse
import importlib.util
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = Path(__file__).resolve().parent
VERSION = "0.2.0"
_spec = importlib.util.spec_from_file_location("transcriber_core", BASE / "app.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
FORMATS = {"txt": "transcript.txt", "timestamps": "transcript.timestamps.txt",
           "srt": "transcript.srt", "json": "transcript.json"}


class APIError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def choose_file():
    if os.name != "nt":
        raise APIError("Paste the full path of a local file on this platform.")
    script = r"""[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Choose audio or video'
$dialog.Filter = 'Audio and video|*.mp3;*.mp4;*.m4a;*.wav;*.webm;*.mkv;*.ogg;*.opus;*.flac;*.mov;*.aac|All files|*.*'
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.FileName)
}
$dialog.Dispose()
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise APIError("File picker could not open. Paste the full file path instead.", 500)
    return result.stdout.strip()


class Jobs:
    def __init__(self, base=BASE):
        self.base = Path(base)
        self.output = self.base / "output"
        self.history = self.base / ".local/ui-jobs.json"
        self.lock = threading.RLock()
        self.picker_lock = threading.Lock()
        self.jobs = {}
        self.processes = {}
        self.active_id = None
        if self.history.is_file():
            try:
                rows = json.loads(self.history.read_text(encoding="utf-8"))
                for row in rows[:50]:
                    if not re.fullmatch(r"[a-f0-9]{12}", row.get("id", "")):
                        continue
                    if row.get("status") in {"queued", "running", "stopping"}:
                        row["status"] = "stopped"
                    row["logs"] = row.get("logs", [])[-600:]
                    self.jobs[row["id"]] = row
            except (OSError, ValueError, TypeError, KeyError):
                print("Could not read previous UI history; existing transcription checkpoints are unchanged.")

    def persist(self):
        rows = sorted(self.jobs.values(), key=lambda x: x["created_at"], reverse=True)[:50]
        core.write_json(self.history, rows)

    def ready(self):
        try:
            core.load_config()
            return {"ready": True, "message": "Ready for local transcription"}
        except (OSError, ValueError, KeyError) as exc:
            return {"ready": False, "message": "Run INSTALL.cmd first. " + str(exc)}

    def folder(self, job):
        value = job.get("output_dir")
        if not value:
            return None
        folder = Path(value).resolve()
        if folder.parent != self.output.resolve() or not folder.name.startswith("job_"):
            return None
        return folder

    def refresh(self, job):
        folder = self.folder(job)
        status = folder / "status.json" if folder else None
        if status and status.is_file():
            try:
                value = json.loads(status.read_text(encoding="utf-8"))
                job["completed_parts"] = int(value.get("completed_parts", 0))
                job["total_parts"] = int(value.get("total_parts", 0))
                job["progress"] = min(100, round(100 * job["completed_parts"] / max(1, job["total_parts"])))
                if value.get("complete") and job["status"] not in {"queued", "running", "stopping"}:
                    job["status"] = "completed"
                    job["error"] = ""
            except (OSError, ValueError, TypeError):
                pass

    def summary(self, job):
        self.refresh(job)
        return {key: job.get(key) for key in (
            "id", "title", "source", "language", "device", "status", "created_at", "progress",
            "completed_parts", "total_parts", "error")}

    def state(self):
        with self.lock:
            rows = sorted(self.jobs.values(), key=lambda x: x["created_at"], reverse=True)
            return {"name": "Local Transcriber", "version": VERSION, "setup": self.ready(),
                    "active_id": self.active_id, "jobs": [self.summary(row) for row in rows]}

    def get(self, identifier):
        if identifier not in self.jobs:
            raise APIError("Job not found.", 404)
        return self.jobs[identifier]

    def detail(self, identifier):
        with self.lock:
            job = self.get(identifier)
            result = self.summary(job)
            folder = self.folder(job)
            preview = ""
            if folder and (folder / "transcript.txt").is_file():
                with (folder / "transcript.txt").open("rb") as stream:
                    stream.seek(max(0, (folder / "transcript.txt").stat().st_size - 48000))
                    preview = stream.read().decode("utf-8", errors="replace")
            result.update(logs=job.get("logs", [])[-600:], preview=preview,
                          output_dir=str(folder) if folder else None,
                          files=[{"format": key, "name": name} for key, name in FORMATS.items()
                                 if folder and (folder / name).is_file()])
            return result

    def create(self, data):
        if not isinstance(data, dict):
            raise APIError("Expected a JSON object.")
        source, language, device = data.get("source"), data.get("language", "auto"), data.get("device", "auto")
        if not isinstance(source, str) or not source.strip() or len(source) > 4096:
            raise APIError("Enter one YouTube URL or local media path.")
        if not isinstance(language, str) or not isinstance(device, str) or language not in {"auto", "en", "ru"} or device not in {"auto", "cpu", "cuda"}:
            raise APIError("Unsupported language or device.")
        try:
            identified = core.identify_source(source)
        except (ValueError, OSError) as exc:
            raise APIError(str(exc)) from exc
        if not self.ready()["ready"]:
            raise APIError("Setup is incomplete. Run INSTALL.cmd first.", 409)
        with self.lock:
            if self.active_id:
                raise APIError("One task is already running. Stop it or wait for it to finish.", 409)
            identifier = uuid.uuid4().hex[:12]
            job = {"id": identifier, "title": identified["label"], "source": identified["source"],
                   "language": language, "device": device, "status": "queued", "created_at": time.time(),
                   "progress": 0, "completed_parts": 0, "total_parts": 0, "error": "", "logs": [],
                   "output_dir": None}
            self.jobs[identifier] = job
            self.active_id = identifier
            self.persist()
            result = self.summary(job)
            threading.Thread(target=self.run, args=(identifier,), daemon=True).start()
            return result

    def run(self, identifier):
        with self.lock:
            job = self.jobs[identifier]
            if job["status"] == "stopping":
                job["status"] = "stopped"
                self.active_id = None
                self.persist()
                return
            command = [sys.executable, "-I", str(self.base / "app.py"), job["source"],
                       "--language", job["language"], "--device", job["device"], "--output", str(self.output)]
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                           errors="replace", shell=False, **options)
            except OSError as exc:
                job.update(status="failed", error=str(exc))
                self.active_id = None
                self.persist()
                return
            self.processes[identifier] = process
            job["status"] = "running"
            self.persist()
        try:
            for line in process.stdout:
                line = line.rstrip()
                with self.lock:
                    job["logs"].append(line[:2000])
                    job["logs"] = job["logs"][-600:]
                    if line.startswith("Результат: "):
                        candidate = Path(line.split(": ", 1)[1]).resolve()
                        if candidate.parent == self.output.resolve() and candidate.name.startswith("job_"):
                            job["output_dir"] = str(candidate)
            code = process.wait()
            with self.lock:
                self.refresh(job)
                if code == 0:
                    job["status"] = "completed"
                    job["progress"] = 100
                elif job["status"] == "stopping" or code == 130:
                    job["status"] = "stopped"
                else:
                    job.update(status="failed", error="Transcription stopped with an error. See the activity log.")
        except Exception as exc:
            with self.lock:
                job.update(status="failed", error=str(exc))
        finally:
            with self.lock:
                self.processes.pop(identifier, None)
                self.active_id = None
                self.persist()

    @staticmethod
    def terminate(process):
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=8)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def stop(self, wait=False):
        with self.lock:
            if not self.active_id:
                return
            identifier = self.active_id
            self.jobs[identifier]["status"] = "stopping"
            process = self.processes.get(identifier)
            self.persist()
        if process:
            if wait:
                self.terminate(process)
            else:
                threading.Thread(target=self.terminate, args=(process,), daemon=True).start()

    def download(self, identifier, kind):
        if kind not in FORMATS:
            raise APIError("Unknown export format.")
        with self.lock:
            folder = self.folder(self.get(identifier))
            path = folder / FORMATS[kind] if folder else None
            if not path or not path.is_file():
                raise APIError("This export is not available yet.", 404)
            return path


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalTranscriber/0.2"

    def log_message(self, *args):
        pass

    def headers_for(self, code, content_type, length, filename=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

    def send_json(self, data, code=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.headers_for(code, "application/json; charset=utf-8", len(raw))
        self.wfile.write(raw)

    def validate(self, api=False):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise APIError("Invalid host.", 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + value for value in hosts}:
            raise APIError("Cross-origin requests are not allowed.", 403)
        if api and not secrets.compare_digest(self.headers.get("X-Local-Token", ""), self.server.token):
            raise APIError("Open the app using the URL printed by START.cmd.", 403)

    def route(self):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            raise APIError("Invalid request target.")
        self.validate(parsed.path.startswith("/api/"))
        return parsed

    def do_GET(self):
        try:
            parsed = self.route()
            if parsed.path == "/api/state":
                return self.send_json(self.server.jobs.state())
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})", parsed.path)
            if match:
                return self.send_json(self.server.jobs.detail(match[1]))
            if parsed.path == "/api/download":
                query = parse_qs(parsed.query)
                path = self.server.jobs.download(query.get("id", [""])[0], query.get("format", [""])[0])
                raw = path.read_bytes()
                content_type = "application/json; charset=utf-8" if path.suffix == ".json" else "text/plain; charset=utf-8"
                self.headers_for(200, content_type, len(raw), path.name)
                return self.wfile.write(raw)
            static = {"/": "web/index.html", "/styles.css": "web/styles.css",
                      "/app.js": "web/app.js", "/assets/mark.svg": "assets/mark.svg"}
            if parsed.path not in static:
                raise APIError("Not found.", 404)
            path = BASE / static[parsed.path]
            if not path.is_file():
                raise APIError("Interface files are missing.", 503)
            raw = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix in {".html", ".css", ".js"}:
                content_type += "; charset=utf-8"
            self.headers_for(200, content_type, len(raw))
            self.wfile.write(raw)
        except APIError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send_json({"error": "Local server error: " + str(exc)}, 500)

    def body(self):
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise APIError("Expected application/json.", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError("Invalid content length.")
        if not 0 < length <= 65536:
            raise APIError("Request body is empty or too large.", 413)
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise APIError("Invalid JSON.")
        if not isinstance(data, dict):
            raise APIError("Expected a JSON object.")
        return data

    def do_POST(self):
        try:
            parsed = self.route()
            if not parsed.path.startswith("/api/"):
                raise APIError("Not found.", 404)
            data = self.body()
            if parsed.path == "/api/jobs":
                return self.send_json({"job": self.server.jobs.create(data)}, 202)
            if parsed.path == "/api/stop":
                self.server.jobs.stop()
                return self.send_json({"ok": True}, 202)
            if parsed.path == "/api/select-file":
                if not self.server.jobs.picker_lock.acquire(blocking=False):
                    raise APIError("A file picker is already open.", 409)
                try:
                    return self.send_json({"path": choose_file()})
                finally:
                    self.server.jobs.picker_lock.release()
            if parsed.path == "/api/open-folder":
                with self.server.jobs.lock:
                    folder = self.server.jobs.folder(self.server.jobs.get(data.get("id", "")))
                if not folder or not folder.is_dir():
                    raise APIError("The output folder is not available yet.", 404)
                if os.name != "nt":
                    raise APIError("Open the output folder using your file manager.")
                os.startfile(str(folder))
                return self.send_json({"ok": True})
            raise APIError("Not found.", 404)
        except APIError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send_json({"error": "Local server error: " + str(exc)}, 500)


def make_server(port=0, jobs=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.token = secrets.token_urlsafe(32)
    server.jobs = jobs or Jobs()
    return server


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Local Transcriber browser interface")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    session = BASE / ".local/ui-session.json"
    try:
        with core.job_lock(BASE / ".local"):
            serve(args, session)
    except ValueError:
        try:
            existing = json.loads(session.read_text(encoding="utf-8"))
            port = int(existing["port"])
            if not 1 <= port <= 65535:
                raise ValueError("Invalid local port")
            address = f"http://127.0.0.1:{port}/"
            request = urllib.request.Request(address + "api/state", headers={"X-Local-Token": existing["token"]})
            with urllib.request.urlopen(request, timeout=3) as response:
                state = json.load(response)
            if state.get("name") != "Local Transcriber":
                raise ValueError("Different server")
            url = address + "#token=" + existing["token"]
            print("Local Transcriber is already running.\n" + url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
        except (OSError, ValueError, KeyError):
            print("Local Transcriber is already starting or running. Use its existing window.")


def serve(args, session):
    server = make_server(args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/#token={server.token}"
    core.write_json(session, {"port": server.server_address[1], "token": server.token})
    print("Local Transcriber — local interface; keep this window open.", flush=True)
    print(url, flush=True)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping local tasks...", flush=True)
    finally:
        server.jobs.stop(wait=True)
        server.server_close()
        session.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

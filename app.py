"""Local, resumable speech recognition. Run START.cmd or python app.py --help."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

BASE = Path(__file__).resolve().parent
DLL_HANDLES = []


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def timestamp(seconds, srt=False):
    ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{',' if srt else '.'}{millis:03d}"


def youtube_source(value):
    url = urlsplit(value)
    if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
        raise ValueError("Нужна обычная HTTPS-ссылка YouTube без логина и пароля.")
    host = (url.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = url.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if url.path == "/watch":
            video_id = parse_qs(url.query).get("v", [""])[0]
        elif url.path.startswith(("/shorts/", "/live/", "/embed/")):
            video_id = url.path.split("/")[2]
        else:
            video_id = ""
    else:
        raise ValueError("Поддерживается ссылка YouTube либо путь к локальному файлу.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Не удалось определить ID одного видео; плейлисты не поддерживаются.")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def identify_source(value):
    value = value.strip().strip('"')
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        canonical, video_id = youtube_source(value)
        return {"kind": "youtube", "source": canonical, "label": video_id,
                "identity": canonical}
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Файл не найден: {path}")
    stat = path.stat()
    return {"kind": "file", "source": str(path), "label": path.stem,
            "identity": f"{path}|{stat.st_size}|{stat.st_mtime_ns}"}


def configure_environment():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    # Only this process receives the existing Windows trust store, not a TLS bypass.
    certificate = BASE / "windows-trust.pem"
    if certificate.is_file():
        os.environ["SSL_CERT_FILE"] = str(certificate)
        os.environ["REQUESTS_CA_BUNDLE"] = str(certificate)
    if os.name == "nt":
        for folder in sorted((Path(sys.prefix) / "Lib/site-packages/nvidia").glob("*/bin")):
            DLL_HANDLES.append(os.add_dll_directory(str(folder)))
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


def load_config():
    config = json.loads((BASE / "config.json").read_text(encoding="utf-8-sig"))
    for key in ("ffmpeg", "ffprobe"):
        if not Path(config[key]).is_file():
            raise ValueError(f"Не найден {key}; проверь config.json.")
    model = (BASE / config["model_dir"]).resolve()
    for filename in ("model.bin", "config.json", "tokenizer.json"):
        if not (model / filename).is_file():
            raise ValueError(f"Не хватает файла модели: {model / filename}")
    config["model_path"] = str(model)
    return config


@contextmanager
def job_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    stream = (folder / ".running.lock").open("a+b")
    locked = False
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Эта задача уже запущена в другом окне.") from exc
        locked = True
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def download_audio(source, folder, config):
    manifest = folder / "download.json"
    if manifest.is_file():
        old = json.loads(manifest.read_text(encoding="utf-8"))
        candidate = (folder / old["filename"]).resolve()
        if candidate.parent == folder.resolve() and candidate.is_file() and candidate.stat().st_size:
            print("Аудио уже скачано; использую сохранённый файл.", flush=True)
            return candidate
    folder.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-I", "-m", "yt_dlp", "--ignore-config",
               "--no-plugin-dirs", "--no-remote-components", "--no-playlist",
               "--format", "bestaudio[ext=m4a]/bestaudio", "--restrict-filenames",
               "--windows-filenames", "--paths", str(folder), "--output", "%(id)s.%(ext)s",
               "--print", "after_move:TRANSCRIBER_PATH:%(filepath)j", "--newline", "--progress",
               "--retries", "3", "--fragment-retries", "3", "--socket-timeout", "30",
               "--ffmpeg-location", str(Path(config["ffmpeg"]).parent)]
    if config.get("node") and Path(config["node"]).is_file():
        command.extend(["--js-runtimes", f"node:{config['node']}"])
    command.extend(["--", source])
    print("Скачивание только аудиодорожки...", flush=True)
    downloaded = None
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", shell=False) as process:
        try:
            for line in process.stdout:
                line = line.rstrip()
                if line.startswith("TRANSCRIBER_PATH:"):
                    downloaded = Path(json.loads(line.split(":", 1)[1])).resolve()
                else:
                    print(line, flush=True)
            code = process.wait()
        except BaseException:
            process.terminate()
            process.wait()
            raise
    if code or not downloaded or not downloaded.is_file():
        raise RuntimeError("YouTube не отдал аудио. Подробности выше; можно указать локальный файл.")
    if downloaded.parent != folder.resolve():
        raise RuntimeError("Загрузчик вернул файл за пределами папки аудио.")
    write_json(manifest, {"filename": downloaded.name})
    return downloaded


def duration_of(media, config):
    result = subprocess.run([config["ffprobe"], "-v", "error", "-show_entries",
                             "format=duration", "-of", "json", str(media)],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError("Не удалось прочитать медиафайл: " + result.stderr[-1500:])
    value = float(json.loads(result.stdout)["format"]["duration"])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Длительность файла неизвестна или равна нулю.")
    return value


def chunk_window(index, chunk_seconds, duration):
    core_start = index * chunk_seconds
    core_end = min(duration, core_start + chunk_seconds)
    start = max(0.0, core_start - 2.0)
    end = min(duration, core_end + 2.0)
    return core_start, core_end, start, end


def owned_segments(segments, offset, core_start, core_end, is_last):
    result = []
    for segment in segments:
        if segment.words:
            words = [w for w in segment.words
                     if core_start <= offset + (w.start + w.end) / 2 < core_end
                     or (is_last and offset + (w.start + w.end) / 2 == core_end)]
            if not words:
                continue
            start = max(core_start, offset + words[0].start)
            end = min(core_end, offset + words[-1].end)
            text = "".join(w.word for w in words).strip()
        else:
            start, end = offset + segment.start, offset + segment.end
            if not core_start <= (start + end) / 2 < core_end:
                continue
            start, end = max(core_start, start), min(core_end, end)
            text = segment.text.strip()
        if text and end > start:
            result.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return result


def export_results(folder, metadata, parts):
    segments = sorted((s for part in parts for s in part["segments"]), key=lambda s: s["start"])
    atomic_text(folder / "transcript.txt", "\n\n".join(s["text"] for s in segments) + "\n")
    atomic_text(folder / "transcript.timestamps.txt", "\n".join(
        f"[{timestamp(s['start'])}] {s['text']}" for s in segments) + "\n")
    atomic_text(folder / "transcript.srt", "\n\n".join(
        f"{i}\n{timestamp(s['start'], True)} --> {timestamp(s['end'], True)}\n{s['text']}"
        for i, s in enumerate(segments, 1)) + "\n")
    write_json(folder / "transcript.json", {"metadata": metadata, "segments": segments})
    write_json(folder / "status.json", metadata)


def load_recognizer(config, requested):
    from faster_whisper import WhisperModel
    import ctranslate2
    count = ctranslate2.get_cuda_device_count()
    if requested == "auto" and config.get("default_device") == "cpu":
        device = "cpu"
    else:
        device = ("cuda" if count else "cpu") if requested == "auto" else requested
    if device == "cuda" and not count:
        raise RuntimeError("CUDA-устройство не найдено. Используй --device cpu для обработки на CPU.")
    print(f"Устройство: {device}; загрузка локальной модели...", flush=True)
    model = WhisperModel(config["model_path"], device=device,
                         compute_type="int8_float16" if device == "cuda" else "int8",
                         cpu_threads=min(os.cpu_count() or 4, 8), local_files_only=True)
    return model, device


def transcribe(args, config):
    source = identify_source(args.source)
    profile = {"source": source["identity"], "language": args.language,
               "model_sha256": config["model_sha256"], "chunk_seconds": args.chunk_seconds,
               "beam_size": args.beam_size, "format_version": 1}
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:12]
    label = re.sub(r'[^\w.-]+', '_', source["label"], flags=re.UNICODE).strip(". ")[:55] or "audio"
    folder = Path(args.output).expanduser().resolve() / f"job_{label}_{digest}"
    with job_lock(folder):
        if source["kind"] == "youtube":
            media = download_audio(source["source"], folder / "source_audio", config)
        else:
            media = Path(source["source"])
        duration = duration_of(media, config)
        total = math.ceil(duration / args.chunk_seconds)
        metadata = {"source": source["source"], "profile": profile, "duration": duration,
                    "total_parts": total, "completed_parts": 0, "complete": False,
                    "language": None, "device": args.device}
        parts_dir = folder / "parts"
        parts_dir.mkdir(exist_ok=True)
        parts = []
        for index in range(total):
            path = parts_dir / f"{index:05d}.json"
            if not path.is_file():
                break
            part = json.loads(path.read_text(encoding="utf-8"))
            if part["index"] != index or part["profile_digest"] != digest:
                raise RuntimeError("Контрольная точка не соответствует задаче; не изменяй parts вручную.")
            parts.append(part)
        if parts:
            metadata["language"] = parts[0].get("language")
        metadata["completed_parts"] = len(parts)
        print(f"Длительность: {timestamp(duration)}; частей: {total}; сохранено: {len(parts)}", flush=True)
        print(f"Результат: {folder}", flush=True)
        if len(parts) == total:
            metadata["complete"] = True
            export_results(folder, metadata, parts)
            print("Все части уже готовы. Итоговые файлы обновлены.")
            return folder
        export_results(folder, metadata, parts)
        model, device = load_recognizer(config, args.device)
        metadata["device"] = device
        selected_language = metadata["language"] or (None if args.language == "auto" else args.language)
        run_start = time.monotonic()
        starting_part = len(parts)
        try:
            for index in range(starting_part, total):
                core_start, core_end, start, end = chunk_window(index, args.chunk_seconds, duration)
                print(f"\nЧасть {index + 1}/{total}: {timestamp(core_start)} — {timestamp(core_end)}", flush=True)
                # Only this temporary WAV is replaced; the input media is never modified.
                with tempfile.TemporaryDirectory(prefix=".chunk_", dir=folder) as temporary:
                    wav = Path(temporary) / "audio.wav"
                    command = [config["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error",
                               "-ss", str(start), "-i", str(media), "-t", str(end - start),
                               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
                    converted = subprocess.run(command, capture_output=True, text=True,
                                               encoding="utf-8", errors="replace")
                    if converted.returncode:
                        raise RuntimeError("Ошибка извлечения звука: " + converted.stderr[-1500:])
                    segments, info = model.transcribe(str(wav), language=selected_language,
                                                     beam_size=args.beam_size, task="transcribe",
                                                     word_timestamps=True, vad_filter=True,
                                                     condition_on_previous_text=False)
                    owned = owned_segments(segments, start, core_start, core_end, index == total - 1)
                    selected_language = selected_language or info.language
                part = {"index": index, "profile_digest": digest,
                        "language": selected_language, "segments": owned}
                write_json(parts_dir / f"{index:05d}.json", part)
                parts.append(part)
                metadata.update(completed_parts=len(parts), language=selected_language,
                                complete=len(parts) == total)
                export_results(folder, metadata, parts)
                elapsed = time.monotonic() - run_start
                print(f"Сохранено {len(parts)}/{total}. В этом запуске прошло {elapsed / 60:.1f} мин.", flush=True)
        except BaseException as exc:
            metadata.update(complete=False, error=type(exc).__name__)
            export_results(folder, metadata, parts)
            raise
        print(f"\nГОТОВО: {folder / 'transcript.txt'}", flush=True)
        return folder


def self_test():
    assert timestamp(3661.125) == "01:01:01.125"
    assert timestamp(1.25, True) == "00:00:01,250"
    _, vid = youtube_source("https://youtu.be/abcdefghijk?si=example")
    assert vid == "abcdefghijk"
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        export_results(folder, {"complete": False}, [{"segments": [
            {"start": 1.0, "end": 2.0, "text": "Local test."}]}])
        assert "00:00:01,000 --> 00:00:02,000" in (folder / "transcript.srt").read_text()
    print("PASS: basic checks; no media downloaded or transcribed.")


def main():
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Локальная расшифровка аудио/видео или одного YouTube-ролика.")
    parser.add_argument("--version", action="version", version="Local Transcriber 0.2.0")
    parser.add_argument("source", nargs="?", help="Путь к файлу или HTTPS-ссылка YouTube")
    parser.add_argument("--language", default="auto", help="auto, en, ru или другой код языка; перевод не выполняется")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--output", default=str(BASE / "output"))
    parser.add_argument("--chunk-seconds", type=int, default=600)
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--check", action="store_true", help="Проверить загрузку локальной модели, без распознавания")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not 30 <= args.chunk_seconds <= 1800 or not 1 <= args.beam_size <= 10:
        parser.error("chunk-seconds: 30..1800; beam-size: 1..10")
    if args.self_test:
        self_test()
        return 0
    configure_environment()
    config = load_config()
    if args.check:
        _, device = load_recognizer(config, args.device)
        print(f"PASS: модель загружена, device={device}; обработка медиа не запускалась.")
        return 0
    if not args.source:
        print("Local Transcriber — локальная расшифровка, без платного API.\n")
        args.source = input("Вставь ссылку YouTube или полный путь к аудио/видео и нажми Enter:\n> ").strip()
        if not args.source:
            return 0
        args.language = input("Язык: Enter = автоматически, en = английский, ru = русский:\n> ").strip() or "auto"
    if not re.fullmatch(r"auto|[a-z]{2,3}", args.language):
        raise ValueError("Нужен код языка: auto, en, ru и т. п.")
    transcribe(args, config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nОстановлено. Завершённые части сохранены; повтори запуск с тем же источником и языком.")
        raise SystemExit(130)
    except Exception as error:
        print(f"\nОШИБКА: {error}", file=sys.stderr)
        print("Завершённые части сохранены. При ошибке CUDA можно попробовать --device cpu.", file=sys.stderr)
        raise SystemExit(1)

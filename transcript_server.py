#!/usr/bin/env python3
"""Serve the personal toolbox and extract public YouTube caption tracks locally."""

import argparse
import html
import json
import os
import re
import subprocess
import tempfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


class TranscriptError(Exception):
    def __init__(self, message, status=400, code="transcript_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def is_youtube_url(value):
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.hostname in YOUTUBE_HOSTS


def run_yt_dlp(binary, arguments, timeout=120):
    command = [binary, "--ignore-config", "--no-playlist", *arguments]
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as error:
        raise TranscriptError("截取逾時，請確認網路後再試一次。", 504, "timeout") from error
    except subprocess.CalledProcessError as error:
        detail = f"{error.stderr}\n{error.stdout}".lower()
        if "private video" in detail or "sign in" in detail or "login" in detail:
            message = "這部影片為私人影片、受年齡限制，或需要登入才能讀取。"
            code = "restricted"
        elif "video unavailable" in detail or "not available" in detail:
            message = "這部影片目前無法使用或不在你的地區提供。"
            code = "unavailable"
        elif "no subtitles" in detail or "requested subtitles" in detail:
            message = "這部影片沒有可用的字幕。"
            code = "no_captions"
        else:
            message = "YouTube 暫時無法提供字幕，請稍後再試。"
            code = "extract_failed"
        raise TranscriptError(message, 502, code) from error


def language_groups(requested):
    groups = {
        "zh-Hant": ["zh-TW", "zh-Hant", "zh-HK", "zh"],
        "zh-Hans": ["zh-CN", "zh-Hans", "zh-SG", "zh"],
        "en": ["en", "en-US", "en-GB"],
    }
    if requested in groups:
        return groups[requested]
    return [
        "zh-TW",
        "zh-Hant",
        "zh-HK",
        "zh",
        "zh-CN",
        "zh-Hans",
        "en",
        "en-US",
        "en-GB",
    ]


def match_language(tracks, preferred, allow_family=True):
    if not isinstance(tracks, dict):
        return None
    lowered = {str(key).lower(): key for key in tracks}
    for language in preferred:
        exact = lowered.get(language.lower())
        if exact:
            return exact
    if allow_family:
        for language in preferred:
            prefix = language.lower().split("-")[0]
            for key in tracks:
                if str(key).lower().split("-")[0] == prefix:
                    return key
    return None


def choose_track(metadata, requested):
    manual = metadata.get("subtitles") or {}
    automatic = metadata.get("automatic_captions") or {}
    preferred = language_groups(requested)

    for language in preferred:
        match = match_language(manual, [language], allow_family=False)
        if match:
            return match, "manual"
        match = match_language(automatic, [language], allow_family=False)
        if match:
            return match, "automatic"

    if requested != "auto":
        raise TranscriptError("找不到所選語言的字幕，請改用自動選擇。", 404, "language_missing")
    match = match_language(manual, preferred)
    if match:
        return match, "manual"
    match = match_language(automatic, preferred)
    if match:
        return match, "automatic"
    if manual:
        return next(iter(manual)), "manual"
    if automatic:
        return next(iter(automatic)), "automatic"
    raise TranscriptError("這部影片沒有可用的字幕。", 404, "no_captions")


TAG_RE = re.compile(r"<[^>]+>")
INLINE_TIME_RE = re.compile(r"<\d{2}:\d{2}:\d{2}[.,]\d{3}>")


def clean_caption(value):
    value = INLINE_TIME_RE.sub("", value or "")
    value = TAG_RE.sub("", value)
    value = html.unescape(value).replace("\u200b", "")
    return re.sub(r"[ \t]+", " ", value).strip()


def append_segment(segments, start, duration, text):
    cleaned = clean_caption(text)
    if not cleaned:
        return
    if segments and cleaned == segments[-1]["text"]:
        segments[-1]["duration"] = max(segments[-1]["duration"], duration)
        return
    segments.append(
        {
            "start": round(max(float(start), 0), 3),
            "duration": round(max(float(duration), 0.5), 3),
            "text": cleaned,
        }
    )


def parse_json3(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    segments = []
    for event in payload.get("events", []):
        pieces = event.get("segs") or []
        text = "".join(piece.get("utf8", "") for piece in pieces)
        start = float(event.get("tStartMs", 0)) / 1000
        duration = float(event.get("dDurationMs", 0)) / 1000
        for line in text.splitlines():
            append_segment(segments, start, duration, line)
    return segments


TIME_RE = re.compile(
    r"(?:(\d{2}):)?(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+"
    r"(?:(\d{2}):)?(\d{2}):(\d{2})[.,](\d{3})"
)


def seconds_from_match(match, offset):
    hours = int(match.group(offset) or 0)
    minutes = int(match.group(offset + 1))
    seconds = int(match.group(offset + 2))
    milliseconds = int(match.group(offset + 3))
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def parse_vtt(path):
    blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig", errors="replace"))
    segments = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = TIME_RE.search(lines[timing_index])
        if not match:
            continue
        start = seconds_from_match(match, 1)
        end = seconds_from_match(match, 5)
        text_lines = lines[timing_index + 1 :]
        for line in text_lines:
            append_segment(segments, start, end - start, line)
    return segments


def get_metadata(binary, url):
    result = run_yt_dlp(
        binary,
        ["--skip-download", "--dump-single-json", "--no-warnings", url],
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise TranscriptError("無法讀取影片資訊，請稍後再試。", 502, "metadata_failed") from error


def extract_transcript(binary, url, requested_language):
    metadata = get_metadata(binary, url)
    language, source = choose_track(metadata, requested_language)
    with tempfile.TemporaryDirectory(prefix="toolbox-transcript-") as temp_dir:
        template = str(Path(temp_dir) / "transcript.%(ext)s")
        subtitle_flag = "--write-subs" if source == "manual" else "--write-auto-subs"
        run_yt_dlp(
            binary,
            [
                "--skip-download",
                subtitle_flag,
                "--sub-langs",
                language,
                "--sub-format",
                "json3/vtt/best",
                "--output",
                template,
                "--no-warnings",
                url,
            ],
        )
        files = list(Path(temp_dir).glob("*.json3")) + list(Path(temp_dir).glob("*.vtt"))
        if not files:
            raise TranscriptError("字幕已找到，但下載失敗，請稍後再試。", 502, "caption_download_failed")
        caption_path = files[0]
        segments = parse_json3(caption_path) if caption_path.suffix == ".json3" else parse_vtt(caption_path)

    if not segments:
        raise TranscriptError("字幕內容是空的，無法建立逐字稿。", 422, "empty_captions")
    return {
        "videoId": str(metadata.get("id") or ""),
        "url": metadata.get("webpage_url") or url,
        "title": metadata.get("title") or "未命名 YouTube 影片",
        "channel": metadata.get("channel") or metadata.get("uploader") or "",
        "thumbnail": metadata.get("thumbnail") or "",
        "duration": metadata.get("duration") or 0,
        "language": language,
        "source": source,
        "segments": segments,
    }


class TranscriptHandler(SimpleHTTPRequestHandler):
    yt_dlp_binary = "yt-dlp"

    def end_headers(self):
        if self.path.startswith("/api/"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/health":
            self.send_json({"status": "ok", "service": "YouTube 逐字稿"})
            return
        super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/transcript":
            self.send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32768:
                raise TranscriptError("請提供有效的 YouTube 網址。")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            url = str(payload.get("url") or "").strip()
            language = str(payload.get("language") or "auto")
            if not is_youtube_url(url):
                raise TranscriptError("請貼上有效的 YouTube 影片網址。", 400, "invalid_url")
            result = extract_transcript(self.yt_dlp_binary, url, language)
            self.send_json({"status": "ok", **result})
        except TranscriptError as error:
            self.send_json({"status": "error", "error": error.message, "code": error.code}, error.status)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"status": "error", "error": "請求格式不正確。", "code": "invalid_request"}, 400)
        except Exception as error:  # pragma: no cover - final safety net for the local service
            print(f"逐字稿服務錯誤: {error}")
            self.send_json({"status": "error", "error": "截取失敗，請稍後再試。", "code": "server_error"}, 500)

    def log_message(self, format_string, *args):
        if self.path.startswith("/api/"):
            print(f"[{self.log_date_time_string()}] {format_string % args}")


def main():
    parser = argparse.ArgumentParser(description="工具箱逐字稿本機服務")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--yt-dlp", default="yt-dlp")
    arguments = parser.parse_args()

    handler = partial(TranscriptHandler, directory=str(Path(arguments.root).resolve()))
    TranscriptHandler.yt_dlp_binary = arguments.yt_dlp
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), handler)
    print(f"工具箱已啟動：http://127.0.0.1:{arguments.port}")
    print("關閉這個視窗即可停止服務。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

import os
import sys
import time
import signal
import subprocess
from datetime import datetime

TIKTOK_URL = os.getenv("TIKTOK_URL", "https://www.tiktok.com/@anass_bn67/live").strip()
YOUTUBE_RTMP = os.getenv("YOUTUBE_RTMP", "").strip()

CHECK_INTERVAL_OFFLINE = 10
RETRY_DELAY = 5
CLEANUP_DELAY = 3
PROBE_TIMEOUT = 25

stop_requested = False
active_processes = []


def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def wait_interruptibly(seconds):
    end = time.monotonic() + seconds
    while not stop_requested and time.monotonic() < end:
        time.sleep(min(1, max(0, end - time.monotonic())))


def stop_process(proc):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Process cleanup warning: {exc}")


def stop_all_processes():
    for proc in reversed(active_processes[:]):
        stop_process(proc)
    active_processes.clear()


def signal_handler(sig, frame):
    global stop_requested
    if not stop_requested:
        log("Shutdown requested.")
        stop_requested = True


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def get_stream_url():
    try:
        result = subprocess.run(
            ["streamlink", "--loglevel", "error", "--stream-timeout", "15",
             "--stream-url", TIKTOK_URL, "best"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=PROBE_TIMEOUT, check=False
        )
        url = result.stdout.strip()
        if result.returncode == 0 and url.startswith(("http://", "https://")):
            return url
        return None
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"TikTok probe failed: {exc}")
        return None


def detect_video_codec(stream_url):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", stream_url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=20, check=False
        )
        codec = result.stdout.strip().lower()
        if result.returncode == 0 and codec:
            return codec
        log(f"Could not identify source video codec; using H.264 encoding. {result.stderr.strip()[-300:]}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"ffprobe failed; using H.264 encoding: {exc}")
    return None


def is_tiktok_live():
    return get_stream_url() is not None


def run_streaming_session():
    started = time.monotonic()
    streamlink_proc = None
    ffmpeg_proc = None

    stream_url = get_stream_url()
    if not stream_url:
        log("TikTok stream is unavailable.")
        return 0

    codec = detect_video_codec(stream_url)
    # FLV/RTMP supports H.264 video reliably. Copy H.264 to avoid re-encoding;
    # transcode other/unknown codecs to H.264 so FFmpeg can write valid FLV.
    if codec in ("h264", "avc"):
        video_args = ["-c:v", "copy"]
        log(f"Source video codec: {codec}; copying video without re-encoding.")
    else:
        video_args = [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "main",
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0"
        ]
        log(f"Source video codec: {codec or 'unknown'}; encoding to H.264 for RTMP/FLV compatibility.")

    streamlink_cmd = [
        "streamlink", "--loglevel", "warning",
        "--ringbuffer-size", "128M",
        "--stream-segment-threads", "2",
        "--retry-streams", "1", "--retry-max", "1",
        "--stream-segment-attempts", "3",
        "--stream-segment-timeout", "15",
        "--stream-timeout", "20",
        "--http-header",
        "User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "--stdout", TIKTOK_URL, "best"
    ]

    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-fflags", "+genpts+discardcorrupt", "-i", "pipe:0",
        "-map", "0:v:0", *video_args,
        "-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k",
        "-ar", "44100", "-ac", "2",
        "-af", "aresample=async=1000:first_pts=0",
        "-max_interleave_delta", "1000000",
        "-flvflags", "no_duration_filesize", "-flush_packets", "1",
        "-f", "flv", YOUTUBE_RTMP
    ]

    try:
        if stop_requested:
            return 0
        log("Starting Streamlink...")
        streamlink_proc = subprocess.Popen(
            streamlink_cmd, stdout=subprocess.PIPE, stderr=None, bufsize=0
        )
        active_processes.append(streamlink_proc)

        log("Starting FFmpeg -> YouTube RTMP...")
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd, stdin=streamlink_proc.stdout,
            stdout=subprocess.DEVNULL, stderr=None, bufsize=0
        )
        active_processes.append(ffmpeg_proc)
        streamlink_proc.stdout.close()
        log("Relay session started.")

        while not stop_requested:
            if streamlink_proc.poll() is not None:
                log(f"Streamlink exited with code {streamlink_proc.returncode}.")
                break
            if ffmpeg_proc.poll() is not None:
                log(f"FFmpeg exited with code {ffmpeg_proc.returncode}.")
                break
            time.sleep(1)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Relay error: {exc}")
    finally:
        stop_process(ffmpeg_proc)
        stop_process(streamlink_proc)
        for proc in (ffmpeg_proc, streamlink_proc):
            if proc in active_processes:
                active_processes.remove(proc)
        if streamlink_proc and streamlink_proc.stdout:
            try:
                streamlink_proc.stdout.close()
            except OSError:
                pass

    duration = time.monotonic() - started
    log(f"Relay session closed after {duration:.1f} seconds.")
    return duration


def main():
    if not TIKTOK_URL:
        log("ERROR: TIKTOK_URL is empty.")
        sys.exit(1)
    if not YOUTUBE_RTMP or "YOUR_STREAM_KEY" in YOUTUBE_RTMP:
        log("ERROR: Set YOUTUBE_RTMP in Railway Variables to the full YouTube RTMP URL and stream key.")
        sys.exit(1)

    log("TikTok -> YouTube supervisor started.")
    log("Video: copy H.264 when compatible; otherwise encode H.264. Audio: AAC 128 kbps.")
    log("Automatic reconnection: enabled.")

    while not stop_requested:
        try:
            if not is_tiktok_live():
                log(f"TikTok offline/unavailable; checking again in {CHECK_INTERVAL_OFFLINE}s.")
                wait_interruptibly(CHECK_INTERVAL_OFFLINE)
                continue
            run_streaming_session()
            if not stop_requested:
                log(f"Checking for a new live in {CLEANUP_DELAY}s.")
                wait_interruptibly(CLEANUP_DELAY)
        except Exception as exc:
            log(f"Supervisor error: {exc}")
            wait_interruptibly(RETRY_DELAY)

    stop_all_processes()
    log("Supervisor stopped cleanly.")


if __name__ == "__main__":
    main()

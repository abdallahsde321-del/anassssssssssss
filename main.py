import sys
import time
import signal
import subprocess
from datetime import datetime

# =========================================================
# Configuration
# =========================================================

TIKTOK_URL = "https://www.tiktok.com/@anass_bn67/live"

YOUTUBE_RTMP = "rtmp://a.rtmp.youtube.com/live2/5czj-a0sq-5cmp-4aps-bq5h"

CHECK_INTERVAL_OFFLINE = 10
RETRY_DELAY = 5
CLEANUP_DELAY = 3
PROBE_TIMEOUT = 25

stop_requested = False
active_processes = []

# =========================================================
# Logging
# =========================================================

def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def wait_interruptibly(seconds):
    end = time.monotonic() + seconds
    while not stop_requested and time.monotonic() < end:
        time.sleep(min(1, max(0, end - time.monotonic())))

# =========================================================
# Process cleanup
# =========================================================

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

    except Exception as exc:
        log(f"Cleanup warning: {exc}")


def stop_all_processes():
    for proc in reversed(active_processes[:]):
        stop_process(proc)

    active_processes.clear()


def signal_handler(sig, frame):
    global stop_requested
    stop_requested = True
    log("Shutdown requested.")


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# =========================================================
# Detect TikTok live
# =========================================================

def get_stream_url():
    try:
        result = subprocess.run(
            [
                "streamlink",
                "--loglevel", "error",
                "--stream-timeout", "15",
                "--stream-url",
                TIKTOK_URL,
                "best"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROBE_TIMEOUT
        )

        url = result.stdout.strip()

        if result.returncode == 0 and url.startswith(("http://", "https://")):
            return url

        return None

    except Exception:
        return None


def is_live():
    url = get_stream_url()

    if url:
        log("TikTok LIVE detected.")
        return True

    log("TikTok is offline.")
    return False

# =========================================================
# Detect source codec
# =========================================================

def detect_codec(url):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                url
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20
        )

        codec = result.stdout.strip().lower()

        if codec:
            return codec

    except Exception:
        pass

    return None

# =========================================================
# Relay
# =========================================================

def relay_once():
    stream_url = get_stream_url()

    if not stream_url:
        return 0

    codec = detect_codec(stream_url)

    if codec == "h264":
        video_args = ["-c:v", "copy"]
        log("Video COPY (H.264)")
    else:
        video_args = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-g", "60",
            "-keyint_min", "60",
            "-sc_threshold", "0"
        ]
        log(f"Source codec {codec or 'unknown'} -> H.264")

    streamlink_cmd = [
        "streamlink",
        "--loglevel", "warning",
        "--ringbuffer-size", "128M",
        "--stream-segment-threads", "2",
        "--retry-streams", "1",
        "--retry-max", "1",
        "--stream-segment-attempts", "3",
        "--stream-segment-timeout", "15",
        "--stream-timeout", "20",
        "--http-header",
        "User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "--stdout",
        TIKTOK_URL,
        "best"
    ]

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",

        "-fflags", "+genpts+discardcorrupt",
        "-i", "pipe:0",

        "-map", "0:v:0",
        *video_args,

        "-map", "0:a:0?",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        "-af", "aresample=async=1000:first_pts=0",

        "-flvflags", "no_duration_filesize",
        "-flush_packets", "1",

        "-f", "flv",
        YOUTUBE_RTMP
    ]

    streamlink_proc = None
    ffmpeg_proc = None

    started = time.monotonic()

    try:
        log("Starting Streamlink...")

        streamlink_proc = subprocess.Popen(
            streamlink_cmd,
            stdout=subprocess.PIPE
        )

        active_processes.append(streamlink_proc)

        log("Starting FFmpeg...")

        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=streamlink_proc.stdout
        )

        active_processes.append(ffmpeg_proc)

        streamlink_proc.stdout.close()

        while not stop_requested:
            if streamlink_proc.poll() is not None:
                log(f"Streamlink exited ({streamlink_proc.returncode})")
                break

            if ffmpeg_proc.poll() is not None:
                log(f"FFmpeg exited ({ffmpeg_proc.returncode})")
                break

            time.sleep(1)

    finally:
        stop_process(ffmpeg_proc)
        stop_process(streamlink_proc)

        for p in (ffmpeg_proc, streamlink_proc):
            if p in active_processes:
                active_processes.remove(p)

    duration = time.monotonic() - started

    log(f"Session ended after {duration:.1f}s")

    return duration

# =========================================================
# Supervisor
# =========================================================

def main():
    log("TikTok -> YouTube Relay Started")
    log("Video: COPY when H.264")
    log("Audio: AAC 128k")
    log("Auto reconnect enabled")

    while not stop_requested:

        if not is_live():
            wait_interruptibly(CHECK_INTERVAL_OFFLINE)
            continue

        relay_once()

        if stop_requested:
            break

        log(f"Checking again in {CLEANUP_DELAY}s")
        wait_interruptibly(CLEANUP_DELAY)

    stop_all_processes()
    log("Stopped cleanly.")


if __name__ == "__main__":
    main()

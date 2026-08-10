#!/usr/bin/env python3
"""
STRATA 24/7 YouTube Live streamer — pure-ffmpeg edition.

No Chromium, no Xvfb, no Dockerfile. The video and audio are both
generated directly by ffmpeg itself:

  - video: an ffmpeg 'lavfi' color canvas (or a static background image,
    if you provide one) with an optional scrolling text overlay drawn by
    ffmpeg's own 'drawtext' filter — no browser involved.
  - audio: silent by default (lavfi 'anullsrc'), or an optional lavfi
    tone generator.

That's it — one subprocess (ffmpeg) doing everything, which is what
keeps this cheap enough to run on a shared GitHub Actions runner.

Supervision: an infinite loop restarts the ffmpeg process after any
failure (RTMP drop, network blip, ffmpeg crash), waiting
RECONNECT_DELAY_SECONDS between attempts, so the script never just dies.
"""

import os
import signal
import subprocess
import sys
import time

# --------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# --------------------------------------------------------------------------

YOUTUBE_STREAM_KEY = os.environ.get("YOUTUBE_STREAM_KEY", "").strip()
RTMP_BASE_URL = os.environ.get("RTMP_BASE_URL", "rtmp://a.rtmp.youtube.com/live2")

# Kept deliberately modest — GitHub Actions runners are shared 2-vCPU
# boxes, and this whole approach is meant to be cheap to encode.
STREAM_WIDTH = os.environ.get("STREAM_WIDTH", "1280")
STREAM_HEIGHT = os.environ.get("STREAM_HEIGHT", "720")
STREAM_FPS = os.environ.get("STREAM_FPS", "24")
STREAM_BITRATE = os.environ.get("STREAM_BITRATE", "2500k")

# Video source: either a static image (looped) or a plain color canvas.
BACKGROUND_IMAGE = os.environ.get("BACKGROUND_IMAGE", "").strip()  # path, optional
BACKGROUND_COLOR = os.environ.get("BACKGROUND_COLOR", "black")     # ffmpeg color name/hex

# Optional scrolling text overlay (drawn by ffmpeg's drawtext, no browser).
OVERLAY_TEXT = os.environ.get("OVERLAY_TEXT", "").strip()
SCROLL_SPEED = os.environ.get("SCROLL_SPEED", "40")  # px/s
FONT_FILE = os.environ.get(
    "FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
)
FONT_SIZE = os.environ.get("FONT_SIZE", "48")
FONT_COLOR = os.environ.get("FONT_COLOR", "white")

# Audio source: "silent" (default) or "tone".
AUDIO_MODE = os.environ.get("AUDIO_MODE", "silent").strip().lower()
TONE_FREQUENCY = os.environ.get("TONE_FREQUENCY", "440")

RECONNECT_DELAY_SECONDS = int(os.environ.get("RECONNECT_DELAY_SECONDS", "5"))

_shutdown_requested = False


def _handle_signal(signum, _frame):
    global _shutdown_requested
    print(f"[{_ts()}] received signal {signum}, shutting down...", flush=True)
    _shutdown_requested = True


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _bitrate_num(bitrate: str) -> int:
    return int(bitrate.lower().rstrip("k"))


def validate_config() -> None:
    if not YOUTUBE_STREAM_KEY:
        print(
            "FATAL: YOUTUBE_STREAM_KEY environment variable is not set.\n"
            "Set it as a GitHub Actions secret (Settings -> Secrets and "
            "variables -> Actions) and reference it in the workflow env.",
            file=sys.stderr,
        )
        sys.exit(1)

    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        print(
            "FATAL: ffmpeg not found on PATH. Install it "
            "(apt-get install -y ffmpeg) before running this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    if BACKGROUND_IMAGE and not os.path.isfile(BACKGROUND_IMAGE):
        print(
            f"FATAL: BACKGROUND_IMAGE is set to '{BACKGROUND_IMAGE}' but "
            "that file doesn't exist.",
            file=sys.stderr,
        )
        sys.exit(1)

    if OVERLAY_TEXT and not os.path.isfile(FONT_FILE):
        print(
            f"FATAL: FONT_FILE '{FONT_FILE}' not found — needed to draw "
            "OVERLAY_TEXT. Install fonts-dejavu-core, or set FONT_FILE to "
            "a font that exists on this runner.",
            file=sys.stderr,
        )
        sys.exit(1)


def build_ffmpeg_command() -> list[str]:
    rtmp_url = f"{RTMP_BASE_URL}/{YOUTUBE_STREAM_KEY}"
    bitrate_k = _bitrate_num(STREAM_BITRATE)
    bufsize = f"{bitrate_k * 2}k"
    gop = str(int(STREAM_FPS) * 2)

    cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-stats"]

    # --- video input: static image (looped) or a generated color canvas ---
    # '-re' paces input at native frame rate, which real-time RTMP needs —
    # without it ffmpeg would generate frames as fast as the CPU allows and
    # finish "instantly" instead of streaming continuously.
    if BACKGROUND_IMAGE:
        cmd += ["-re", "-loop", "1", "-i", BACKGROUND_IMAGE]
    else:
        cmd += [
            "-re", "-f", "lavfi",
            "-i", f"color=c={BACKGROUND_COLOR}:s={STREAM_WIDTH}x{STREAM_HEIGHT}:r={STREAM_FPS}",
        ]

    # --- audio input: silent, or a generated tone ---
    if AUDIO_MODE == "tone":
        cmd += ["-re", "-f", "lavfi", "-i", f"sine=frequency={TONE_FREQUENCY}:sample_rate=44100"]
    else:
        cmd += ["-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    # --- optional scrolling text overlay, drawn by ffmpeg itself ---
    if OVERLAY_TEXT:
        escaped_text = OVERLAY_TEXT.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        drawtext = (
            f"drawtext=fontfile={FONT_FILE}:text='{escaped_text}':"
            f"fontsize={FONT_SIZE}:fontcolor={FONT_COLOR}:"
            "x=(w-text_w)/2:"
            f"y=h-mod(t*{SCROLL_SPEED}\\,h+text_h):"
            "box=1:boxcolor=black@0.4:boxborderw=12"
        )
        cmd += ["-vf", drawtext]

    # --- encode + push ---
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-b:v", STREAM_BITRATE, "-maxrate", STREAM_BITRATE, "-bufsize", bufsize,
        "-pix_fmt", "yuv420p", "-g", gop, "-r", STREAM_FPS,
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-f", "flv", rtmp_url,
    ]
    return cmd


def run_once() -> int:
    """Runs a single ffmpeg attempt and returns its exit code."""
    cmd = build_ffmpeg_command()
    proc = subprocess.Popen(cmd)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=5)
        raise


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    validate_config()

    print("=== STRATA 24/7 streamer (pure ffmpeg) ===", flush=True)
    print(
        f"Resolution: {STREAM_WIDTH}x{STREAM_HEIGHT} @ {STREAM_FPS}fps | "
        f"bitrate {STREAM_BITRATE} | source: "
        f"{'image ' + BACKGROUND_IMAGE if BACKGROUND_IMAGE else 'color ' + BACKGROUND_COLOR}",
        flush=True,
    )
    print(f"Pushing to: {RTMP_BASE_URL}/<key hidden>", flush=True)

    while not _shutdown_requested:
        print(f"[{_ts()}] (re)starting ffmpeg...", flush=True)
        try:
            exit_code = run_once()
            print(f"[{_ts()}] ffmpeg exited (code {exit_code})", flush=True)
        except Exception as exc:  # noqa: BLE001 - keep the supervisor alive
            print(f"[{_ts()}] ERROR launching ffmpeg: {exc}", flush=True)

        if _shutdown_requested:
            break

        print(f"[{_ts()}] restarting in {RECONNECT_DELAY_SECONDS}s...", flush=True)
        time.sleep(RECONNECT_DELAY_SECONDS)

    print(f"[{_ts()}] shutdown complete.", flush=True)


if __name__ == "__main__":
    main()

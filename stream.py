#!/usr/bin/env python3
"""
Dino AI 24/7 YouTube Live streamer — browser-rendered edition, GitHub
Actions native (no Docker).

Pipeline:
    Xvfb (virtual display)
      -> Chromium, headful, loads public/dino-game.html (the T-Rex AI
         game + the "support me" banner, all self-contained in one file
         - real rendering, nothing faked with a static overlay)
         -> ffmpeg (x11grab captures that display + a silent audio track)
            -> RTMP -> YouTube Live

All three tools (Xvfb, Chromium, ffmpeg) are installed directly onto the
GitHub Actions 'ubuntu-latest' runner by the workflow — see
.github/workflows/stream.yml. Chromium's binary path comes from the
CHROMIUM_BINARY env var, which the workflow sets from the
browser-actions/setup-chrome action's output (apt-installing chromium
directly on Ubuntu runners is unreliable — it routes through snap and
often hangs in CI, so we avoid that).

Supervision: an infinite loop restarts the whole pipeline after any
failure (Chromium crash, dropped RTMP connection, network blip), waiting
RECONNECT_DELAY_SECONDS between attempts.
"""

import os
import shutil
import signal
import subprocess
import sys
import time

# --------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# --------------------------------------------------------------------------

YOUTUBE_STREAM_KEY = os.environ.get("YOUTUBE_STREAM_KEY", "").strip()
RTMP_BASE_URL = os.environ.get("RTMP_BASE_URL", "rtmp://a.rtmp.youtube.com/live2")

# Kept moderate by default — GitHub Actions runners are shared 2-vCPU
# boxes and now have to run Chromium *and* ffmpeg at once. Raise these
# only if the stream health panel in YouTube Studio looks clean.
STREAM_WIDTH = os.environ.get("STREAM_WIDTH", "1280")
STREAM_HEIGHT = os.environ.get("STREAM_HEIGHT", "720")
STREAM_FPS = os.environ.get("STREAM_FPS", "24")
STREAM_BITRATE = os.environ.get("STREAM_BITRATE", "3000k")

DISPLAY_NUM = os.environ.get("DISPLAY_NUM", "99")
DISPLAY = f":{DISPLAY_NUM}"

CHROMIUM_BINARY = os.environ.get("CHROMIUM_BINARY", "chromium")

# Path to the page that gets rendered and streamed. This is the combined
# dino-game.html - the T-Rex game, its AI trainer, and the CSS-driven
# support banner are all in this one file, so there's nothing else to
# point at and no query-string config it reads (the old STRATA panel's
# ?speed= param is gone along with the panel - the banner's scroll speed
# is just a fixed value in the file's own CSS now).
PAGE_PATH = os.environ.get(
    "PAGE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "dino-game.html"),
)

# Optional soundtrack to loop under the stream. Leave unset for silence.
AUDIO_PATH = os.environ.get("AUDIO_PATH", "").strip()
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "128k")

RECONNECT_DELAY_SECONDS = int(os.environ.get("RECONNECT_DELAY_SECONDS", "5"))
CHROME_WARMUP_SECONDS = int(os.environ.get("CHROME_WARMUP_SECONDS", "8"))
XVFB_WARMUP_SECONDS = int(os.environ.get("XVFB_WARMUP_SECONDS", "2"))

_shutdown_requested = False


def _handle_signal(signum, _frame):
    global _shutdown_requested
    print(f"[{_ts()}] received signal {signum}, shutting down...", flush=True)
    _shutdown_requested = True


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _bitrate_num(bitrate: str) -> int:
    return int(bitrate.lower().rstrip("k"))


def _terminate(proc: "subprocess.Popen | None", name: str, timeout: float = 5.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[{_ts()}] {name} did not exit in {timeout}s, killing", flush=True)
        proc.kill()
        proc.wait()
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup
        print(f"[{_ts()}] error stopping {name}: {exc}", flush=True)


def validate_config() -> None:
    if not YOUTUBE_STREAM_KEY:
        print(
            "FATAL: YOUTUBE_STREAM_KEY environment variable is not set.\n"
            "Set it as a GitHub Actions secret and reference it in the "
            "workflow env.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.path.isfile(PAGE_PATH):
        print(
            f"FATAL: page file not found at {PAGE_PATH}. Add your "
            "public/dino-game.html to the repo (set PAGE_PATH env var if "
            "it lives somewhere else).",
            file=sys.stderr,
        )
        sys.exit(1)

    if AUDIO_PATH and not os.path.isfile(AUDIO_PATH):
        print(
            f"FATAL: AUDIO_PATH is set to '{AUDIO_PATH}' but that file "
            "doesn't exist. Add your mp3/wav to the repo at that path, or "
            "clear AUDIO_PATH to stream silent.",
            file=sys.stderr,
        )
        sys.exit(1)

    for label, binary in (("Xvfb", "Xvfb"), ("Chromium", CHROMIUM_BINARY), ("ffmpeg", "ffmpeg")):
        if shutil.which(binary) is None:
            print(
                f"FATAL: required binary for {label} ('{binary}') not found "
                "on PATH. Check the workflow's install steps.",
                file=sys.stderr,
            )
            sys.exit(1)


def run_once() -> int:
    """
    Runs a single Xvfb -> Chromium -> ffmpeg pipeline attempt.
    Returns ffmpeg's exit code.
    """
    xvfb_proc = None
    chrome_proc = None
    ffmpeg_proc = None
    chrome_log = None

    try:
        # 1. Virtual display -------------------------------------------------
        try:
            os.remove(f"/tmp/.X{DISPLAY_NUM}-lock")
        except FileNotFoundError:
            pass

        xvfb_proc = subprocess.Popen(
            ["Xvfb", DISPLAY, "-screen", "0", f"{STREAM_WIDTH}x{STREAM_HEIGHT}x24", "-nolisten", "tcp"]
        )
        time.sleep(XVFB_WARMUP_SECONDS)

        # 2. Headful Chromium rendering the page ------------------------------
        page_url = f"file://{PAGE_PATH}"
        env = dict(os.environ, DISPLAY=DISPLAY)
        chrome_log = open("/tmp/chromium.log", "wb")
        chrome_proc = subprocess.Popen(
            [
                CHROMIUM_BINARY,
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-position=0,0",
                f"--window-size={STREAM_WIDTH},{STREAM_HEIGHT}",
                "--start-fullscreen",
                "--kiosk",
                "--noerrdialogs",
                "--disable-infobars",
                "--disable-session-crashed-bubble",
                "--disable-features=TranslateUI",
                "--autoplay-policy=no-user-gesture-required",
                "--no-first-run",
                "--user-data-dir=/tmp/chrome-profile",
                page_url,
            ],
            env=env,
            stdout=chrome_log,
            stderr=subprocess.STDOUT,
        )

        # Let fonts/animation settle before ffmpeg starts grabbing frames.
        time.sleep(CHROME_WARMUP_SECONDS)

        # 3. ffmpeg: capture the X11 display + audio, push RTMP --------------
        rtmp_url = f"{RTMP_BASE_URL}/{YOUTUBE_STREAM_KEY}"
        bitrate_k = _bitrate_num(STREAM_BITRATE)
        bufsize = f"{bitrate_k * 2}k"
        gop = str(int(STREAM_FPS) * 4)

        ffmpeg_cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-f", "x11grab", "-video_size", f"{STREAM_WIDTH}x{STREAM_HEIGHT}",
            "-framerate", STREAM_FPS, "-draw_mouse", "0", "-i", DISPLAY,
        ]

        if AUDIO_PATH:
            # -stream_loop -1 repeats the file forever; -re paces it at
            # real-time speed so it doesn't just blast through the whole
            # track's duration in a few seconds of wall-clock time.
            ffmpeg_cmd += ["-stream_loop", "-1", "-re", "-i", AUDIO_PATH]
        else:
            ffmpeg_cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

        # Explicit mapping: video from input 0 (the x11grab display),
        # audio from input 1 (the file or the silent generator) — this
        # removes any ambiguity about which stream ffmpeg picks by default.
        ffmpeg_cmd += ["-map", "0:v:0", "-map", "1:a:0"]

        ffmpeg_cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-b:v", STREAM_BITRATE, "-maxrate", STREAM_BITRATE, "-bufsize", bufsize,
            "-pix_fmt", "yuv420p", "-g", gop,
            "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "44100",
            "-f", "flv", rtmp_url,
        ]
        env_ffmpeg = dict(os.environ, DISPLAY=DISPLAY)
        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, env=env_ffmpeg)
        ffmpeg_exit = ffmpeg_proc.wait()
        ffmpeg_proc = None  # already exited
        return ffmpeg_exit

    finally:
        _terminate(ffmpeg_proc, "ffmpeg")
        _terminate(chrome_proc, "chromium")
        _terminate(xvfb_proc, "Xvfb")
        if chrome_log is not None:
            try:
                chrome_log.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    validate_config()

    print("=== Dino AI 24/7 streamer (browser-rendered, GitHub Actions) ===", flush=True)
    print(
        f"Resolution: {STREAM_WIDTH}x{STREAM_HEIGHT} @ {STREAM_FPS}fps | "
        f"bitrate {STREAM_BITRATE} | page: {PAGE_PATH}",
        flush=True,
    )
    print(f"Pushing to: {RTMP_BASE_URL}/<key hidden>", flush=True)
    if AUDIO_PATH:
        print(f"Audio: looping file '{AUDIO_PATH}'", flush=True)
    else:
        print("Audio: SILENT (AUDIO_PATH not set)", flush=True)

    while not _shutdown_requested:
        print(f"[{_ts()}] (re)starting pipeline...", flush=True)
        try:
            exit_code = run_once()
            print(f"[{_ts()}] ffmpeg exited (code {exit_code})", flush=True)
            try:
                with open("/tmp/chromium.log", "rb") as f:
                    tail = f.readlines()[-20:]
                print(f"[{_ts()}] tail of chromium log:", flush=True)
                for line in tail:
                    sys.stdout.buffer.write(line)
                sys.stdout.flush()
            except FileNotFoundError:
                pass
        except Exception as exc:  # noqa: BLE001 - keep the supervisor alive
            print(f"[{_ts()}] ERROR in pipeline: {exc}", flush=True)

        if _shutdown_requested:
            break

        print(f"[{_ts()}] restarting in {RECONNECT_DELAY_SECONDS}s...", flush=True)
        time.sleep(RECONNECT_DELAY_SECONDS)

    print(f"[{_ts()}] shutdown complete.", flush=True)


if __name__ == "__main__":
    main()

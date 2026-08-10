# Setup — STRATA 24/7 YouTube streamer on GitHub Actions (free tier)

## Why this can be free with no external host

No Koyeb/Render/Hugging Face — the runner itself is the compute. GitHub
gives **unlimited free Actions minutes on public repos**. A private repo
only gets ~2,000 free minutes/month, which a 24/7 job burns through in
under 3 days — so **the repo needs to be public** for this to actually be
free indefinitely.

## 1. Regenerate your stream key first

You shared a stream key in our conversation, which means it's now sitting
in plaintext chat history — treat it as compromised:

1. studio.youtube.com → Go live → Stream → regenerate the stream key.
2. Use the **new** key below, never the one you already pasted.

## 2. Create the repo and add the files

```
your-repo/
├── stream.py
├── .github/
│   └── workflows/
│       └── stream.yml
└── (optional) assets/background.jpg
```

Push `stream.py` and `.github/workflows/stream.yml` (both attached) to a
**public** GitHub repo.

## 3. Add your stream key as a secret

Repo → **Settings → Secrets and variables → Actions → New repository
secret**:

- Name: `YOUTUBE_STREAM_KEY`
- Value: your new stream key

Never put the key directly in `stream.yml` or any committed file — the
workflow reads it from `secrets.YOUTUBE_STREAM_KEY` at runtime, so it's
never written to disk or shown in logs.

## 4. (Optional) overlay text or a background image

Repo → **Settings → Secrets and variables → Actions → Variables tab**
(these are non-secret, plain repo Variables, not secrets):

- `OVERLAY_TEXT` — e.g. `Live 24/7 · your message here` — scrolls upward
  over the canvas, drawn by ffmpeg itself.
- `BACKGROUND_IMAGE` — a path to an image you've committed to the repo,
  e.g. `assets/background.jpg`, used as a static looped backdrop instead
  of a plain color.

Leave both unset and you get a plain black canvas with silent audio,
which is the lightest possible option.

## 5. Start it

- **Now:** repo → **Actions** tab → "STRATA 24/7 YouTube Stream" →
  **Run workflow**.
- **Automatically:** the `schedule: cron: "0 */5 * * *"` in the workflow
  restarts it every 5 hours on its own, no action needed after the first
  manual run.

Check the run's logs for:
```
=== STRATA 24/7 streamer (pure ffmpeg) ===
Resolution: 1280x720 @ 24fps | bitrate 2500k | source: color black
Pushing to: rtmp://a.rtmp.youtube.com/live2/<key hidden>
[timestamp] (re)starting ffmpeg...
```
YouTube Studio's stream health panel should show "Excellent"/"Good"
within 15–30s of that.

## Things worth knowing about this approach

- **GitHub-hosted jobs hard-cap at 6 hours.** That's why the workflow
  restarts every 5 hours via cron — there's no way around the cap, only
  around it.
- **Cron schedules on GitHub aren't exact** — expect the actual restart
  to land anywhere from a few seconds to a few minutes after the
  scheduled time, and during that gap the stream is briefly offline.
  YouTube will show it as ended/reconnecting, not crashed.
- **Scheduled workflows auto-disable after 60 days with no repo
  activity** (commits, manual runs, etc.). A `workflow_dispatch` run or
  any small commit resets that clock.
- **This isn't really what Actions minutes are meant for.** GitHub's
  Acceptable Use policies scope Actions to CI/CD work for the repo's own
  project, not general 24/7 hosting. Running a continuous stream this way
  is a known workaround people use, but it's outside the intended use
  case — GitHub has throttled or flagged accounts for non-CI/CD workloads
  before, so treat this as "works today, not guaranteed forever."

## Tuning reference

| Variable | Default | Notes |
|---|---|---|
| `YOUTUBE_STREAM_KEY` | *(required secret)* | From YouTube Studio |
| `RTMP_BASE_URL` | `rtmp://a.rtmp.youtube.com/live2` | Backup ingest URL if needed |
| `STREAM_WIDTH` / `STREAM_HEIGHT` | `1280` / `720` | Lower = cheaper on the shared runner CPU |
| `STREAM_FPS` | `24` | |
| `STREAM_BITRATE` | `2500k` | Raise cautiously — runners are shared 2-vCPU boxes |
| `BACKGROUND_COLOR` | `black` | Any ffmpeg color name or hex, used when no image is set |
| `BACKGROUND_IMAGE` | *(unset)* | Path to a committed image; overrides the color canvas |
| `OVERLAY_TEXT` | *(unset)* | Scrolling text drawn by ffmpeg's drawtext |
| `AUDIO_MODE` | `silent` | Or `tone` for a constant sine tone |
| `RECONNECT_DELAY_SECONDS` | `5` | Wait before retrying after any ffmpeg failure |

## Troubleshooting

- **Job fails immediately with "FATAL: YOUTUBE_STREAM_KEY..."** — the
  secret isn't set, or isn't named exactly `YOUTUBE_STREAM_KEY`.
- **"No data received" in YouTube Studio** — key may have been
  regenerated since you set the secret; update the secret to match.
- **Stream cuts out every ~5 hours for a bit** — expected, see the cron
  gap note above.
- **Choppy video** — lower `STREAM_FPS`/resolution/bitrate via repo
  Variables; shared runner CPU is the ceiling, not something to tune
  around.

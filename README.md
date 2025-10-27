# Newsletter to Podcast (Axios)

Minimal, deploy-ready Python 3.11 project to:
- Fetch Axios newsletter RSS daily
- Clean and normalize text
- Generate TTS audio (Google Cloud Text-to-Speech or OpenAI TTS)
- Produce Podcast 2.0-compatible RSS feed
- Publish static files to GitHub Pages (docs/)
- Automate via GitHub Actions on a schedule

## Quick Start

1) Configure GitHub Pages
- In your repo settings, enable Pages with source: `main` branch, `/docs` folder.
- Update `config.yaml` `site.link` to your Pages URL.

2) Choose TTS Provider
- The project supports Google TTS and OpenAI TTS. Set in `config.yaml` → `tts.provider` to `gcp` or `openai`.

2a) Google Cloud TTS Credentials (if using `gcp`)
- Create a service account with Text-to-Speech permission.
- Add the JSON key as a GitHub secret named `GCP_TTS_SERVICE_ACCOUNT_JSON` (full JSON content).

2b) OpenAI TTS and LLM
- Add a repository secret `OPENAI_API_KEY` (used for OpenAI TTS and LLM steps).
- Optional: enable audio-friendly rewrite: `llm.rewrite_enabled: true` and select `llm.rewrite_model`.

3) Configure
- Adjust `config.yaml` as needed (mode, naming, cleaning, provider/models).

4) Run locally (optional)
```
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m newsletter_to_podcast
```
- Environment: If running locally, set `GOOGLE_APPLICATION_CREDENTIALS` to a path with your service account JSON, or set env `GCP_TTS_SERVICE_ACCOUNT_JSON` to inline JSON.

5) GitHub Actions
- This repo includes `.github/workflows/publish.yml` scheduling daily runs.
- It writes the service account JSON to a temp file and runs the job, then commits `docs/` and `data/state.json` updates.

### Configure GitHub Actions

1) Enable GitHub Pages (Settings → Pages):
   - Source: `Deploy from a branch`
   - Branch: `main` / folder: `/docs`
   - Set `site.link` in `config.yaml` to your public Pages URL, e.g. `https://<user>.github.io/<repo>/`.

2) Add repository secrets (Settings → Secrets and variables → Actions → New repository secret):
   - `GCP_TTS_SERVICE_ACCOUNT_JSON` — full JSON of your Google Cloud TTS service account
   - `OPENAI_API_KEY` — only if `llm.enabled: true`

3) Trigger the workflow:
   - Manual: Actions → Build and Publish Podcast → Run workflow
   - Scheduled: runs daily via cron (see publish.yml)

Feed URL after first run: `https://<user>.github.io/<repo>/feed.xml`

### Submit to Spotify

- Go to https://podcasters.spotify.com → Create podcast → “I have a podcast” → paste your RSS feed URL.
- Verify ownership (email) and finish.
- Spotify will poll your feed; new episodes appear automatically after workflows run.

## Modes
- `compilation`: All new items of the day are merged into a single episode.
- `separate`: Each new item becomes an individual episode.

## Fail-safe Behavior
- If TTS fails or rate limits trigger, the run still updates RSS with a text-only episode (no enclosure), noting the failure reason in the description.

## Notes
- Dedup is based on `guid/link` + cleaned content hash to be idempotent and resume safely.
- Output files:
  - `docs/feed.xml` RSS
  - `docs/audio/...` MP3 files
  - `data/state.json` state for dedup/episodes

- Large repos: audio files grow repo size. For long-term use, consider object storage for MP3s and point `site.link` there.

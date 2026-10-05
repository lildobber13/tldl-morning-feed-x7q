# TLDL: Too Long, Didn't Listen

A private daily podcast made for one person. Every morning around 6:45 AM ET it:

1. pulls today's weather for Auburn Hills (Open-Meteo, free, no key)
2. pulls the latest TLDR issues (AI and Tech by default), skipping sponsors and job ads
3. pulls fresh IBM Newsroom items (says so when there is nothing new)
4. has Claude write a brand new conversational script, with rotating opening, tone, transition and sign-off styles, and memory of recent episodes so it never repeats itself
5. turns the script into audio with free Microsoft neural voices (edge-tts)
6. publishes the MP3 and an RSS feed on GitHub Pages, which Apple Podcasts follows

Cost: GitHub Actions, GitHub Pages and edge-tts are free. The only paid piece is the Claude API call, roughly one script per day (check current pricing for your model).

## One-time setup (about 10 minutes)

1. Create a new **public** GitHub repo (Pages is free for public repos). Upload everything in this folder, including the hidden `.github` folder.
2. Repo **Settings > Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`. Your site URL will be `https://<your-username>.github.io/<repo-name>`.
3. Repo **Settings > Secrets and variables > Actions**:
   - **Secrets** tab: add `ANTHROPIC_API_KEY` (from console.anthropic.com).
   - **Variables** tab: add `BASE_URL` = your site URL from step 2, with no trailing slash.
4. Repo **Actions > Daily TLDL > Run workflow**, tick "Rebuild today's episode", run it. It takes a few minutes. Check that `docs/feed.xml` and an MP3 appeared in the repo.
5. On your iPhone: **Podcasts > Library > ... (top right) > Follow a Show by URL**, paste `BASE_URL/feed.xml`. Turn on automatic downloads for the show so each episode is waiting at 7:30.

## Optional settings (repo Variables)

| Variable | Default | What it does |
| --- | --- | --- |
| `TLDR_TOPICS` | `ai,tech` | Comma list of TLDR newsletters (`ai`, `tech`, `webdev`, `infosec`, `product`, `founders`, ...) |
| `TTS_VOICE` | `en-US-AndrewNeural` | Any edge-tts voice (try `en-US-AvaNeural`, `en-US-AriaNeural`, `en-GB-RyanNeural`) |
| `TARGET_MINUTES` | `12` | Rough episode length |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Model used to write the script |

Other knobs (`LISTENER_NAME`, `PLACE`, `LAT`, `LON`, `KEEP_EPISODES`, `TTS_RATE`) are environment variables read at the top of `generate.py`.

## Testing locally

```
pip install -r requirements.txt
python generate.py --sources-only        # shows what was scraped, no API cost
ANTHROPIC_API_KEY=... python generate.py --force --no-audio   # writes only the script
```

## Notes

- Because the repo is public, anyone with the URL can fetch the feed and audio. The content is public news plus weather and a first-name greeting, but pick a repo name that is not obvious. The feed is also marked `itunes:block` so it stays out of podcast directories.
- Only the last 14 episodes are kept (about 4 MB each) so the repo stays small.
- GitHub scheduled runs can start 5 to 30 minutes late, which is why the job targets 6:45 AM for a 7:30 AM listen.
- If TLDR's archive page for today is not up yet, the newest issue from the last few days is used, and a given issue is never covered twice.
- If edge-tts is ever blocked from GitHub's servers, the script falls back to a plainer Google voice so you still get an episode.
- Scrapers depend on TLDR's and IBM's page layouts. If a source breaks, the episode still builds and the host says that source was unavailable; run `--sources-only` to debug.

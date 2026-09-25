# n8n Starter Workflows

Pre-built workflows for the bob stack. Import any `.json` file directly into n8n.

n8n and SearXNG are opt-in add-ons (n8n runs native, SearXNG runs in Docker). Start them before importing:
`bob services n8n start`, and for the workflows that cross-reference sources, `bob services searxng start`.

## How the workflows reach Bob

n8n runs natively on the host, so every workflow calls Bob's services on loopback at the default ports
from `config/defaults.json`: LiteLLM `http://127.0.0.1:8081`, STT `http://127.0.0.1:8082`, SearXNG
`http://127.0.0.1:8888`. If you changed a port in `config/user.json`, edit the URL in the matching HTTP
Request node after importing.

LiteLLM requests authenticate with an n8n credential, not with a key in the workflow. The two LiteLLM
HTTP Request nodes use the **Bob LiteLLM** credential (type Header Auth, id `bob-litellm`), which sends
`Authorization: Bearer <Bob's LiteLLM key>`. `bob services n8n start` creates or updates that credential
with `n8n import:credentials` before n8n starts, so n8n stores it encrypted under its own key. It re-imports
only when the LiteLLM key or the n8n encryption key changed: `.bob-litellm-credential.sha256` in the n8n
data dir (`tools/n8n-data`) holds a hash of the two, never the key. n8n's environment is not exposed to
workflows, so no expression can read a secret from it.

| Workflow | Calls | Auth |
|----------|-------|------|
| `daily-research-digest.json` | SearXNG `/search`, LiteLLM `/v1/chat/completions` | Bob LiteLLM credential |
| `vision-describe.json` | LiteLLM `/v1/chat/completions` with the `vision` role | Bob LiteLLM credential |
| `voice-transcribe.json` | STT `/v1/audio/transcriptions` (start it with `bob whisper start`) | none (the STT server has no auth) |

## How to import

1. Open `http://localhost:5678`
2. Top-right menu (three lines, top right) → **Import from file**
3. Select the `.json` file → **Import**
4. Open the workflow, set the **Config** node if it has one (the digest does), click **Save**
5. Toggle **Active** to enable scheduled runs

---

## vision-describe.json

A webhook at `/webhook/vision-describe` that takes a JSON body with a base64 `image` and an optional
`prompt` (read as `$json.body.image` and `$json.body.prompt`), sends them to the `vision` role, and returns
`{ "description": "..." }`.

```bash
curl -X POST http://localhost:5678/webhook/vision-describe \
  -H 'Content-Type: application/json' \
  -d "{\"image\": \"$(base64 -w0 screenshot.png)\", \"prompt\": \"Describe this image.\"}"
```

## voice-transcribe.json

A webhook at `/webhook/transcribe` that takes an audio file (WAV or MP3), transcribes it with Bob's
faster-whisper server, and returns `{ "text": "..." }`.

```bash
curl -X POST http://localhost:5678/webhook/transcribe -F "file=@recording.wav"
```

---

## daily-research-digest.json

Fetches RSS articles every morning, cross-references each one via SearXNG to check whether other sources cover the same topic, summarizes them one at a time with the local LLM, and posts to Discord as linked embeds. Articles seen in the last 7 days are skipped.

**Two modes, same workflow:**

| Trigger | How | What happens |
|---------|-----|-------------|
| Scheduled | Automatic at 8am | RSS → filter → deduplicate → verify → summarize → Discord |
| On-demand | POST to webhook | SearXNG search on a custom topic → verify → summarize → Discord |

### Setup

Open the **Config** node and set three values:

**`discord_url`**: your Discord webhook URL  
Discord > Server Settings > Integrations > Webhooks > New Webhook > Copy URL

**`rss_feed_url`**: RSS feed to monitor (default: Hacker News front page)  
To add more feeds: duplicate the `RSS: Fetch Feed` node and connect it to `Keyword Filter`

**`keywords_csv`**: optional comma-separated topic filter (leave empty for all articles)  
Example: `AI, open source, security, rust`

Other options in Config:

| Field | Default | Notes |
|-------|---------|-------|
| `max_items` | 8 | Max articles per run. Discord allows 10 embeds per message. |
| `model` | `chat` | Local model alias. `chat` is fast; `ponder` gives deeper analysis. |

### On-demand research via webhook

```bash
curl -X POST http://localhost:5678/webhook/research-digest \
  -H "Content-Type: application/json" \
  -d '{"topic": "llm quantization techniques"}'
```

### Discord output format

```
Daily Tech Digest - Tuesday, June 28, 2026 | 5 articles

LLM Inference Gets 40% Faster With New Quantization Method       [thumbnail]
New research from MIT demonstrates a quantization approach that reduces
memory usage by 40% with less than 1% accuracy loss on standard benchmarks.

Why it matters: Enables running 70B parameter models on consumer GPUs.

Footer: Verified: arxiv.org, theverge.com, arstechnica.com
```

Green border = SearXNG found coverage on at least one independent source.  
Blue border = single source, so the article is included but treat with more caution.

### Deduplication

Article URLs are stored in n8n workflow static data. Anything seen in the last 7 days is skipped on the next scheduled run. Static data is cleared if you delete the workflow or wipe n8n storage.

### Schedule

Edit the **Daily Schedule** node. Default cron: `0 8 * * *` (8am daily).  
The timezone is `n8nTimezone` (default `UTC`), a top-level key in `config/user.json`; restart n8n after changing it.

---

## Adding more RSS feeds

1. Duplicate the `RSS: Fetch Feed` node
2. Set a different URL in the duplicate
3. Connect the duplicate's output to `Keyword Filter` (n8n merges both inputs automatically)

Some feeds that work well with this workflow:

| Feed | URL |
|------|-----|
| Hacker News front page | `https://hnrss.org/frontpage` |
| HuggingFace blog | `https://huggingface.co/blog/feed.xml` |
| MIT Technology Review | `https://www.technologyreview.com/feed/` |
| The Gradient | `https://thegradient.pub/rss/` |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Discord not receiving messages | Invalid webhook URL | Check `discord_url` in Config node |
| SearXNG errors in execution log | Service not running | `bob services status`; open `http://localhost:8888` |
| LLM timeout or empty summary | Model still loading (llama-swap) | Wait 30s and re-run; or switch `model` to `chat` |
| LiteLLM returns 401 | The Bob LiteLLM credential is missing or stale | Restart n8n with `bob services n8n stop` then `bob services n8n start`; the start output names any import failure |
| All articles show "Single source" | SearXNG engines not returning results | Open `http://localhost:8888/preferences` and enable more engines |
| "No new articles" on every run | Dedup marked everything as seen | Clear workflow static data: workflow menu > Settings > Clear static data |
| Summaries start with "The article..." | Model ignoring system prompt | Switch `model` to `ponder` for better instruction following |

# n8n Starter Workflows

Pre-built workflows for the bob stack. Import any `.json` file directly into n8n.

n8n and SearXNG are opt-in add-ons (n8n runs native, SearXNG runs in Docker). Start them before importing:
`bob services n8n start`, and for the workflows that cross-reference sources, `bob services searxng start`.

## How to import

1. Open `http://localhost:5678`
2. Top-right menu (three lines, top right) → **Import from file**
3. Select the `.json` file → **Import**
4. Open the workflow, configure the **Config** node, click **Save**
5. Toggle **Active** to enable scheduled runs

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

```powershell
Invoke-RestMethod -Method POST `
  -Uri "http://localhost:5678/webhook/research-digest" `
  -Body '{"topic": "llm quantization techniques"}' `
  -ContentType "application/json"
```

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
The timezone follows n8n's `GENERIC_TIMEZONE` env var; set `n8nTimezone` in `config/user.json`.

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
| All articles show "Single source" | SearXNG engines not returning results | Open `http://localhost:8888/preferences` and enable more engines |
| "No new articles" on every run | Dedup marked everything as seen | Clear workflow static data: workflow menu > Settings > Clear static data |
| Summaries start with "The article..." | Model ignoring system prompt | Switch `model` to `ponder` for better instruction following |

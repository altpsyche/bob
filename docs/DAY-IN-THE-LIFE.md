# A Day with bob

This is a hands-on tour of every feature in the stack, structured as a typical working session. Follow it end-to-end the first time to see everything in action. After that, jump to any section as a quick reference.

**Prerequisites:** `install_prereqs.bat` + `setup.bat` (Windows) or `./install_prereqs.sh` + `./setup.sh` (Linux) have been run and completed successfully. You have an open terminal. Bob runs cross-platform under PowerShell 7; see [PORTABILITY.md](PORTABILITY.md).

---

## Contents

- [Morning: Starting Up](#morning-starting-up)
- [Feature 1: Bob Chat (Terminal Assistant)](#feature-1-bob-chat-terminal-assistant)
- [Feature 2: Open WebUI (Browser Chat)](#feature-2-open-webui-browser-chat)
- [Feature 3: Continue.dev (VS Code Autocomplete and Chat)](#feature-3-continuedev-vs-code-autocomplete-and-chat)
- [Feature 4: Cline (VS Code Agentic Edits)](#feature-4-cline-vs-code-agentic-edits)
- [Feature 5: Aider (Terminal Plan-then-Edit)](#feature-5-aider-terminal-plan-then-edit)
- [Feature 6: Fabric (Shell Pattern Pipes)](#feature-6-fabric-shell-pattern-pipes)
- [Feature 7: SearXNG (Private Web Search)](#feature-7-searxng-private-web-search)
- [Feature 8: n8n (Workflow Automation)](#feature-8-n8n-workflow-automation)
- [Feature 9: Langfuse (bob Observability)](#feature-9-langfuse-llm-observability)
- [Feature 10: Voice Loop (STT + TTS)](#feature-10-voice-loop-stt--tts)
- [Feature 11: Vision (Describe and Screenshot)](#feature-11-vision-describe-and-screenshot)
- [Feature 12: Bob Agent (Local Tool Use)](#feature-12-bob-agent-local-tool-use)
- [Feature 13: Plugins (summarise, draft, search, play)](#feature-13-plugins-summarise-draft-search-play)
- [Command Reference](#command-reference-everything-at-a-glance)
- [Evening: Wrapping Up](#evening-wrapping-up)
- [What to Try First](#what-to-try-first)

---

## Morning: Starting Up

### Start the stack

Open a terminal and run:

```powershell
bob up
```

This starts three things silently in the background:
- The **llama-swap engine** at `http://localhost:8080/v1`: local model server (llama.cpp)
- The **LiteLLM proxy** at `http://localhost:8081/v1`: all your AI tools point here (retry + pro models)
- **Open WebUI** at `http://localhost:3000`: a browser chat interface

Your browser opens automatically. If you'd rather it didn't:
```powershell
bob up -NoOpen
```

Check that everything is running:
```powershell
bob status
```

You should see seven models listed: `planner`, `coder`, `chat`, `fim`, `embed`, `vision`, `agent`. None are loaded into VRAM yet; they load on first use and stay there until idle. `fim` (autocomplete) and `embed` (search indexing) are pinned and never unload.

> **Pro models:** If you've set `DEEPSEEK_API_KEY` and `ZHIPU_API_KEY`, three additional models are available via the LiteLLM proxy at `:8081`: `chat-pro`, `planner-pro`, `coder-pro`. These route directly to DeepSeek and Zhipu APIs: no local GPU required, no platform fee. See [USAGE.md § Pro models](USAGE.md#pro-models-api-backed-no-platform-fee).

> **Tip:** To start everything (including Docker services) automatically at every login: on Windows create a Task Scheduler task set to "At log on"; on Linux add a user systemd unit or a `@reboot` cron entry, both running:
> `pwsh -File <repo>/scripts/up.ps1 -NoOpen`

### Start Docker services

The optional services (Langfuse, SearXNG, n8n) run in Docker. Make sure Docker Desktop is open (whale icon in the system tray), then:

```powershell
bob services start
bob services status
```

You should see four containers, all `Up`:
```
compose-langfuse-postgres-1   Up
compose-langfuse-1            Up
compose-searxng-1             Up
compose-n8n-1                 Up
```

The services are now available at:
- Langfuse: http://localhost:3001
- SearXNG: http://localhost:8888
- n8n: http://localhost:5678

---

## Feature 1: Bob Chat (Terminal Assistant)

**What it is:** A multi-turn conversational assistant in your terminal. Bob knows your name and work context (from onboarding), can route to the right model based on what you're doing, and supports optional memory recall.

### Start a conversation

```powershell
bob chat
```

Bob enters an interactive REPL:
```
Bob [chat | Qwen3-14B-Instruct-Q4_K_M]  (empty line to exit, !recall <query> to inject memory)

>
```

Type anything. Bob streams the response. Keep typing to continue the conversation. An empty line exits.

### Route to the right model

```powershell
bob chat            # default: chat (general conversation, Qwen3-14B)
bob think           # planner: Qwen3-30B, deep reasoning, thinking mode on
bob code            # coder: Qwen2.5-Coder-14B, code-focused
bob chat --pro      # chat-pro: DeepSeek V4 via API (needs DEEPSEEK_API_KEY)
bob think --pro     # planner-pro: DeepSeek R1 via API (strongest reasoning)
bob code --pro      # coder-pro: DeepSeek V4 via API
```

### One-shot from the terminal

No REPL, just pipe a question and get an answer:

```powershell
bob chat "what is the difference between a mutex and a semaphore?"
bob think "design a plugin architecture for a game engine"
bob code "write a PowerShell function that retries a script block N times with exponential backoff"
bob chat --pro "explain CAP theorem with a concrete example"
```

### Memory

Bob remembers across sessions, on by default (SQLite + BGE-M3, 0 extra VRAM). Store and query
explicitly:

```powershell
bob remember "working on a Unreal 5.4 game engine plugin called BobBot"
bob remember "prefer explicit error messages over silent failures"
bob recall "current project"   # blended-rank search; prints matching memories
bob memory list                # browse what Bob knows (typed: profile/preference/project/fact/…)
```

But mostly you don't manage it by hand. Open the `bob` shell and just talk:

```
bob                                    # the shell, persisted sessions
> I just switched the plugin over to Unreal 5.5
  Bob: Noted...
> /exit                                # leaving consolidates the session into memory
```

Next time you start a session, Bob injects your stable profile automatically, and a later
contradiction *supersedes* the old fact instead of piling up ("Unreal 5.4" → "Unreal 5.5" leaves only
5.5). Resume a past session with `/session list` then `/session resume <id>`. Retract everything one
session taught Bob with `bob memory forget --session <id>`.

Curate directly when you want to: `bob memory pin <id>` (protect a fact), `bob memory edit <id>
"..."`, `bob memory show <id>` (see which session taught it). For **per-repo, git-committable** rules,
drop a `BOB.md` in the project root. Bob reads it at session start.

Full reference: [MEMORY.md](MEMORY.md). Disable memory with `memory = @{ enabled = $false }` in
`config/bob.psd1`.

---

## Feature 2: Open WebUI (Browser Chat)

**What it is:** A full-featured chat interface in your browser, like ChatGPT but running locally.

Open http://localhost:3000. On first visit, create a local account (username and password stored locally, with no signup email or server involved).

Try a first message:
```
Explain what a hash map is in simple terms.
```

The `chat` model is used by default. You'll notice a brief pause before the first word appears; the model is loading into VRAM. Subsequent messages in the same session are much faster.

### Switching models

At the top of the chat, click the model name dropdown and switch to `planner`. This is the larger reasoning model, better for complex questions, architecture discussions, or anything where you want it to think carefully before answering.

Switch back to `coder` for programming questions. It's faster and more precise on code tasks.

### Thinking mode and /no_think

The `planner` and `chat` models use a reasoning scratchpad by default. Before writing a response they think through the problem silently. This produces better answers for hard questions, but adds latency.

For quick questions where you don't need deep reasoning:
```
What's the keyboard shortcut to close a tab in Chrome? /no_think
```

Adding `/no_think` at the end of your message skips the scratchpad. Use it for simple lookups. Leave it off for planning, debugging, or architecture questions.

### Document chat (RAG)

Open the sidebar and find **Workspace → Knowledge**. Upload any PDF, text file, or document. Once indexed, start a new chat and click the `+` icon to attach it as context. Ask questions about it:
```
What are the main conclusions in this document?
```

The `embed` model indexes the document locally. Nothing leaves your machine.

---

## Feature 3: Continue.dev (VS Code Autocomplete and Chat)

**What it is:** Two things inside VS Code: as-you-type autocomplete and a chat panel with access to your codebase.

Open VS Code. The Continue panel is in the left sidebar (the Continue icon, or press `Ctrl+L` to open the chat tab).

### Autocomplete

Open any source file and start typing a function. After a second or two, ghost text appears suggesting how to continue. Press `Tab` to accept, or keep typing to dismiss. This is the `fim` model: small, fast, and pinned in VRAM so it never causes a reload delay.

Try typing in a Python file:
```python
def calculate_fibonacci(n):
```
Ghost text will suggest the body. Tab to accept.

### Chat panel

Press `Ctrl+L` to open or focus the Continue chat. Type a question about your code:
```
How does this function handle edge cases?
```

To include a specific file as context, type `@` in the chat box:
```
@filesystem C:\my-project\src\parser.py what does the parse_line function do?
```

To include the current file automatically, select some code before pressing `Ctrl+L` and it's included as context.

### Web search in chat

With SearXNG running, type `@web` to pull in live search results:
```
@web latest Python async best practices 2025
```

Continue sends the query to your local SearXNG, gets the top results, and gives them to the model as context before answering. Your search never goes to Google directly.

### Inline edit

Select a block of code in your editor and press `Ctrl+I`. A text box appears; type an instruction:
```
add input validation: raise ValueError if the string is empty
```

A diff appears inline. Press `Ctrl+Enter` (or click Accept) to apply it, or `Ctrl+Del` to reject and try again.

### Switching between coder and planner

At the bottom of the Continue chat panel, there's a model dropdown. Use `coder` for everyday edits and quick questions. Switch to `planner` when you want to discuss architecture or get deeper reasoning. Switching models causes a brief VRAM swap (a few seconds).

---

## Feature 4: Cline (VS Code Agentic Edits)

**What it is:** An AI agent inside VS Code that reads files, writes files, runs commands, and works across many turns without you guiding each step.

Open the Cline panel (C icon in the sidebar). If you haven't configured it yet:
- Click the settings gear
- API Provider: `OpenAI Compatible`
- Base URL: `http://localhost:8081/v1`
- API Key: `sk-local` (anything non-empty)
- Model ID: `coder`
- Context window: `16384`

### Your first Cline task

Give it a specific, contained task. Cline works best when the goal is clear:
```
Add a --verbose flag to the CLI that prints each step to stderr as it runs.
Look at src/cli.py to understand the current structure first.
```

Cline will:
1. Read the relevant files
2. Show you what it plans to do
3. Wait for your approval before writing anything
4. Make the edits, then ask if you want to continue

Review what it's about to do before clicking **Approve**. If the plan looks wrong, type a correction.

### Plan mode vs Act mode

In Cline settings, enable **"Use different models for Plan and Act"**:
- Plan model: `planner`
- Act model: `coder`

With this on, Cline uses `planner` (the larger reasoning model) to figure out the approach, then switches to `coder` to write the actual code. This costs a VRAM swap between models, but the plans are significantly better for complex tasks.

> **Tip:** Keep Cline tasks focused. If the conversation history gets long, start a new task. Long histories consume context window quickly.

---

## Feature 5: Aider (Terminal Plan-then-Edit)

**What it is:** A terminal coding agent with a genuine planning step. `planner` describes what needs to change in plain English; `coder` turns that into file edits. You review the plan before any file is touched.

Open a terminal, navigate to a project, and run:

```powershell
cd C:\my-project
bob aider
```

### A typical aider session

```
> /add src/auth.py
> /read docs/auth-spec.md

> Add JWT token expiry validation. Raise AuthError with a clear message if the token is expired.
```

What happens:
1. `planner` reads the files and writes a prose description of the changes it will make
2. You see the plan in the terminal
3. Press **Enter** to proceed, or type feedback to refine the plan
4. `coder` generates the diff and applies it

```
> /diff       # see what's pending
> /undo       # roll back the last edit (reverts git commit)
> /drop src/auth.py    # remove from context when done
```

aider commits each accepted edit to git automatically. Work on a branch so `/undo` stays clean.

### When to use aider vs Cline

| | aider | Cline |
|---|---|---|
| Lives in | Terminal | VS Code |
| Plan review | Always explicit | Shown but faster to skip |
| File scope control | Manual (`/add`, `/drop`) | Cline decides |
| Best for | Careful, reviewable edits | Fast multi-step tasks |

---

## Feature 6: Fabric (Shell Pattern Pipes)

**What it is:** Named prompt patterns you pipe text through in the terminal. Instead of writing the same system prompt every time ("summarize this in bullet points, formatted as..."), you pipe to `fabric --pattern <name>`.

First-time setup (once):
```powershell
bob fabric-setup
```

### Common patterns

```powershell
# Write a commit message from your staged diff
git diff --staged | fabric --pattern write_git_commit

# Summarize any document
cat meeting-notes.txt | fabric --pattern summarize

# Extract key takeaways and action items
cat meeting-notes.txt | fabric --pattern extract_wisdom

# Review code quality
cat src/parser.py | fabric --pattern code_review

# Explain an error log
cat error.log | fabric --pattern explain_code
```

See all 254 patterns:
```powershell
fabric -l
```

Fabric uses the `coder` model by default. For deeper analysis:
```powershell
cat architecture-doc.md | fabric --pattern analyze_claims --model planner
```

---

## Feature 7: SearXNG (Private Web Search)

**What it is:** A self-hosted search engine at http://localhost:8888. You type a query, SearXNG sends it to Google, Bing, DuckDuckGo, and others in parallel, and shows you combined results. Your searches aren't linked to any account.

Open http://localhost:8888 and try a search. Results come from multiple engines simultaneously.

### Set it as your browser's default search

Go to your browser's settings → Search engines → Add:
- Name: `Local Search`
- Shortcut: `s`
- URL: `http://localhost:8888/search?q=%s`

Now type `s <query>` in the address bar to search privately.

### @web in Continue.dev

This is where SearXNG integrates with your coding workflow. In the Continue chat panel:

```
@web what are the breaking changes in Python 3.13?
@web site:github.com llama.cpp fix KV cache
@web FastAPI background tasks best practices
```

Continue queries SearXNG, includes the top results as context, then asks the model. So the model answers with current information, not just what it was trained on. This is especially useful for library releases, recent bug fixes, and anything that changes frequently.

If `@web` returns nothing, check that Docker services are running: `bob services status`.

---

## Feature 8: n8n (Workflow Automation)

**What it is:** A visual workflow builder at http://localhost:5678. Connect triggers (a schedule, a webhook, a file change) to actions (call the local LLM, send an email, post to Slack) without writing scripts.

Open http://localhost:5678. On first visit, create a local account.

### Import the starter workflow

A ready-to-import workflow is at `tools/n8n-workflows/daily-research-digest.json`. It runs daily at 8am, fetches RSS articles, cross-references each one via SearXNG, summarizes them with the local LLM, and posts Discord embeds with clickable links. Articles seen in the last 7 days are skipped automatically.

**Import steps:**
1. Open http://localhost:5678 → top-right menu (≡) → **Import from file**
2. Select `tools/n8n-workflows/daily-research-digest.json`
3. Open the workflow → click the **Config** node → set:
   - `discord_url`: your Discord webhook URL (Server Settings > Integrations > Webhooks > New Webhook > Copy URL)
   - `rss_feed_url`: feed to monitor (default: Hacker News front page)
   - `keywords_csv`: optional topic filter, comma-separated (empty = all articles)
   - `model`: which local model to use (`chat` is the default)
4. Click **Save** → toggle the workflow **Active**

**On-demand research mode**: POST a topic to get a one-off digest without waiting for the schedule:
```powershell
Invoke-RestMethod -Method POST `
  -Uri "http://localhost:5678/webhook/research-digest" `
  -Body '{"topic": "bob quantization techniques"}' `
  -ContentType "application/json"
```

See `tools/n8n-workflows/README.md` for troubleshooting and RSS customization tips.

### Build your own: Commit message generator

To understand how n8n works by building from scratch:

1. Click **New Workflow**
2. Add a **Webhook** trigger node → Method: `POST` → copy the webhook URL
3. Add an **HTTP Request** node:
   - Method: `POST`, URL: `http://host.docker.internal:8081/v1/chat/completions`
   - Header: `Authorization: Bearer sk-local`
   - Body (raw JSON): `{{ JSON.stringify({model: "coder", messages: [{role: "system", content: "Write a concise git commit message for this diff. Output only the message."}, {role: "user", content: $json.body.diff}]}) }}`
4. Add a **Set** node → extract `message` = `{{ $json.choices[0].message.content }}`
5. Add a **Respond to Webhook** node → click **Save** → **Activate**

Test from a terminal:
```powershell
$diff = git diff --staged
Invoke-RestMethod -Uri "http://localhost:5678/webhook/<your-id>" -Method POST `
  -Body (@{ diff = $diff } | ConvertTo-Json) -ContentType "application/json"
```

> Inside n8n containers, your machine is at `host.docker.internal`. Use `:8081` (LiteLLM proxy) for automatic retry, or `:8080` for the direct endpoint.

### More workflow ideas

| Workflow | Trigger | What it does |
|---|---|---|
| PR summary | GitHub webhook on PR open | Fetches the diff → asks `coder` → posts summary comment |
| Code review | Webhook from CI | Sends changed files to `coder` → returns review checklist |
| Release notes | Git tag push | Reads commit log → asks `planner` → writes formatted release notes |
| Chat memory | Webhook | Stores conversation history in n8n static data, calls `chat` model |

---

## Feature 9: Langfuse (bob Observability)

**What it is:** A dashboard at http://localhost:3001 that records every AI request routed through LiteLLM: the full prompt, response, latency, token counts, and retries. Useful for understanding what the model actually received (not what you think you sent), debugging unexpected answers, and seeing which workflows are expensive.

Default login: `admin@local.dev` / `admin123`

### Enabling tracing

Langfuse only captures requests routed through the LiteLLM proxy (port 8081). Direct requests to port 8080 are invisible. Here's how to wire it up:

**Step 1: Get API keys from Langfuse:**
1. Open http://localhost:3001
2. Go to **Settings → API Keys**
3. Click **Create API Key** and copy both the **Public Key** (`pk-lf-...`) and **Secret Key** (`sk-lf-...`)

**Step 2: Set API keys as environment variables:**

```powershell
$env:LANGFUSE_PUBLIC_KEY = 'pk-lf-...'   # paste your public key
$env:LANGFUSE_SECRET_KEY = 'sk-lf-...'   # paste your secret key
```

Add these to your PowerShell profile (`$PROFILE`) so they persist across sessions.

**Step 3: Enable Langfuse callbacks and regenerate config:**

Add one line to `config/user.psd1` (create it if it doesn't exist):
```powershell
@{ defaults = @{ langfuseEnabled = $true } }
```

Then regenerate and restart LiteLLM:
```powershell
bob gen
bob litellm stop
bob litellm -NoWindow
bob litellm status    # confirm it's running
```

> `config/litellm.yaml` is generated automatically; do not edit it directly. Use `user.psd1` + `bob gen` to make any persistent changes.

**Step 4: Confirm clients use :8081:**

All bundled clients (Continue, aider, Cline, fabric, Open WebUI, `bob chat`) are already configured for `:8081`. If you use a custom tool, set its API base to `http://localhost:8081/v1`.

**Step 5: Make a request and check Langfuse:**

```powershell
bob chat coder "explain what a mutex is"
```

Open http://localhost:3001 → **Traces**. Within a few seconds you'll see the request appear with the full prompt, the response, and timing information.

### Reading a trace

Click any trace to expand it. You'll see:
- **Input**: the exact messages the model received, including system prompt
- **Output**: the model's full response
- **Latency**: time to first token and total generation time
- **Token usage**: prompt tokens + completion tokens + cost estimate (at $0 since it's local, but useful for seeing what's expensive)

This is how you debug "why did the model respond like that?": you see the exact system prompt and conversation history, not your application's internal representation.

---

## Feature 10: Voice Loop (STT + TTS)

**Prerequisites:** Run `bob setup-voice` once (downloads whisper + piper + vision mmproj), then set `voice.enabled = $true` in `config/bob.psd1`. Restart with `bob up` so `bob up` auto-starts the whisper server.

Voice adds microphone input (whisper.cpp STT on port 8082) and speaker output (piper TTS) to the terminal. All processing is local.

### Try it: individual commands

```powershell
bob speak "Hello, I am Bob."                # synthesise text and play it
bob listen                                   # record mic until 1.5 s silence, print transcript
bob transcribe path\to\audio.wav            # transcribe a file instead of recording
```

### Try it: manual pipeline

```powershell
bob listen | bob chat | bob speak           # one turn: speak a question, hear the answer
```

### Try it: continuous voice loop

```powershell
bob voice              # runs until Ctrl+C: listen → chat → speak, repeat
bob voice --pro        # same loop but routes chat to the cloud (DeepSeek API)
bob voice --agent      # routes each voice turn through the full agent tool loop (Hermes 3 + tools)
```

Bob listens for speech, transcribes it, sends the text to the chat model, then reads the response aloud. The energy gate in `bob-voice-capture.py` swallows silent moments so near-silence doesn't produce empty transcripts.

The voice loop uses a dedicated system prompt (`voice.systemPrompt` in `config/bob.psd1`) that instructs the model to reply in plain spoken sentences: no asterisks, no bullet points, no markdown. A `Format-ForSpeech` sanitizer also strips any remaining markdown symbols before the text reaches piper, so Bob never reads `**bold**` or `- item` aloud.

**Tips:**
- Use headphones to stop the mic from picking up the speaker.
- Whisper small runs in ~300 ms on GPU after the first load. Silence detection threshold is configurable via `silenceSec` in `config/bob.psd1`.
- `voice.maxTokens` (default: `512`) caps reply length. Lower it for faster short answers.
- To wire `bob voice` audio through Open WebUI instead: `bob piper` starts a piper HTTP server on `:8083`; wire it in WebUI Admin Panel → Audio → Text-to-Speech Engine.

---

## Feature 11: Vision (Describe and Screenshot)

**Prerequisites:** The vision GGUF is downloaded by `bob fetch` (it's part of the 16gb profile). The mmproj is downloaded by `bob setup-voice`. Set `vision.enabled = $true` in `config/bob.psd1`.

Vision uses Qwen2-VL-7B to describe images and answer visual questions. The model loads on demand from the swap group and unloads after 30 s of idle.

### Try it: describe an image file

```powershell
bob describe C:\path\to\photo.jpg
bob describe C:\path\to\diagram.png "What does this diagram show?"
bob describe C:\path\to\diagram.png --pro "Analyse this architecture diagram in detail"
```

The `--pro` flag routes to DeepSeek V4 (which supports vision input natively) using your existing `DEEPSEEK_API_KEY`. Use it when you need stronger OCR, complex diagrams, or longer analysis than the local model produces.

### Try it: describe your screen

```powershell
bob screenshot
bob screenshot "What error is showing on screen?"
bob screenshot "Summarise the code visible in the editor"
bob screenshot --pro "Explain the code on screen in detail"
```

`bob screenshot` captures the primary display, resizes it to max 1024 px (so it fits in the 4096-token context), and sends it to the vision model. The temp PNG is deleted when the response finishes. `--pro` passes the same screenshot to DeepSeek cloud vision.

### How it works

Images are sent as `image_url` data URIs in the OpenAI chat completions format. They route through LiteLLM → llama-swap → a dedicated llama-server instance with `--mmproj` for the vision encoder. Flash attention is automatically disabled for the vision model (flash-attn is incompatible with multimodal projection in the current llama.cpp build; `gen-llama-swap.ps1` handles this transparently).

`--pro` skips llama-swap entirely and routes directly to the DeepSeek API via the `vision-pro` LiteLLM entry. No separate key needed; it uses `DEEPSEEK_API_KEY`.

---

## Feature 12: Bob Agent (Local Tool Use)

**What it is:** An autonomous agent loop that uses Hermes 3 (8B) locally. You give it a goal; it decides which tools to call, executes them, and iterates until it has a final answer. Everything (the reasoning, the tool calls, the results) stays on your machine.

**Prerequisites:** `bob up` is running. The `agent` model (Hermes 3) is included in the 16 GB profile; it loaded on first use. Run `bob doctor` to verify everything is wired.

### Try it: one-shot goals

```powershell
bob agent "what is the git status of this repo?"
```

You'll see the agent's thinking process in your terminal:
```
  → git_status({})
    M config/bob.psd1
    M scripts/bob_loop.py
    M scripts/tools/git.py

The repo has three modified files: config/bob.psd1, scripts/bob_loop.py, and scripts/tools/git.py.
```

Cyan lines (`→`) show tool calls. Dark gray shows the tool output. The final answer prints to stdout.

### Try it: multi-step reasoning

```powershell
bob agent "what were the last 5 commits and what files changed in the most recent one?"
```

The agent calls `git_log` and `git_diff` in the same step, then synthesises the answer from both results.

### Try it: web research

```powershell
bob agent "search for the latest llama.cpp release and summarise what changed"
```

The agent calls `web_search` (via SearXNG, no cloud, no tracking), fetches the top result with `web_fetch`, then summarises.

> SearXNG must be running: `bob services start`

### Try it: memory + reasoning

```powershell
bob agent "recall what I'm currently working on and suggest what to tackle next based on git status"
```

The agent calls `memory_recall` to pull context from your memory DB, then `git_status` to see the current state, then reasons over both.

### Try it: confirm mode (safest)

```powershell
bob agent --agency confirm "check git status and draft a commit message for the staged changes"
```

With `confirm`, the agent pauses before each tool call and asks `Execute? [y/N]`. Useful when you want to supervise every step.

### Schedule a background goal

```powershell
# Summarise git activity every morning at 09:00
bob agent schedule add morning-summary --cron "0 9 * * *" --goal "check git log for today and write a one-paragraph summary"
bob agent schedule list
```

The recurring `BobAgent` task (registered with `bob agent install`: a Windows Scheduled Task, or a cron entry on Linux) runs every minute and fires any due entries. Results are stored in `data/schedules.json`. The scheduler always runs in `silent` mode, with no terminal output.

### Save a web page to memory

```powershell
bob clip https://news.ycombinator.com/item?id=12345678
```

Fetches the page, strips HTML, summarises in 3 to 5 sentences, prints the summary, and stores `url: summary` to Bob's memory DB. Not an agent loop; one LLM call, very fast.

### Serve via HTTP (for n8n and Open WebUI)

```powershell
bob agent serve     # starts FastAPI on 127.0.0.1:8084; keep this terminal open
```

Exposes the agent loop as REST + SSE. Every endpoint except `/health` requires a Bearer token: the litellm key (`sk-local` by default) or any entry in `agent.apiTokens`:

```
POST http://localhost:8084/v1/agent/completions
Header: Authorization: Bearer sk-local
Body:   {"goal": "what is the git status?"}
Returns: {"result": "...", "session_id": null, "error": null}
```

For token-by-token streaming, POST the same body to `/v1/agent/completions/stream` (Server-Sent Events; the run cancels within ~1s if you disconnect). For a multi-turn conversation, create a session with `POST /v1/sessions` and pass its `session_id` on each call. Each token maps to an owner, and **sessions are owner-scoped**: a token can only see sessions its own owner created (another owner's `session_id` returns 404). Give distinct callers distinct `@{ token; owner }` entries in `agent.apiTokens`. Full endpoint contract + event schema: [AGENT-SERVER.md](AGENT-SERVER.md); security model + `0.0.0.0` checklist: [SECURITY.md](SECURITY.md).

Wire into n8n with an HTTP Request node: URL `http://host.docker.internal:8084/v1/agent/completions`, method POST, header `Authorization: Bearer sk-local`, body `{"goal": "{{ $json.goal }}"}`. Bind address and port are `agent.serveHost` / `agent.agentPort` in `config/bob.psd1` (loopback by default; set `serveHost = '0.0.0.0'` to expose on the LAN; keep `allowPrivateFetch = $false`).

> **Note:** Selecting the `agent` model directly in Open WebUI runs raw Hermes 3 inference without tool injection; `<tool_call>` blocks appear as plain text. Use `bob agent serve` for full tool use from WebUI via a custom function or n8n workflow.

### Check agent health

```powershell
bob setup check     # dependency + registration checks
bob doctor          # the above + runtime: endpoint reachable, GPU/VRAM, writable dirs, config.json parses
```

`bob setup check` prints yes or no for each agent dependency (venv, Python packages, model file, tool loading, services, scheduled task) with the exact fix command for anything that fails. `bob doctor` is the superset; run it first when something's off.

---

## Feature 13: Plugins (summarise, draft, search, play)

**What it is:** Drop-in CLI commands you call directly. Unlike agent tools (which the LLM calls autonomously), plugins run when you say so. Four are built-in; you can add your own by dropping a script in `plugins/<name>/`.

```powershell
bob plugins list    # see what's installed
```

### bob summarise

```powershell
# Summarise a file
bob summarise docs/USAGE.md
bob summarise docs/USAGE.md --length short

# Pipe text to it
cat meeting-notes.txt | bob summarise
git diff | bob summarise --length long
```

Useful after a long meeting, for a quick digest of a changelog, or to compress a big file before you read it.

### bob draft

```powershell
# Draft an email
bob draft "apologise for missing the deadline, new date is Thursday" --type email

# Draft a PR description
bob draft "add streaming support to the chat completions endpoint" --type pr

# Draft a Slack message
bob draft "let the team know the prod deploy is done, ask them to monitor errors" --type slack

# Free-form draft
bob draft "write a short bio for my GitHub profile"
```

Output is clean and paste-ready, with no "Here is a draft:" wrapper.

### bob search

```powershell
# Search the current directory
bob search "TODO"
bob search "error handling" --path src/
bob search "config loading" --ext .py

# Skip LLM, just show raw grep results
bob search "litellm" --raw
```

Runs ripgrep (or `findstr` as fallback), then the LLM summarises what it found and points to specific files and line numbers.

### bob play

```powershell
bob play lofi hip hop
bob play pink floyd the wall
bob play focus instrumental for coding
bob play --ytm jazz piano    # force YouTube Music
```

Opens Spotify via URI protocol if installed, otherwise opens YouTube Music in your browser. No API keys, no account needed.

---

## Command Reference: Everything at a Glance

### Inference

| Task | Command |
|---|---|
| Start everything | `bob up` |
| Start without browser | `bob up -NoOpen` |
| Start inference only | `bob serve` |
| Check what's running | `bob status` |
| Stop everything | `bob stop` |
| Tail logs | `bob logs` |

### Chat from terminal (Bob identity)

| Task | Command |
|---|---|
| Interactive REPL (default role) | `bob chat` |
| Interactive REPL with planner | `bob think` |
| Interactive REPL with coder | `bob code` |
| One-shot question | `bob chat "your question"` |
| One-shot with cloud | `bob chat --pro "your question"` |
| Legacy one-shot | `bob chat coder "your question"` |
| Skip the scratchpad | `bob chat "quick question /no_think"` |
| Store a memory | `bob remember "fact to remember"` |
| Search memories | `bob recall "query"` |
| Memory DB status | `bob memory status` |
| Spending summary | `bob budget` |

### Docker services

| Task | Command |
|---|---|
| Start services | `bob services start` |
| Stop services | `bob services stop` |
| Check status | `bob services status` |
| Tail logs | `bob services logs` |

### Models

| Task | Command |
|---|---|
| List models | `bob models` |
| Switch to 12gb profile | `bob profile 12gb` |
| Download missing models | `bob fetch` |
| Throughput benchmark | `bob bench` |

### Aider

| Command | What it does |
|---|---|
| `/add src/file.py` | Add file as editable |
| `/read docs/spec.md` | Add file as read-only reference |
| `/ask <question>` | Ask without triggering edits |
| `/undo` | Revert last committed edit |
| `/diff` | Show pending changes |
| `/drop src/file.py` | Remove from context |

### Fabric

| Task | Command |
|---|---|
| Commit message from staged diff | `git diff --staged \| fabric --pattern write_git_commit` |
| Summarize a document | `cat notes.txt \| fabric --pattern summarize` |
| Extract action items | `cat meeting.txt \| fabric --pattern extract_wisdom` |
| Code review | `cat file.py \| fabric --pattern code_review` |
| Explain an error | `cat error.log \| fabric --pattern explain_code` |
| List all patterns | `fabric -l` |
| Use planner model | `cat doc.md \| fabric --pattern analyze_claims --model planner` |

### LiteLLM proxy

| Task | Command |
|---|---|
| Start proxy in background | `bob litellm -NoWindow` |
| Check proxy is running | `bob litellm status` |
| Stop proxy | `bob litellm stop` |
| Start foreground (see logs) | `bob litellm` |

LiteLLM runs on port 8081 and starts automatically with `bob up`. All bundled clients default to `:8081`. Direct `:8080` (llama-swap) still works for local models but bypasses retry and Langfuse.

### Voice (Phase 2)

| Task | Command |
|---|---|
| One-time setup | `bob setup-voice` |
| Speak text aloud | `bob speak "text"` |
| Record mic → transcript | `bob listen` |
| Transcribe audio file | `bob transcribe file.wav` |
| Continuous voice loop | `bob voice` |
| Voice loop (cloud model) | `bob voice --pro` |
| Voice loop (full tool use) | `bob voice --agent` |
| Whisper server status | `bob whisper status` |
| Start piper HTTP server | `bob piper` |
| Stop piper HTTP server | `bob piper stop` |
| Piper server status | `bob piper status` |

### Vision (Phase 2)

| Task | Command |
|---|---|
| Describe an image file | `bob describe C:\path\to\img.png` |
| Ask a question about an image | `bob describe img.png "What text is visible?"` |
| Describe with cloud vision | `bob describe img.png --pro "Analyse this in detail"` |
| Describe current screen | `bob screenshot` |
| Ask about the screen | `bob screenshot "What error is showing?"` |
| Screenshot with cloud vision | `bob screenshot --pro "Explain the code on screen"` |

### Agent (Phase 3)

| Task | Command |
|---|---|
| Run a goal | `bob agent "your goal here"` |
| Run with confirmation | `bob agent --agency confirm "goal"` |
| Run silently (scripts) | `bob agent --agency silent "goal"` |
| Stream the answer | `bob agent "goal" --stream` |
| Clip a URL to memory | `bob clip <url>` |
| List tool modules | `bob tools list` |
| Check agent deps | `bob setup check` |
| Full pre-flight (deps + runtime) | `bob doctor` |
| Serve over HTTP (REST + SSE) | `bob agent serve` |
| View agent log | `bob agent log` |
| Add a scheduled goal | `bob agent schedule add name --cron "0 9 * * *" --goal "..."` |
| List scheduled goals | `bob agent schedule list` |
| Run a schedule now | `bob agent schedule run name` |
| Install BobAgent task | `bob agent install` |
| BobAgent task status | `bob agent status` |

### Plugins

| Task | Command |
|---|---|
| List installed plugins | `bob plugins list` |
| Summarise a file | `bob summarise <file>` |
| Summarise piped text | `cat file.txt \| bob summarise` |
| Draft an email | `bob draft "<prompt>" --type email` |
| Draft a PR description | `bob draft "<prompt>" --type pr` |
| Draft a Slack message | `bob draft "<prompt>" --type slack` |
| Search files + LLM synthesis | `bob search "<query>"` |
| Search files (raw output) | `bob search "<query>" --raw` |
| Play music (Spotify / YouTube Music) | `bob play <search query>` |

### Diagnostics

| Task | Command |
|---|---|
| Hardware + CUDA + model health | `bob diagnose` |
| Full agent + runtime pre-flight | `bob doctor` |
| Running processes (PID, RAM) | `bob ps` |
| Check model files on disk | `bob show coder` |
| Throughput benchmark | `bob bench` |

---

## Evening: Wrapping Up

Stop the inference stack to free VRAM:
```powershell
bob stop
```

Stop Docker services to free RAM (optional; they're lightweight, you can leave them running):
```powershell
bob services stop
```

Data is always preserved when you stop. Langfuse traces, n8n workflows, and model files are all on disk. `bob up` tomorrow picks up exactly where you left off.

---

## What to Try First

If this was your first read-through, here's a short sequence that touches every feature:

1. `bob up`: start the stack
2. `bob diagnose`: confirm GPU, CUDA, and model files are all healthy
3. `bob chat`: open the interactive REPL, type a question, get a streaming answer, empty line to exit
4. `bob think "design a plugin architecture for a game engine"`: one-shot with the planner
5. `bob remember "working on X project"` then `bob recall "current project"`: test memory store/search
6. Open http://localhost:3000: chat with Open WebUI, try `/no_think` on a simple question
7. Open VS Code: accept an autocomplete suggestion, try `Ctrl+I` on a block of code
8. Open the Continue panel (`Ctrl+L`): ask `@web what changed in the latest Python release?`
9. Open the Cline panel: give it a small contained task ("add a docstring to this function")
10. In a terminal: `cd C:\my-project && bob aider`: add a file with `/add`, ask for a change, review the plan
11. In a terminal: `git diff --staged | fabric --pattern write_git_commit`
12. `bob services start`: start Docker services
13. Open http://localhost:8888: do a search, set it as a browser shortcut
14. Open http://localhost:5678: create a webhook workflow that calls the LLM
15. Enable Langfuse tracing: set `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` env vars + `langfuseEnabled = $true` in `user.psd1` + `bob gen && bob litellm`, make a request, open http://localhost:3001 and look at the trace
16. `bob setup-voice` then `bob speak "Hello"`: test TTS; you should hear a response
17. `bob listen`: say a few words into the mic; the transcript should print
18. `bob voice`: run one full loop (speak a question, hear the answer back), then Ctrl+C
19. `bob piper`: start the piper HTTP server on `:8083` (used by Open WebUI for browser TTS)
20. `bob describe C:\Windows\Web\Wallpaper\Windows\img0.jpg`: describe the default Windows wallpaper using the vision model
21. `bob screenshot "What is on my screen?"`: take a live screenshot and describe it
22. `bob agent "what is the git status of this repo?"`: run your first agent goal, watch it call git_status and reason about the result
23. `bob agent --agency confirm "check the last 3 commits and summarise them"`: try confirm mode to supervise tool calls
24. `bob clip https://news.ycombinator.com`: clip a page to memory (fetch → summarise → store)
25. `bob doctor`: full pre-flight: verify agent deps + runtime (endpoint, GPU, writable dirs, config) are green
26. `bob plugins list`: see the four built-in plugins (summarise, draft, search, play)
27. `cat docs/USAGE.md | bob summarise --length short`: summarise a large file in 2-3 sentences
28. `bob draft "apologise for missing the deadline, new date Thursday" --type email`: draft a paste-ready email
29. `bob search "TODO" --path scripts/`: search for TODOs in the scripts folder, get an LLM synthesis
30. `bob play lofi hip hop`: open Spotify (or YouTube Music if Spotify isn't installed) with a search query
31. `bob stop`: shut down cleanly

For more detail on any feature: [USAGE.md](USAGE.md). For troubleshooting the Docker services: [USAGE.md § Docker troubleshooting](USAGE.md#troubleshooting-docker).

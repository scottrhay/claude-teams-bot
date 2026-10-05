# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Claude-Teams-Bot. Claude joins a Microsoft Teams meeting as a participant by driving the Teams web
client in Playwright Chromium, hears the meeting through Teams live captions, answers "Hey Claude, ..."
in the meeting chat (and aloud through Azure AI Speech when asked), and writes a transcript plus an
executive summary when the meeting ends. Built for leadership meetings: Windows, one meeting per
running instance. Human docs: `README.md` (overview), `docs/USER_GUIDE.md` (operator runbook),
`docs/ADMIN_GUIDE.md` (Teams prerequisites, provider, configuration, security, CLI reference). Keep them
in step with behavior changes.

## Commands

PowerShell, from this folder, always through the venv interpreter:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1          # one-time: .venv (Python 3.11), deps, Chromium
.\.venv\Scripts\python.exe -m pytest -q                       # full suite: offline, no model calls, ~4.5 min
.\.venv\Scripts\python.exe -m pytest -q tests/test_unit.py tests/test_app.py   # fast loop (skips e2e), seconds
.\.venv\Scripts\python.exe -m pytest -q tests/test_unit.py::test_wake_phrase_negatives
.\.venv\Scripts\python.exe teams_meeting_bot.py --help        # every bot flag and its default
```

These bill model calls, need a person at the keyboard, or act in a real meeting. Ask first:

```powershell
.\run.ps1 -Check                  # preflight: real API call, real agent run, real Azure TTS call
.\run.ps1 -Login                  # interactive sign-in for the bot account; writes teams_auth.json
.\run.ps1 -Url "<join link>" -Project "<materials folder>"   # joins; posts a notice and answers in chat
.\.venv\Scripts\pythonw.exe app.py                           # desktop app (wraps the same bot)
$env:RUN_GUI_TESTS = "1"; .\.venv\Scripts\python.exe -m pytest -q tests/test_app.py   # GUI e2e, real model
```

`run.ps1` and `app.py` load `foundry.env.ps1` when it exists, which sends every
model call and the Azure TTS calls to that Foundry resource, billed per token. Running
`teams_meeting_bot.py` directly does not load it (it then needs `ANTHROPIC_API_KEY`), and its `--outdir`
defaults to the current folder. `--console-only` never posts to the meeting chat.

There is no build step, linter or formatter.

## Architecture

### Two processes, line-based IPC

`app.py` (tkinter, runs under `pythonw`) launches `teams_meeting_bot.py` as a child process
(`python.exe -u`, piped stdin/stdout, no console window, `--browser embedded` so the browser is off-screen).

- **app → bot:** stdin lines, the same commands an operator types at the bot's console (`/leave`, `/chat`,
  `/say`, `/speak`, `/voice`, `/summary`, `/post-summary`, plain text = private question), plus
  Meeting-view input (`/click x y`, `/type`, `/key`, `/scroll x y dy`, in browser-viewport pixels).
  A new app feature means a new branch in `MeetingBot.handle_command`.
- **bot → app:** stdout log lines. `classify_line` sorts each one for the Activity feed: caption
  (`[ts] Name: text`), question (`Q (...)`), Claude's answer (`CLAUDE:` between `====` fences), technical
  detail (two-space indent after the timestamp), or bot event. Only bot events move the status pill
  (`STATUS_RULES`: "Listening.", "Waiting in the lobby", "ACTION NEEDED", "CRASH", "Transcript:", ...);
  captions, answers and details (tool inputs, spoken text) never do. "ACTION NEEDED: <what> (in <where>)"
  raises the amber banner (`action_text`) and switches to the Meeting view when `<where>` names it.
  **Rewording a `log()` message, or changing its indent or "Name: text" shape, can silently break the
  app.** A bot event shaped like "Word: text" must be listed in `NOT_SPEAKERS`. Log text is written for a
  non-technical operator; keep it that way.
- **Verbatim feed.** Lines enter the Activity `Text` widget unchanged; `feed_segments` only splits them into
  tagged runs (the `[` is elided, the `]` drawn invisible), so copy/paste and tests see exactly what the bot
  printed. Restyle with tags, never by rewriting the text.
- **Meeting view:** the bot writes `<outdir>/live_view.jpg` (atomic replace, every `--frame-interval` s);
  the app polls its mtime and maps canvas clicks back to the 1366x860 viewport (`fit_layout`, `canvas_to_page`).
- The app parses `foundry.env.ps1` with a regex (`load_env_file`: quoted `$env:NAME = "value"` lines only)
  instead of running PowerShell, while `run.ps1` dot-sources it. Keep that file to plain assignments.
- `clean_name` / `NAME_BAD_CHARS` (Teams display-name rules) are duplicated in both files.

### Desktop app look (`app_style.py`)

Colour tokens, fonts, every ttk style (on the `clam` theme) and `StatusPill` live in `app_style.py`; `app.py`
defines no colours or fonts of its own. `main()` calls `enable_dpi_awareness()` before `tk.Tk()` (otherwise
Windows bitmap-stretches the window at 125%+ and text blurs), and sizes go through `Theme.px()` (design pixels
at 96 DPI). Default fonts are set on the `TkDefaultFont` / `TkTextFont` named fonts: `option_add("*Font")`
would override every ttk label style's font. The header tabs are radio buttons driving a tabless
`ttk.Notebook`.

### Bot pipeline (`teams_meeting_bot.py`)

One file, top-down: selectors, injected JS, prompts, helpers, `MeetingAgent`, `MeetingBot`, CLI.

1. **Browser.** Chromium with fake media devices. `INIT_JS` runs before Teams loads: it swaps
   `getUserMedia` audio for a WebAudio stream the bot controls (`window.__claudeSpeak(wavBase64)`) and
   removes `msteams:` iframes so Chrome never offers to open the desktop app. Meeting audio is muted on
   this PC to prevent room echo (`--hear-meeting` overrides). The saved sign-in `teams_auth.json` is used
   when present; otherwise the bot falls back to joining as a guest.
2. **Join.** `direct_web_join` rewrites `/meet/` and `/l/meetup-join/` links straight to the web app
   (`/_#/...`, plus `anon=true` for guests) so the launcher never loads; `teams_web_url`, `direct_join_url`
   and `leave_launcher` cover other links. Then display name, mic and camera off, Join, lobby wait (`LOBBY_RE`).
3. **Hearing.** Captions are turned on (Alt+Shift+C, then the More menu, then the operator). `SCRAPER_JS`
   polls caption nodes every 300 ms and calls the exposed `claudeCaption` binding on change. `on_caption`
   drops the bot's own captions. `finalizer` emits a caption once it is unchanged for `--stable` seconds,
   and only its new words: Teams keeps appending to the same element while a speaker continues.
4. **Questions.** `WAKE` requires a hey/hi/ok prefix plus "Claude" or a caption mishear of it (cloud,
   clod, ...), so "Azure cloud" never fires; `WAKE_START` covers "Claude, ...". `check_wake` gathers the
   asker's following lines into `pending`; `due_question(now)` releases it once the asker is quiet
   (`--question-wait`, shorter when it ends in "?"); `dispatch` drops a same-speaker near-duplicate (prefix
   match) within 30 s.
5. **Answering** (`answer`) never leaves the meeting without a reply: the agent with tools, then on any
   error `claude()`, which tries the agent without tools and then the direct Messages API (`make_client`).
   "Say that out loud" (`REPEAT_ASK`) replays `last_answer` with no model call. A question `dispatch`
   took from the captions (`heard=True`; not `/chat`, not private) first gets `acknowledge`: "Got it,
   <asker> – "<question as heard>" Working on it." (`--no-ack` turns it off). It runs as a task beside
   the model call; the reply awaits it, so it always posts first, and if every model path fails the asker
   gets a "Sorry" note instead of silence. Every answer is appended to `self.transcript` as a `Claude:` line;
   that list is the bot's only memory, re-sent with each question and the input to the summary.
   `memory(private)` builds what the model sees: private Q&A (`to_chat=False`: plain console text, the
   app's "Ask privately") goes to `operator_private_*.md` instead of the transcript file and reaches only
   later private answers, never a public answer, the summary or the wrap-up. `private` is decided before
   `--console-only` masks `to_chat`, so console-only answers stay public. A new model call uses `memory()`
   unless it answers the operator privately; the `test_private_*` tests cover both model paths.
6. **Output.** `post_chat` strips markdown (`chat_text`) and types lines with Shift+Enter. Voice:
   `azure_tts` (plain REST), then `wait_for_floor` (captions quiet 1.5 s), unmute, `__claudeSpeak`, mute.
   `spoken_version` keeps the first 1-2 sentences.
7. **End.** `watch_end` ends the meeting when the Leave control is missing on 3 checks in a row (page-text
   matching could be triggered by speech) and re-enables captions if they disappear. `wrap_up` writes the
   summary, posts a chat wrap-up (not after Ctrl+C) and leaves.

UI failures go through `debug()` (screenshot + HTML in `<outdir>/bot_debug/`) and `manual()` (asks the
operator, unless `--headless`). `robust_click` clears overlays, then retries normally, by script, and forced.

### The agent (`MeetingAgent`, Claude Agent SDK)

One fresh `query()` per question, locked down: built-in tools only Read/Grep/Glob; `cwd` = the materials
folder (`--project`, default `agent/project/`); `allowed_tools` = only `mcp__<name>` for each server in
`agent/mcp.json` (none ships enabled; `agent/mcp.example.json` is the sample). The built-ins get no allow
rule on purpose: reads inside `cwd` need none, and a bare `Read` rule matches every file on the PC, so
`permission_mode="dontAsk"` refuses reads elsewhere. `strict_mcp_config=True`; `setting_sources=[]` (no user
or global Claude Code settings); `verbatim_prompts=True` (an `@path` in caption text is not expanded into the
file); `--no-session-persistence` (no session copy under `~/.claude/projects`); `cli_path=bundled_engine()`
(the claude.exe inside the pinned SDK wheel, see `requirements.txt`, not PATH's). Per-question limits from
`--agent-budget` (USD 2.00), `--agent-max-turns`, `--agent-timeout`; the summary and wrap-up get
`SUMMARY_BUDGET_USD`. A tripped limit raises and `answer()` falls back, so a limit never silences the bot.
There is no meeting or monthly limit by design: `MeetingAgent.spent` (the engine's cost estimates, failed
runs included) is logged after each answer and at the end. A model name the engine doesn't know (newer
than the engine, or a custom Foundry deployment name) is priced at a default rate (2x its Sonnet 5 rate)
and makes it print `[claude-code:unrecognized_model]` on stderr; `stderr=engine_note` turns engine
stderr into log details, since bare lines would show in the app's feed. Under
Foundry, `_env` pins every internal model role to the one deployment. `prepare_materials` (every start)
copies the text of .docx/.pptx/.xlsx/.pdf files into `_converted_text/` inside the materials folder: Read
can't open Office files, and Grep can't see inside PDFs (compressed streams). Each copy carries its
original's mtime, so a file converts again only when its mtime differs (newer or older); copies of deleted
files are removed. Unreadable files (encrypted, corrupt) get a note copy so they aren't retried until they
change; a missing library or an I/O error leaves no copy, so the next start retries.

### Prompts live in several places

- Agent path: `agent/instructions.md` (system prompt) and `AGENT_PROMPT` (per question).
- Direct-API fallback: `SYSTEM`, plus the `--context` file as `<background>`.
- Summaries: `SUMMARY_PROMPT` and `CHAT_SUMMARY_PROMPT`, sent through `claude()`, so normally by the
  tool-less agent with `instructions.md` as system prompt. The explicit Markdown format wins over its
  plain-text rule.

Answer-behavior rules must change in both `instructions.md` and `SYSTEM` (e.g. "never say you can't
speak"). `spoken_version` strips citations with `SOURCE_PAREN`, which expects the `(source: ...)` form
both prompts ask for.

## Model provider

Anthropic API by default (`ANTHROPIC_API_KEY`; `--model` defaults to `claude-sonnet-5-5`). Microsoft Foundry
when `CLAUDE_CODE_USE_FOUNDRY=1` with `ANTHROPIC_FOUNDRY_RESOURCE` (or `_BASE_URL`) and
`ANTHROPIC_FOUNDRY_API_KEY`; `--model` is then the deployment name, which `run.ps1` and
`app.py` take from `CLAUDE_FOUNDRY_DEPLOYMENT`. Keyless Entra ID is not supported: without the key
`make_client()` raises "Missing credentials" when `MeetingBot` starts, and preflight and `azure_tts` need it
too (only the agent subprocess would fall back to `az login`). Speech uses `SPEECH_RESOURCE` / `SPEECH_KEY` /
`SPEECH_ENDPOINT`, falling back to the Foundry resource and key. `foundry.env.ps1` holds a live key in
plaintext (gitignored): never print its values. `foundry.env.ps1.template`
is published, so it holds placeholders only.

## When Teams changes its UI

The most common break. Selectors are the `SEL` table (ordered candidates, first visible wins) and the
caption constants at the top of `teams_meeting_bot.py`, plus `MeetingBot.MIC_SELECTORS`, the
`#video-button` lookup in `ensure_silent`, and `LOBBY_RE`. To diagnose, read
`meetings/<stamp>/bot_<stamp>.log` (it logs `call controls: [...]` and the More-menu items) and the matching
`bot_debug/*.html` / `.png`. `tests/mock_teams.html` reproduces real Teams quirks (data-tid attributes, the
mic button's id and label flipping on mute, an overlay over Join, a launcher button that ignores early
clicks, a JS confirm dialog, captions that grow word by word); update it with any selector change.

## Tests

- `test_unit.py`: pure logic. Bots come from `make_bot(tmp_path, "--no-agent", ...)`; timing is tested by
  passing explicit times to `due_question(now)`.
- `test_e2e.py`: real headless Chromium against `mock_teams.html`, which scripts a whole meeting in real
  time (launcher, lobby, captions, end), so each test takes about a minute. Claude is faked by replacing `anthropic.AsyncAnthropic`,
  `bot.agent.ask` / `complete`, and `tts`.
- `test_app.py`: app helpers and the Activity feed (`classify_line`, `feed_segments`). Tests taking the `gui`
  fixture drive a real, hidden `App` with no bot by calling `show_line()` with bot lines. Create test windows
  with `new_root()`, never bare `tk.Tk()`: it starts Tk without std handles, as `pythonw` does. Otherwise Tcl
  keeps stdout/stderr channels on pytest's per-test capture files, and once Windows reuses those closed
  handles, later Tk startups fail at random ("Can't find a usable tk.tcl", "couldn't read file ... init.tcl:
  No error"). The GUI end-to-end test is skipped unless `RUN_GUI_TESTS=1` and makes real model calls.
- `test_agent_options_are_locked_down` reads the real `agent/mcp.json` and `instructions.md`: adding an MCP
  server or dropping the phrase "AI meeting assistant for the leadership team" means updating it.
- `test_engine.py` checks what the lock-down settings actually do, by running the real pinned claude.exe
  against `FakeModel`: a local HTTP server that speaks the Messages API (streamed) and plays scripted tool
  calls, with `CLAUDE_CONFIG_DIR` in a temp folder. Only the three tools are offered, a read outside the
  folder is denied, `@path` is not expanded, no session copy is written, the turn and spend limits end a
  run with an error (also for an unpriced deployment name), and engine stderr arrives through
  `engine_note`. No model calls, about 10 s. An engine upgrade means re-running it.

## Runtime files

Each run writes `meetings/<yyyy-MM-dd_HHmm>/`: `meeting_transcript_*.md` (appended live),
`operator_private_*.md` (private Q&A, once one is asked), `meeting_summary_*.md`, `bot_*.log` (also logs
private questions), `live_view.jpg`, `silence.wav`, `bot_debug/`. These are real, confidential meeting
records; the operator file and the log are not for attendees. `teams_auth.json` is the bot account's Teams session: treat it as a password.
App settings persist in `%APPDATA%\Claude-Teams-Bot\settings.json`. `agent/project/` holds fictional
sample data.

## Publishing (public GitHub repo)

The repo is public. `.gitignore` keeps out `foundry.env.ps1` and any `*foundry.env*` copy (OneDrive
conflict copies are named `foundry.env-<PC>.ps1`), `teams_auth*.json`, meeting records (including
`operator_private_*.md` and logs), `materials/`, `_converted_text/` and `bot_debug/`. Only
`foundry.env.ps1.template` is published, so it holds placeholders only; `agent/mcp.json` is published too,
so no tokens in it. Publish with git only: GitHub's drag-and-drop web upload ignores `.gitignore`. After
`git add -A`, before every push:

```powershell
git ls-files | Select-String 'foundry\.env|teams_auth|meetings/|bot_debug|materials/|_converted_text|meeting_(transcript|summary)_|operator_private|live_view|\.log$'   # only foundry.env.ps1.template
git grep --cached -nE 'sk-ant-[A-Za-z0-9_-]{10}|_(API_)?KEY *= *"[^<"]|Bearer [A-Za-z0-9._~+/=-]{20,}|[A-Za-z0-9]{80,}'   # no output
```

# Claude-Teams-Bot Admin Guide

This guide is for whoever installs and configures the bot: the Teams prerequisites, the model provider,
voice, knowledge folders, MCP servers, cost controls, security and maintenance. For running a meeting,
see the [User Guide](USER_GUIDE.md). For internals, see [CLAUDE.md](../CLAUDE.md).

## Contents

1. [How it fits together](#how-it-fits-together)
2. [Requirements](#requirements)
3. [Microsoft Teams prerequisites](#microsoft-teams-prerequisites)
4. [Install and update](#install-and-update)
5. [Bot account and sign-in](#bot-account-and-sign-in)
6. [Model provider](#model-provider)
7. [Voice (Azure AI Speech)](#voice-azure-ai-speech)
8. [Knowledge folders](#knowledge-folders)
9. [MCP servers](#mcp-servers)
10. [Agent instructions and lock-down](#agent-instructions-and-lock-down)
11. [Cost and latency controls](#cost-and-latency-controls)
12. [Security and data handling](#security-and-data-handling)
13. [Command-line reference](#command-line-reference)
14. [When Teams changes its UI](#when-teams-changes-its-ui)
15. [Testing](#testing)

## How it fits together

Everything runs on one Windows PC, the **meeting PC**:

- **`app.py`** is the desktop app. It starts **`teams_meeting_bot.py`** as a child process and talks to it
  over standard input and output.
- **The bot** drives a Playwright **Chromium** browser into the Teams web client. Chromium runs off-screen
  when the bot is started from the app. The bot reads live captions from the page and posts answers in
  the meeting chat.
- **Each question** starts one fresh, locked-down session of the **Claude Agent SDK**. The session can
  read the meeting's knowledge folder and call the MCP servers you list.
- **Voice** uses the Azure AI Speech REST API. The audio is played into the meeting through a virtual
  microphone.

The README has a table of [what leaves the meeting PC](../README.md#what-leaves-the-meeting-pc).

## Requirements

| Item | Detail |
|---|---|
| Meeting PC | Windows 10 or 11, awake and online for the whole meeting. No microphone, speakers or camera needed: the bot uses virtual media devices. |
| Python | 3.11 recommended (`setup.ps1` uses `py -3.11` when the Python launcher is installed); 3.10 or later works |
| Teams | A licensed Microsoft 365 account for the bot (recommended), or guest join (see below) |
| Claude | An Anthropic API key, or a Claude deployment in Microsoft Foundry |
| Voice (optional) | An Azure AI Speech resource, or a Foundry resource whose key also works for Speech |

The meeting PC needs outbound HTTPS to these hosts:

- Teams and Microsoft sign-in (`teams.microsoft.com` and the Microsoft 365 endpoints it uses).
- The model endpoint:
  - Anthropic API: `api.anthropic.com`.
  - Microsoft Foundry: `https://<resource>.services.ai.azure.com/anthropic/`.
- Speech: `https://<resource>.cognitiveservices.azure.com`, unless you set `SPEECH_ENDPOINT`.
- The URL of each MCP server you add to `agent/mcp.json`. None is enabled by default.

Setup also downloads Python packages from PyPI and a Chromium build through Playwright. The Claude
Agent SDK package (about 100 MB) carries the agent engine, pinned to the version the tests ran; it
needs 64-bit Windows.

## Microsoft Teams prerequisites

| Item | Why | Where |
|---|---|---|
| **A licensed bot account** whose display name contains "Meeting Assistant", for example "Claude Meeting Assistant" | It joins as an internal participant, so it usually skips the lobby, can use the chat and is auditable. The bot ignores captions from any speaker whose name contains "Meeting Assistant" or matches its Bot name. That is how it avoids transcribing its own voice. | Microsoft 365 admin center |
| **Live captions** available to the bot account | Captions are the only way the bot hears the meeting | Teams admin center → Meetings → Meeting policies → Recording & transcription → **Live captions**: any value except **Off**. The default lets users turn captions on. PowerShell: `Set-CsTeamsMeetingPolicy -Identity <policy> -LiveCaptionsEnabledType DisabledUserOverride` |
| **Meeting chat** on for the meeting | Answers are posted there | The organizer's meeting policy → Meeting engagement → **Meeting chat** |
| **Guest mode only:** anonymous join and chat | Without a saved sign-in, the bot joins as an anonymous guest | Teams admin center → Meetings → Meeting settings → **Anonymous users can join a meeting**. The organizer's policy must also allow anonymous join, and **Meeting chat** must not exclude anonymous users. Someone admits the bot from the lobby. |
| **Approval and attendee notice** | Meeting content goes to the model provider. Attendees should know an AI assistant is present. | Your governance process. The bot also posts a notice in the chat when it joins. |

- **Policy changes can take time.** Changes to Teams meeting policies can take up to 24 hours to take
  effect.
- **Some meetings can't be followed.** The bot can't follow a meeting where live captions are
  unavailable, such as an end-to-end encrypted meeting.

## Install and update

```powershell
git clone https://github.com/scottrhay/claude-teams-bot.git
cd claude-teams-bot
powershell -ExecutionPolicy Bypass -File .\setup.ps1              # .venv, packages, Chromium (nothing global)
powershell -ExecutionPolicy Bypass -File .\install_shortcut.ps1   # Desktop + Start menu shortcut
.\.venv\Scripts\python.exe -m pytest -q                           # offline suite; all must pass
```

Choose the install folder carefully. Meeting records are written to `meetings\` inside it. Avoid a
folder that syncs to cloud storage (OneDrive, Dropbox and the like) unless that is where your records
should live.

To update, run `git pull`, then run `setup.ps1` again (it picks up new dependencies), then run the test
suite.

## Bot account and sign-in

```powershell
.\run.ps1 -Login
```

1. Chromium opens Teams. Sign in as the bot account, including MFA.
2. When Teams has fully loaded, press Enter in the console.
3. The bot saves the browser session to `teams_auth.json`.

- **Treat `teams_auth.json` like a password.** Anyone who has it can act as the bot in Teams until the
  session expires. It is gitignored (`teams_auth*.json`).
- **Session lifetime** depends on your Microsoft Entra ID and Conditional Access policies. Sign in again
  when the bot lands on a sign-in page, or when **Check setup** reports the saved sign-in as absent.
  Signing in weekly before important meetings is a sensible default.
- **Conditional Access.** If your tenant only allows sign-ins from compliant devices, the meeting PC must
  meet that policy.
- **No saved sign-in.** Without `teams_auth.json`, the bot joins as a guest under the Bot name (`--name`).
  `-Guest` forces a guest join even when a sign-in is saved.

## Model provider

### Anthropic API (default)

```powershell
setx ANTHROPIC_API_KEY "<your-key>"     # then open a new terminal
```

The default model is `claude-sonnet-5-5`. Override it with `-Model` in `run.ps1` or `--model` on the
command line.

### Microsoft Foundry

Copy `foundry.env.ps1.template` to `foundry.env.ps1` in the bot folder and fill it in:

| Variable | Value |
|---|---|
| `CLAUDE_CODE_USE_FOUNDRY` | `"1"` turns Foundry on |
| `ANTHROPIC_FOUNDRY_RESOURCE` | The Foundry resource name. You can set `ANTHROPIC_FOUNDRY_BASE_URL` to a full endpoint URL instead. |
| `ANTHROPIC_FOUNDRY_API_KEY` | The resource key |
| `CLAUDE_FOUNDRY_DEPLOYMENT` | The Claude deployment name. `run.ps1` and the app pass it as `--model`. |

How each entry point uses the file:
- `run.ps1` dot-sources `foundry.env.ps1`.
- The desktop app parses it and reads only lines of the form `$env:NAME = "value"`, so keep the file to
  plain, quoted assignments.
- Running `teams_meeting_bot.py` directly does not load it. Set the variables in the environment
  yourself.

When Foundry is on:
- **Every model call goes to your deployment:** the agent, the fallback and the summary. The agent
  engine's internal model roles are pinned to the same deployment, so it never asks for a model you
  haven't deployed.
- **The key is required.** This version does not support keyless Microsoft Entra ID authentication.
- **Inference runs through your Azure subscription's Foundry resource.** Check Microsoft's and
  Anthropic's terms for data processing, and the regions and data zones available for your deployment.

### Storing keys

`foundry.env.ps1` holds the key in plain text on the meeting PC. It is gitignored, including OneDrive
conflict copies (`*foundry.env*`), and only the placeholder template is published. For production:
- Inject the variables from your secrets manager (for example Azure Key Vault) into the environment of
  the process that starts the bot. The bot reads only environment variables.
- Restrict who can read the bot folder.
- Rotate any key that may have been exposed.

The agent always runs with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, so it sends no telemetry or
error reports. It ignores user and machine-wide Claude Code settings.

## Voice (Azure AI Speech)

| Variable | Value |
|---|---|
| `SPEECH_RESOURCE` | Speech resource name. The endpoint is `https://<resource>.cognitiveservices.azure.com`. |
| `SPEECH_KEY` | Speech key |
| `SPEECH_ENDPOINT` | Optional: a full text-to-speech URL, which overrides the resource |

If these are unset, Speech uses the Foundry resource name and key. **Check setup** makes one short
text-to-speech call to confirm that it works.

| Setting | Default | Notes |
|---|---|---|
| `--voice` (app: Speak answers) | `asked` | `asked` speaks when a question says "out loud", "aloud", "tell us" and similar. `always` speaks every answer. `off` never speaks. |
| `--voice-name` | `en-US-AndrewNeural` | Any Azure neural voice name |

The bot speaks only after the captions have been quiet for 1.5 seconds. It unmutes only while speaking
and speaks the first one or two sentences, without citations. The full answer always goes to the chat.

The bot never plays the meeting's audio on the meeting PC, so its speakers can't echo into an open
microphone in the room. Use `--hear-meeting` only for troubleshooting.

## Knowledge folders

The knowledge folder is the meeting's material.
- **Where it's set.** The app's Knowledge folder field, `-Project` in `run.ps1`, or `--project`.
- **The default** is `agent/project/`, which holds fictional sample files.
- **What Claude does with it.** Claude searches the folder with Grep and Glob, reads files on demand and
  cites file names.

**Formats.** PDF, Word, PowerPoint, Excel, Markdown and text. Each time the bot starts, it converts new or
changed Word, PowerPoint, Excel and PDF files into searchable text in `_converted_text\` inside the
folder:
- PDFs are split by page.
- Copies of deleted or renamed files are removed.
- Scanned PDFs (images only) can't be searched, and password-protected files can't be read. The bot
  names such files in Activity.

The bot needs **write access** to the folder for `_converted_text\`. The agent itself has read-only
access.

> Put in the folder only what every attendee may see. Any attendee can ask about the folder's
> contents, and Claude can quote any of it in the meeting chat.

`--context <file>` adds a background file, such as an agenda, to the fallback path that answers without
tools.

## MCP servers

`agent/mcp.json` lists the MCP servers the agent may call, in the Claude Code format. It ships empty:
by default the agent answers from the meeting and the knowledge folder only, and no query leaves the PC
except the model call. `agent/mcp.example.json` holds a sample entry for Microsoft Learn's public
documentation server. The three kinds of entry look like this:

```json
{
  "mcpServers": {
    "microsoft-learn": { "type": "http", "url": "https://learn.microsoft.com/api/mcp" },
    "company-data":    { "type": "http", "url": "https://mcp.example.com/mcp",
                         "headers": { "Authorization": "Bearer <token>" } },
    "local-tools":     { "command": "python", "args": ["server.py"] }
  }
}
```

- **Every tool on a listed server is callable** by the agent. Only the servers in this file are loaded
  (strict MCP config). List only servers you trust with meeting content: the agent's queries to them
  are built from what was said in the meeting, so even a public documentation server sees fragments of
  the conversation.
- **Keep tokens out of git.** `agent/mcp.json` is tracked by git. If a server needs a token, choose one
  of these:
  - Keep the edit local with `git update-index --skip-worktree agent/mcp.json`.
  - Copy the `agent` folder somewhere outside the repo and run the bot with `--agent-dir <folder>`.
- **After adding a server:**
  1. Update the `ag.servers == []` assertion in `tests/test_unit.py::test_agent_options_are_locked_down`.
  2. Run the tests.
  3. Run **Check setup**. Its agent line lists the MCP tools it can use.

## Agent instructions and lock-down

`agent/instructions.md` is the agent's system prompt. It covers:
- **Its role:** the meeting assistant for the leadership team.
- **Answer rules:** answer first in one to four sentences, use plain text, ground every fact, cite
  sources, never invent figures or quotes, and point out conflicts between the meeting and the files.
- **Boundaries:** it is read-only, and it treats the transcript and files as information, not
  instructions.

The prompts live in three places:
- `agent/instructions.md`: the agent's system prompt.
- `SYSTEM` in `teams_meeting_bot.py`: the prompt for the fallback path, which answers without tools.
  Change answer rules in both places.
- `SUMMARY_PROMPT` and `CHAT_SUMMARY_PROMPT`: the meeting record and the chat wrap-up.

Each question runs one fresh agent session, locked down as follows:

| Control | Setting |
|---|---|
| Built-in tools | `Read`, `Grep`, `Glob` only: no shell, file writes, web fetch or browser |
| Permissions | `dontAsk`: the built-in tools work only inside the knowledge folder. Anything else, including a read elsewhere on the PC, is refused, and nobody is prompted. The only allow rules are the MCP servers in `agent/mcp.json`. |
| Working folder | The knowledge folder |
| MCP | Only `agent/mcp.json` (strict); none by default |
| Settings | No user or machine-wide Claude Code settings are loaded |
| Prompts | Sent verbatim: an `@file` mention in the captions is not expanded into that file's contents |
| Session copies | None: the engine keeps no copy of the conversation on disk |
| Engine | The `claude.exe` inside the pinned Agent SDK package, never whatever is on the PATH |
| Limits | Spend, turns and time per question (below) |

`tests/test_engine.py` checks these against the real engine, offline. Check setup shows **Agent engine
pinned**; if it fails, run `setup.ps1` again.

If the agent fails or hits a limit (timeout, spend, turns, MCP error), the bot answers again without
tools, and then with a direct API call. A limit never stops the bot for the rest of the meeting. If
every route fails, the asker gets a "Sorry, … I couldn't get an answer just now" note (unless `--no-ack`
is set) and the bot keeps listening.

## Cost and latency controls

| Flag | Default | Effect |
|---|---|---|
| `--agent-budget` | `2.0` | USD backstop per question. A typical answer costs $0.01 to $0.06, so this only catches a runaway. When it trips, the bot answers without tools. |
| `--agent-max-turns` | `12` | Maximum agent turns per question. When it's reached, the bot answers without tools. |
| `--agent-timeout` | `120` | Seconds per question before the bot falls back to answering without tools |
| `--no-ack` | off | Don't post the "Got it" note. By default the note goes in the chat as soon as a question is taken, quoting it as the captions heard it. |

What makes billed calls:
- **Each question:** an agent run, plus any fallback call.
- **The end of the meeting:** the summary and the chat wrap-up.
- **Check setup:** one short model call, one agent run and one text-to-speech call.
- **The GUI end-to-end test:** it runs only when `RUN_GUI_TESTS=1` is set.

The offline test suite makes no model calls. In testing, a typical question took 4 to 8 seconds and cost
$0.01 to $0.05.

The summary and the chat wrap-up each get their own $5.00 limit (`SUMMARY_BUDGET_USD`), since they
read the whole transcript.

**There is no meeting or monthly limit, by design.** A limit that tripped mid-meeting would silence the
bot when it matters most. The log shows the running spend instead, after each answer
(`agent done: … cost=$0.023 (meeting so far: $0.41)`) and in total when the meeting ends
(`model spend this meeting: about $…`). These figures are the agent engine's own estimates, so treat
them as a guide, not a bill. When the engine doesn't recognize the model name (a model newer than the
engine, or a Foundry deployment with its own name), it uses a default rate, twice its Sonnet 5 rate,
so the figures run high, not low.

Watch real spend at the provider with alerts, not hard stops, so nothing cuts the bot off mid-meeting:
- **Azure:**
  - Put a cost budget on the resource group, with alert thresholds (for example 50%, 80% and 100%)
    sent to the owner. Azure budgets only send alerts; they never stop a resource.
  - Check that the subscription has no spending limit. Credit-based subscriptions (Visual Studio,
    free trial, sponsorship) have one on by default, and Azure disables the subscription's resources
    when the credit runs out.
  - Give the deployment a tokens-per-minute quota well above a meeting's peak. Each question sends the
    transcript so far (roughly 12,000 tokens per hour of meeting) plus the files the agent reads, and an
    agent run makes several calls. Calls over the quota fail; the bot falls back, but answers get
    worse.
- **Anthropic:** a workspace spend limit stops all calls once it's reached. Set it well above a month's
  expected use and check usage in the Console.

## Security and data handling

- **Meeting records** are written to `meetings\<yyyy-MM-dd_HHmm>\`:
  - the transcript;
  - the summary;
  - `operator_private_*.md`;
  - `bot_*.log`;
  - `live_view.jpg`, a frame of the bot's browser;
  - `bot_debug\`.

  They are confidential and never deleted automatically. Set a retention practice and delete old
  folders.
- **Private operator questions** never reach the meeting chat, the summary or the wrap-up. They are
  saved to `operator_private_*.md`, and the log also lists them. Neither file is for attendees.
- **Secrets on disk:**
  - `foundry.env.ps1`: the model and Speech key.
  - `teams_auth.json`: the bot's Teams session.

  Both are gitignored. Protect them with folder permissions or a secrets manager (see
  [Storing keys](#storing-keys)).
- **Prompt injection.** The agent treats the transcript and files as information, not instructions. It
  can't write files, run commands, browse the web or send anything except the answer the bot posts. It
  can still repeat what it can read. Choose the knowledge folder and MCP servers with that in mind.
- **Transparency.** The bot posts a join notice in the chat. `--no-intro` turns it off; leave it on
  unless attendees are told another way.
- **Before you push to a public fork,** check that git tracks no records or secrets:

  ```powershell
  git ls-files | Select-String 'foundry\.env|teams_auth|meetings/|bot_debug|materials/|_converted_text|meeting_(transcript|summary)_|operator_private|live_view|\.log$'   # only foundry.env.ps1.template
  git grep --cached -nE 'sk-ant-[A-Za-z0-9_-]{10}|_(API_)?KEY *= *"[^<"]|Bearer [A-Za-z0-9._~+/=-]{20,}|[A-Za-z0-9]{80,}'   # no output
  ```

  Use git to publish. GitHub's drag-and-drop web upload ignores `.gitignore`.

## Command-line reference

### `run.ps1`

`run.ps1` loads `foundry.env.ps1` when the file exists. It writes to `meetings\<yyyy-MM-dd_HHmm>\`, and
passes `CLAUDE_FOUNDRY_DEPLOYMENT` as the model unless you set `-Model`.

| Parameter | Effect |
|---|---|
| `-Url "<link>"` | Join this meeting |
| `-Project "<folder>"` | Knowledge folder for this meeting |
| `-Guest` | Join as a guest even when a sign-in is saved |
| `-Context "<file>"` | Background file for the fallback path |
| `-Model "<name>"` | Model, or Foundry deployment name |
| `-Login` | Sign the bot account in and save `teams_auth.json` |
| `-Check` | Preflight: Python, Chromium, provider settings, a model call, the pinned agent engine, an agent run (files and MCP), Speech, saved sign-in |

### `teams_meeting_bot.py`

Run it with `.\.venv\Scripts\python.exe teams_meeting_bot.py`. It does not load `foundry.env.ps1`, and
`--outdir` defaults to the current folder.

| Flag | Default | Effect |
|---|---|---|
| `--url` | | Teams meeting join link |
| `--login` / `--check` | | Sign in / preflight |
| `--auth` | `teams_auth.json` | Saved sign-in file |
| `--guest` | off | Join anonymously as `--name` |
| `--name` | `Claude Meeting Assistant` | Display name for a guest join (letters, numbers, spaces, `- ' . _ @`) |
| `--voice` | `asked` | `off`, `asked` or `always` |
| `--voice-name` | `en-US-AndrewNeural` | Azure AI Speech voice |
| `--browser` | `window` | `embedded` runs the browser off-screen (the app uses this) |
| `--no-live-view` | | Don't write `live_view.jpg` frames |
| `--frame-interval` | `1.0` | Seconds between live frames |
| `--context` | | Background file for the fallback path |
| `--model` | `claude-sonnet-5-5` | Model, or Foundry deployment name |
| `--headless` | off | No visible browser and no manual fallback |
| `--console-only` | off | Never post to the meeting chat |
| `--no-intro` | | Skip the join notice |
| `--no-post-summary` | | Don't post the wrap-up in the chat |
| `--stable` | `1.2` | Seconds a caption must stay unchanged to count as final |
| `--hear-meeting` | off | Play the meeting's audio on this PC (troubleshooting only) |
| `--question-wait` | `1.8` | Seconds the asker must be quiet before a question is answered |
| `--question-wait-short` | `0.6` | The same, when the caption already ends with "?" |
| `--lobby-timeout` | `20` | Minutes to wait for admission |
| `--end-check` | `5` | Seconds between end-of-meeting checks |
| `--agent-dir` | `agent` | Folder with `instructions.md`, `project/` and `mcp.json` |
| `--project` | `<agent-dir>/project` | Knowledge folder |
| `--no-agent` | off | Transcript-only answers (no tools) |
| `--agent-max-turns` / `--agent-budget` / `--agent-timeout` | `12` / `2.0` / `120` | Per-question limits; tripping one falls back to an answer without tools |
| `--no-ack` | off | Don't post "Got it" in the chat when a question is heard |
| `--outdir` | `.` | Where the transcript, summary and log are written |

## When Teams changes its UI

This is the most common break. The usual symptoms are:
- an **Action needed** prompt for a step that used to be automatic;
- no captions arriving;
- a stop with an error.

**To diagnose,** open the meeting's folder:
- `bot_*.log` lists the call controls and the More-menu items the bot found.
- `bot_debug\*.png` and `.html` capture the page at the failing step.

**To fix,** update the selectors at the top of `teams_meeting_bot.py`:
- The `SEL` table holds ordered candidates per step; the first visible match wins.
- The caption constants: `CAPTION_TEXT`, `CAPTION_AUTHOR`, `CAPTION_ITEM`.
- `MeetingBot.MIC_SELECTORS`.
- The `#video-button` lookup in `ensure_silent`.
- `LOBBY_RE`.

Then update `tests/mock_teams.html` to reproduce the new UI, run the test suite, and hold a short test
meeting.

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest -q                                        # full offline suite, ~5-6 min
.\.venv\Scripts\python.exe -m pytest -q tests/test_unit.py tests/test_app.py   # fast loop
$env:RUN_GUI_TESTS = "1"; .\.venv\Scripts\python.exe -m pytest -q tests/test_app.py   # GUI end-to-end, billed
```

The end-to-end tests run whole meetings in headless Chromium against `tests/mock_teams.html`, with
Claude replaced by fakes. [CLAUDE.md](../CLAUDE.md#tests) describes each test file.

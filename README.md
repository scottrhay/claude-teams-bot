# Claude-Teams-Bot

Claude joins a Microsoft Teams meeting as a participant. It follows the conversation through Teams live
captions, answers when anyone says **"Hey Claude, ..."**, and writes the transcript and an executive
summary when the meeting ends.

> **Status: proof of concept.** It runs on Windows and handles one meeting per running instance. It is
> built for a few high-value meetings, such as leadership and executive reviews, not for scale.

## What it does

- **Listens through live captions.** The bot reads Teams captions and speaker names straight from the
  meeting window. It needs no audio capture, speech-to-text service or Teams bot registration.
- **Answers in the meeting chat.** Anyone can say "Hey Claude, what did we decide about the vendor
  shortlist?".
  - About 2 seconds after the asker stops talking, the chat shows `Got it` with the question as the
    captions heard it. That confirms Claude is working on it and shows any mishearing straight away.
  - The answer follows within seconds.
- **Grounds answers in your material.** A Claude agent can search the meeting's documents (PDF, Word,
  PowerPoint, Excel, text) and any MCP servers you connect. It cites its sources and points out when
  something said in the meeting conflicts with a document.
- **Speaks when asked.** Add "out loud" to a question and Azure AI Speech reads a short version of the
  answer into the meeting. "Hey Claude, say that out loud" repeats the last answer without another model
  call.
- **Takes private questions.** The operator can ask Claude something only they see. Private questions
  never reach the chat, the summary or the wrap-up.
- **Writes the meeting record.** It saves a speaker-labelled transcript and an executive summary
  (decisions, action items with named owners, open questions, risks), and posts a short wrap-up in the
  chat.
- **Has a desktop app.** You paste the join link and click **Join meeting**. The app shows what the bot
  hears and answers, and includes a live **Meeting view** of the bot's browser for the occasional step
  that needs a person.

## How it works

```
Teams web client (Chromium driven by Playwright, signed in as the bot account)
  └─ live captions ──► speaker-labelled transcript ──► wake phrase "Hey Claude"
                                                           │
                    Claude Agent SDK ◄─────────────────────┘
                      system prompt:  agent/instructions.md
                      tools:          Read / Grep / Glob, confined to the meeting's materials folder
                      MCP:            none by default; servers you add to agent/mcp.json
                      guard rails:    no permission prompts (anything else is refused), strict MCP
                                      config, pinned engine, no session copies on disk,
                                      per-question $ / turn / time limits that fall back, never stop
                           │
                           ├──► answer typed into the meeting chat
                           └──► optional: Azure AI Speech ──► spoken into the meeting
```

- **No bot infrastructure.** The bot drives the Teams web client the way a person would. It needs no
  Azure Bot registration, no Graph media SDK and no third-party meeting-bot vendor.
- **A locked-down agent.** Each question runs one fresh agent session. The agent can only read and
  search the materials folder and call the MCP servers you list (none by default). Reads anywhere else
  on the PC are refused. It has no shell, file-write or web tools, and it keeps no copy of the meeting.
- **Always an answer.** If the agent fails or hits a per-question limit (MCP down, timeout, spend or
  turn limit), the bot answers again from the transcript alone. No limit stops the bot for the rest of
  the meeting; the log shows the running spend instead.
- **Help with UI changes.** If a Teams UI step fails, the bot saves a screenshot and HTML for diagnosis
  and asks the operator to do that one click in the Meeting view.

### What leaves the meeting PC

| Destination | What is sent | When |
|---|---|---|
| Claude (Anthropic API, or Microsoft Foundry in your Azure subscription) | The transcript so far, the question, and the file excerpts and MCP results the agent reads | Each question, plus the summary at the end |
| Azure AI Speech | The text of the answer to be spoken | Only when an answer is spoken |
| MCP servers in `agent/mcp.json` (none by default) | The queries the agent sends them | Only when a question needs them |
| Microsoft Teams | Chat messages, plus audio while the bot speaks | During the meeting |

Transcripts, summaries, logs and the bot's saved sign-in stay on the meeting PC, under `meetings\`.
`.gitignore` keeps all of them out of git.

## Quick start

**You need:**
- Windows 10 or 11 with Python 3.11 (3.10 or later works).
- A Teams account for the bot. Without one, it can join as a guest if your tenant allows it.
- Claude access: an Anthropic API key, or a Claude deployment in Microsoft Foundry.
- Optional, for voice: an Azure AI Speech resource. With Foundry, the bot uses the Foundry resource and
  key for Speech too.

```powershell
git clone https://github.com/scottrhay/claude-teams-bot.git
cd claude-teams-bot
powershell -ExecutionPolicy Bypass -File .\setup.ps1              # private .venv, dependencies, Chromium
powershell -ExecutionPolicy Bypass -File .\install_shortcut.ps1   # "Claude-Teams-Bot" on Desktop + Start menu
.\.venv\Scripts\python.exe -m pytest -q                           # offline test suite, no model calls
```

Choose a model provider:

- **Anthropic API.** Run `setx ANTHROPIC_API_KEY "<your-key>"`, then open a new terminal.
- **Microsoft Foundry.** Copy `foundry.env.ps1.template` to `foundry.env.ps1` and fill in the resource
  name, key and deployment. `foundry.env.ps1` is gitignored.

Then, in the same folder:

```powershell
.\run.ps1 -Login     # sign in once as the bot's Teams account (saves teams_auth.json)
.\run.ps1 -Check     # preflight: browser, model, pinned agent engine, MCP, voice (a few small billed calls)
```

Open **Claude-Teams-Bot** from the Start menu, paste a Teams join link and click **Join meeting**.

## Documentation

| Guide | For | Covers |
|---|---|---|
| [User Guide](docs/USER_GUIDE.md) | The person running the bot in a meeting | Before, during and after a meeting, the desktop app, voice, private questions, troubleshooting |
| [Admin Guide](docs/ADMIN_GUIDE.md) | Whoever deploys it in a tenant | Teams prerequisites, bot account, model provider, Speech, knowledge folders, MCP servers, cost controls, security and data handling, every command-line option, maintenance |
| [CLAUDE.md](CLAUDE.md) | Developers (and Claude Code) | Internals: process model, bot pipeline, agent lock-down, prompts, tests |

## Repository layout

| Path | What it is |
|---|---|
| `teams_meeting_bot.py` | The bot: browser automation, captions, wake phrase, agent, voice, summary, CLI |
| `app.py`, `app_style.py` | Windows desktop app (tkinter) that runs the bot and shows its activity |
| `agent/instructions.md` | The agent's system prompt: role, answer rules, boundaries |
| `agent/mcp.json` | MCP servers the agent may call (Claude Code format); empty by default |
| `agent/mcp.example.json` | A sample entry (Microsoft Learn's public docs server) to copy into `mcp.json` |
| `agent/project/` | Sample meeting materials (fictional), used when no knowledge folder is chosen |
| `run.ps1` | Command-line launcher: preflight, sign-in, join |
| `setup.ps1`, `install_shortcut.ps1`, `requirements.txt` | Install |
| `foundry.env.ps1.template` | Placeholder settings for Microsoft Foundry |
| `tests/` | Offline tests, including end-to-end runs in real Chromium against a mock Teams meeting |

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest -q                                        # full suite, ~4-5 min
.\.venv\Scripts\python.exe -m pytest -q tests/test_unit.py tests/test_app.py   # fast loop, seconds
```

The suite makes no model calls. It covers:
- the wake phrase, including caption mishears and false positives;
- caption finalization;
- join-link rewriting;
- the agent lock-down and the Foundry model pinning;
- the agent engine itself: `tests/test_engine.py` runs the pinned engine against a local fake model and
  checks that reads outside the folder are refused, `@file` text in captions isn't expanded, no
  session copy is written, and the turn and spend limits end a run;
- a tripped limit still getting the asker an answer;
- Office and PDF conversion;
- keeping private questions out of the meeting;
- the desktop app's activity feed.

`tests/test_e2e.py` runs whole meetings in headless Chromium against `tests/mock_teams.html`, a mock
that reproduces real Teams quirks. Those runs cover the lobby, captions, the "Got it" note before each
answer, the agent-failure fallback and the end of the meeting. One GUI end-to-end test makes real model calls
and runs only when `RUN_GUI_TESTS=1` is set.

## Known limits

- **Teams UI changes can break a step.** Selectors live in the `SEL` table at the top of
  `teams_meeting_bot.py`. The [Admin Guide](docs/ADMIN_GUIDE.md#when-teams-changes-its-ui) explains how
  to diagnose and fix one. Run a short test meeting before important meetings.
- **Accuracy depends on Teams captions.** The bot is only as accurate as the captions.
- **One meeting per running instance**, on a Windows PC that stays on for the whole meeting.
- **Key-based authentication only.** This version authenticates to Foundry and Speech with keys.
  Keyless Microsoft Entra ID sign-in is not supported.

## Responsible use

- When the bot joins, it posts a notice in the meeting chat saying it reads live captions to keep a
  transcript. Check your organization's policies on meeting transcription and AI assistants before use,
  and tell attendees in advance.
- Meeting records are confidential. They stay on the meeting PC. Keep the private-question file and the
  log away from attendees, because both contain the operator's private questions.

---

Claude-Teams-Bot is an independent project. It is not affiliated with or endorsed by Anthropic or
Microsoft. Claude is a trademark of Anthropic. Microsoft Teams is a trademark of Microsoft.

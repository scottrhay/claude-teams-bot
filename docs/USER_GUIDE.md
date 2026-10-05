# Claude-Teams-Bot User Guide

This guide is for the **operator**, the person who runs the bot for a meeting. Attendees need only the
section [For attendees](#for-attendees). Installation and tenant setup are in the
[Admin Guide](ADMIN_GUIDE.md).

## Contents

1. [The day before](#the-day-before)
2. [Starting the bot](#starting-the-bot)
3. [During the meeting](#during-the-meeting)
4. [Ending the meeting](#ending-the-meeting)
5. [After the meeting](#after-the-meeting)
6. [Running from the command line](#running-from-the-command-line)
7. [Troubleshooting](#troubleshooting)

## The day before

1. **Make a knowledge folder for the meeting.** Create one folder per meeting and copy in the agenda,
   briefs, decks, budget sheets and prior minutes (PDF, Word, PowerPoint, Excel, Markdown, text).
   Claude searches these files and cites them by name. The folder can be anywhere on the PC. If you
   keep it inside the bot's folder, put it under `materials\`, which git ignores.
2. **Run Check setup.** Open the app, choose the knowledge folder and click **Check setup**. Every line
   should read PASS. The agent line should list the files it found and any MCP tools. The check makes a
   few small billed model calls.
3. **Refresh the bot's sign-in if it is more than about a week old.** Run `.\run.ps1 -Login` and sign in
   as the bot account (see the [Admin Guide](ADMIN_GUIDE.md#bot-account-and-sign-in)).
4. **Do a dry run before an important meeting.** Hold a five-minute test meeting. Ask one question about
   a file and, if you use MCP servers, one question that needs them.

## Starting the bot

Open **Claude-Teams-Bot** from the Desktop or the Start menu. The **Control** tab has these fields:

| Field | What to enter |
|---|---|
| **Meeting link** | The Teams join link (starts with `https://teams.microsoft.com/`). A link that isn't a Teams link is flagged under the field. |
| **Bot name** | How the bot appears when it joins as a guest. Pre-filled "Claude Meeting Assistant"; **Reset** restores it. Teams accepts letters, numbers, spaces and `- ' . _ @`. When the bot is signed in as its own account, Teams shows that account's name instead. |
| **Knowledge folder** | The meeting's folder (**Browse…**). If you leave the default (`agent\project`), Claude answers from the fictional sample files. |
| **Speak answers** | **When asked**: speaks only when a question says "out loud". **Always**: speaks every answer. **Off**: chat only. |

Click **Join meeting**. The setup form folds into a **This meeting** summary while the bot runs. The
app remembers the last link, name, folder, voice choice and technical-details setting.

The status pill at the top right shows what the bot is doing:

| Status | Meaning |
|---|---|
| Opening the meeting… | The bot's browser is loading the meeting. |
| Joining – admit the bot from the lobby | The bot clicked Join. Admit it in Teams if your meeting uses a lobby. |
| In the lobby – admit the bot in Teams | It is waiting for someone to admit it (up to 20 minutes). |
| In the meeting – turning on captions | It is in the call and turning on live captions. |
| In the meeting – listening for "Hey Claude" | Ready. A notice has been posted in the meeting chat. |
| **Needs you** | A step needs a person. See [Action needed](#action-needed). |
| Meeting ended – writing summary | The meeting ended. The bot is writing the summary. |
| Finished – transcript and summary saved | Done. Click **Open meeting files**. |
| Stopped with an error – see Activity | Something failed. See [Troubleshooting](#troubleshooting). |

When the bot is ready, it posts this notice in the meeting chat:

> Claude meeting assistant has joined and is reading Teams live captions to keep a transcript. Ask a
> question by saying "Hey Claude, ..." - answers post here. Add "out loud" to hear the answer spoken.

## During the meeting

### For attendees

- **Ask out loud.** Start with "Hey Claude", for example "Hey Claude, what's the Q4 capex number?".
  "Hi Claude" and "OK Claude" also work, and so does starting a sentence with "Claude, ...". Caption
  mishearings such as "Hey cloud" are recognized. A plain mention of "the cloud" is not.
- **Finish your thought.** Claude answers once you have been quiet for about 2 seconds, or sooner when
  your question ends with a question mark in the captions. A pause in the middle of a question is fine.
- **Read the answer in the chat.** Answers appear in the meeting chat, usually within 4 to 8 seconds.
  Longer ones get a "working on it" note first. Answers cite their source, for example
  `(source: q4_it_budget_brief.md)`.
- **Hear it.** Add "out loud", "aloud" or "tell us" to the question, for example "Hey Claude, tell us
  out loud who owns the network refresh". The bot speaks the first one or two sentences. The full
  answer still goes to the chat.
- **Forgot to say "out loud"?** Say "Hey Claude, say that out loud" (or "read that", "repeat that"). The
  bot reads its last answer straight away.
- **Know what Claude can answer from.** Claude knows what was said so far (from the captions), the
  meeting's files and any connected MCP tools. It has no web access. When none of those contain the
  answer, it says so rather than guessing.

### For the operator

- **Activity** shows each caption, each question and each of Claude's answers as they happen. Tick
  **Show technical details** to add the bot's step-by-step log.
- **Ask Claude** (available once the bot is in the meeting):
  - **Ask privately**: type a question that only you see. The answer appears in Activity. Private
    questions and answers never reach the meeting chat, the summary or the wrap-up. They are saved to
    `operator_private_*.md`. Your later private questions can see the earlier private ones.
  - **Say out loud**: Claude says your text, word for word, in the meeting.
- **Meeting view** tab: a live picture of the bot's browser, updated about once a second. Click in the
  picture to click in the meeting. Use the box underneath to type, with buttons for Enter, Esc, Tab and
  Backspace.

### Action needed

When Teams shows something the bot can't handle on its own, the status turns amber (**Needs you**) and
an **Action needed** banner says what to click and where. The app switches to the Meeting view when the
step is there. Do that one click (or type), and the bot carries on.

If you would rather watch the bot's browser directly, tick **Show the bot's browser in its own window
(troubleshooting)** on the Control tab before joining.

## Ending the meeting

The bot wraps up in any of these three cases:

- **The meeting ends** for everyone. The bot notices within about 15 seconds.
- **You click Leave meeting** in the This meeting summary and confirm.
- **You close the app** while the bot is in the meeting. It asks first, then leaves.

In each case the bot writes the summary, posts a short **wrap-up** in the meeting chat (when it is
still in the call) and leaves. If it hasn't exited 90 seconds after you click Leave, the app stops it.

## After the meeting

Click **Open meeting files**. Each meeting has its own folder, `meetings\<yyyy-MM-dd_HHmm>\`:

| File | Contents | Share with attendees? |
|---|---|---|
| `meeting_transcript_*.md` | Speaker-labelled transcript, including Claude's public answers | Yes, if your policy allows |
| `meeting_summary_*.md` | Summary, decisions, action items (owner, due date), open questions, risks, follow-ups | Yes |
| `operator_private_*.md` | Your private questions and Claude's answers | **No** |
| `bot_*.log` | The bot's step-by-step log, which also lists private questions | **No** |
| `bot_debug\` | Screenshots and page HTML from any UI step that failed | No (for troubleshooting) |

These files are confidential meeting records. They stay on this PC and are never committed to git.

## Running from the command line

`run.ps1` runs the same bot without the app. The browser opens in its own window, and you type commands
in the console.

```powershell
.\run.ps1 -Url "<Teams join link>" -Project "C:\Meetings\2026-10-02_exec"
.\run.ps1 -Url "<Teams join link>" -Guest       # join as a guest even if a sign-in is saved
```

While the bot is in the meeting, you can type these commands in the console:

| Command | Effect |
|---|---|
| `<question>` | Ask privately: the answer shows in the console only |
| `/chat <question>` | Ask, and post the answer in the meeting chat |
| `/say <text>` | Post the text in the meeting chat as-is |
| `/speak <text>` | Say the text out loud in the meeting |
| `/voice off\|asked\|always` | Change when answers are spoken |
| `/summary` | Write the summary file now |
| `/post-summary` | Post the summary in the meeting chat |
| `/leave` | Leave: write the transcript and summary, then exit |

Ctrl+C also writes the transcript and summary, but posts no wrap-up.

## Troubleshooting

| Symptom | What to do |
|---|---|
| Status stays "In the lobby" | Admit "Claude Meeting Assistant" (or the bot account) in Teams. The bot gives up after 20 minutes. A signed-in bot account from your own organization usually skips the lobby. |
| **Needs you** / Action needed | Follow the banner: do the one click in the Meeting view. |
| Nobody gets an answer | 1. Check that Activity shows captions arriving. If it doesn't, live captions are off or not allowed for the bot (see the Admin Guide). 2. Check that the question started with "Hey Claude". 3. Tick **Show technical details** and look for the question line. |
| The answer ignores the meeting's files | Make sure the Knowledge folder points at the meeting's folder, not the default sample folder. When the bot starts, Activity shows "Preparing the knowledge folder…" and then "Knowledge folder ready". |
| A file is never cited | Scanned PDFs (pages that are only pictures) can't be searched, and password-protected files can't be read. Activity names such files when the bot first sees them. Use a text-based copy. |
| No voice | Check that Speak answers is not **Off** and that the question said "out loud". Run **Check setup**: the Azure AI Speech line must pass. The bot waits until nobody has spoken for 1.5 seconds before it speaks. |
| The room hears an echo | The bot never plays the meeting's audio on its PC. Make sure nobody started it with `--hear-meeting`. |
| The bot joins as a guest instead of as its account, or stops at a Microsoft sign-in page | The saved sign-in is missing (**Check setup** shows "Saved sign-in … absent") or has expired. Run `.\run.ps1 -Login` to sign the bot account in again. |
| **Check setup** shows FAIL lines | Fix each FAIL line. Its text says what is missing: the API key, the Foundry settings, the Chromium install (`setup.ps1`) or the Speech resource. |
| Stopped with an error | Click **Open meeting files**, then send `bot_*.log` and the `bot_debug\` folder to your administrator. Teams UI changes are the usual cause (see [When Teams changes its UI](ADMIN_GUIDE.md#when-teams-changes-its-ui)). |

#!/usr/bin/env python3
"""
teams_meeting_bot.py - Claude joins a Microsoft Teams meeting as a participant.

How it works (same pattern as Recall.ai's open write-up):
  1. Playwright drives Chromium into the Teams *web* client via the join link.
  2. The bot joins (as a signed-in service account, or as a named guest), turns on
     Teams live captions, and reads every caption + speaker name from the page.
  3. When anyone says "Hey Claude, ...", the question plus the full speaker-labeled
     transcript goes to Claude, and the answer is typed into the meeting chat.
  4. When the meeting ends (or you type /leave) it writes the transcript and an
     exec summary (decisions, action items with real owner names, risks).

No Azure bot registration, no Graph media SDK, no third-party bot vendor.
Built for a few high-value meetings, not for scale.

SETUP (Windows or Mac, Python 3.10+):
    pip install -r requirements.txt
    python -m playwright install chromium
    setx ANTHROPIC_API_KEY "sk-ant-..."         (new terminal afterwards)

RECOMMENDED: run as a service account in your tenant (internal participant: no lobby, chat allowed)
    python teams_meeting_bot.py --login                       (sign in once; saves teams_auth.json)
    python teams_meeting_bot.py --url "<Teams join link>" --context exec_agenda.md

GUEST MODE (no account; host admits from the lobby; tenant policy must allow
anonymous join + meeting chat for guests):
    python teams_meeting_bot.py --url "<join link>" --guest --name "Claude Meeting Assistant"

CONSOLE COMMANDS while the bot is in the meeting:
    <question>        ask privately - answer shows on this console only; never used for chat
                      answers, the summary or the wrap-up (saved to operator_private_*.md)
    /chat <question>  ask and post the answer to the meeting chat
    /say <text>       post text to the meeting chat as-is
    /summary          write the summary file now
    /post-summary     post the summary into the meeting chat
    /leave            leave the meeting, write transcript + summary, exit
    /speak <text>     say text out loud in the meeting
    /voice off|asked|always   change when answers are spoken (default: asked)

VOICE: answers are spoken with Azure AI Speech when someone asks for it out loud
("Hey Claude, tell us out loud ...") or always with --voice always. Uses the Foundry
resource and key from foundry.env.ps1 unless SPEECH_RESOURCE / SPEECH_KEY are set.

If a step fails (Teams UI changed), the bot saves a screenshot + HTML in ./bot_debug/
and, in the default visible-browser mode, asks you to do that one click by hand.
Selectors live in the SEL table below - that is the only place to update.
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import re
import struct
import sys
import threading
import time
import traceback
import base64
import urllib.parse
import urllib.request
import wave
from xml.sax.saxutils import escape as xml_escape

import anthropic
from playwright.async_api import async_playwright

# ------------------------------------------------------------------------------------
# Selectors. Each entry is a list of candidates tried in order; first visible wins.
#   ("css", selector) | ("role", role, name_regex) | ("placeholder", regex) | ("text", regex)
# Caption selectors come from Recall.ai's published Teams bot.
# ------------------------------------------------------------------------------------
SEL = {
    "continue_browser": [("css", 'button[data-tid="joinOnWeb"]'),
                         ("role", "button", r"join meeting from this browser"),
                         ("role", "button", r"continue on this browser"),
                         ("role", "link", r"continue on this browser"),
                         ("role", "button", r"join on the web")],
    "name_input": [("css", 'input[data-tid="prejoin-display-name-input"]'),
                   ("placeholder", r"name")],
    "mic_switch": [("css", '[data-tid="toggle-mute"]'),
                   ("role", "switch", r"micro(phone)?|mic\b")],
    "cam_switch": [("css", '[data-tid="toggle-video"]'),
                   ("role", "switch", r"camera|video")],
    "join_now": [("css", 'button[data-tid="prejoin-join-button"]'),
                 ("role", "button", r"^\s*join now\s*$")],
    "in_meeting": [("css", 'button[data-tid="hangup-main-btn"]'),
                   ("css", "#hangup-button"),
                   ("role", "button", r"^\s*leave")],
    "more": [("css", 'button[data-tid="more-button"]'),
             ("css", "#callingButtons-showMoreBtn"),
             ("role", "button", r"^\s*more( actions)?\s*$")],
    "lang_speech": [("role", "menuitem", r"language and speech"),
                    ("text", r"^\s*language and speech\s*$")],
    "captions_on": [("role", "menuitem", r"(show|turn on).*captions"),
                    ("role", "menuitemcheckbox", r"captions"),
                    ("role", "menuitem", r"^\s*(live )?captions\s*$"),
                    ("text", r"(show|turn on).*captions")],
    "chat_button": [("css", 'button[data-tid="chat-button"]'),
                    ("css", "#chat-button"),
                    ("role", "button", r"^\s*chat\s*$")],
    "chat_box": [("css", '[data-tid="ckeditor"]'),
                 ("role", "textbox", r"type a message|type a new message"),
                 ("css", 'div[contenteditable="true"][role="textbox"]')],
}
CAPTIONS_CONTAINER = ('div[data-tid="closed-caption-renderer-wrapper"], '
                      '[data-tid*="closed-caption"]')
CAPTION_TEXT = '[data-tid="closed-caption-text"]'
CAPTION_AUTHOR = '[data-tid="author"]'
CAPTION_ITEM = ".fui-ChatMessageCompact"
# Teams guest names: letters, numbers, spaces and - ' . _ @ only.
NAME_BAD_CHARS = re.compile(r"[^A-Za-z0-9 \-'._@]")
NAME_SUFFIX = re.compile(r"\s*\((guest|unverified|external)\)\s*$", re.I)
VOICE_ASK = re.compile(r"\b(out loud|aloud|say it|tell (us|me|everyone)|speak|verbally|read (it|that) out)\b", re.I)
# "say that out loud", "read your answer", "repeat that", "can you say it again?"
REPEAT_ASK = re.compile(r"^\W*(?:(?:can|could|would|will) you\s+|please\s+|ok(?:ay)?\s+|now\s+)*"
                        r"(?:say|read|repeat|speak)\b(?:\s+(?:that|it|this|the|your|last|previous|"
                        r"answer|response|again|back|out|loud|aloud|to us|for us|please))*\W*$", re.I)
SOURCE_PAREN = re.compile(r"\(\s*sources?:[^)]*\)", re.I)

# Runs in every page before Teams loads: the bot's microphone becomes an audio channel
# the bot controls (silent until it speaks), and desktop-app hand-off iframes are removed
# so Chrome never shows "Open Microsoft Teams?".
INIT_JS = """
(() => {
  const md = navigator.mediaDevices;
  if (md && md.getUserMedia && !window.__claudeAudioPatched) {
    window.__claudeAudioPatched = true;
    const orig = md.getUserMedia.bind(md);
    let ctx = null, dest = null;
    const audio = () => {
      if (!ctx) { ctx = new AudioContext(); dest = ctx.createMediaStreamDestination(); }
      return { ctx, dest };
    };
    md.getUserMedia = async (c) => {
      if (c && c.audio) {
        const { dest } = audio();
        const out = new MediaStream(dest.stream.getAudioTracks().map(t => t.clone()));
        if (c.video) {
          const v = await orig({ video: c.video });
          v.getVideoTracks().forEach(t => out.addTrack(t));
        }
        return out;
      }
      return orig(c);
    };
    window.__claudeSpeak = async (b64) => {
      const { ctx, dest } = audio();
      if (ctx.state !== 'running') await ctx.resume();
      const bin = Uint8Array.from(atob(b64), ch => ch.charCodeAt(0));
      const buf = await ctx.decodeAudioData(bin.buffer);
      const src = ctx.createBufferSource();
      src.buffer = buf; src.connect(dest);
      await new Promise(r => { src.onended = r; src.start(); });
      return buf.duration;
    };
  }
  const strip = () => document.querySelectorAll('iframe').forEach(f => {
    if ((f.getAttribute('src') || '').toLowerCase().startsWith('msteams:')) f.remove();
  });
  new MutationObserver(strip).observe(document, { childList: true, subtree: true });
})();
"""

LOBBY_RE = re.compile(r"let you in (shortly|soon)|should let you in|waiting for (someone|the organizer)|"
                      r"in the lobby|someone will let you in", re.I)

# "Hey Claude ..." anywhere, or an utterance that starts with "Claude,".
# Captions sometimes render Claude as cloud/clod; the hey/ok prefix keeps
# "the Azure cloud" from firing.
WAKE = re.compile(
    r"\b(?:hey|hi|ok|okay)[,\s]+(?:claude|claud|clod|cloud|clawed|klaud)\b[,.!?\s]*", re.I)
WAKE_START = re.compile(r"^\s*claude\s*[,:]\s*", re.I)

SYSTEM = """You are Claude, an AI assistant participating in a Microsoft Teams meeting
for the leadership team. You receive the live transcript (from Teams captions, with speaker
names; expect some caption errors) and a question someone just asked you.
Rules:
- Your answer is posted in the meeting chat and read by executives mid-meeting:
  1-4 short sentences unless asked for more. Answer first. No preamble.
- When the question is about what was said, ground it in the transcript and name the
  speaker. If the transcript doesn't contain it, say so. Never invent who said what.
- For general-knowledge questions, answer directly; flag uncertainty in a few words.
- You have no web access. For current events, prices or anything recent, say your
  information may be out of date.
- Plain text only (no markdown headers or tables) - it is going into a chat box.
- Lines labeled Claude are your own earlier answers.
- Your answers are also read aloud by the meeting assistant's voice when someone asks.
  Never say you can't speak or have no voice."""

SUMMARY_PROMPT = """Write the executive meeting record from this transcript. Markdown:
## Summary (3-5 sentences)
## Decisions made
## Action items (table: Action | Owner | Due | Time)
## Open questions
## Risks / concerns raised
## Follow-ups
Rules: owners are the speaker names in the transcript - attribute only when clear,
otherwise "Unassigned". Never invent dates or commitments. Cite times like [14:32].
Skip empty sections."""

CHAT_SUMMARY_PROMPT = """Write a short meeting wrap-up for the meeting chat: plain text,
no markdown, under 120 words: key decisions, then action items as "Owner - action - due".
Only what the transcript supports."""


LOG_FILE = None


def log(msg):
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def chat_text(text):
    """Teams chat isn't a markdown renderer: strip emphasis/headers the model may add."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


OFFICE_EXT = (".docx", ".pptx", ".xlsx")
CONVERT_EXT = OFFICE_EXT + (".pdf",)
TEXT_DIR = "_converted_text"
SCANNED_NOTE = "(No text found: this PDF looks like a scanned image. Open the original to read it.)"


def _to_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import logging
        from pypdf import PdfReader
        logging.getLogger("pypdf").setLevel(logging.ERROR)     # no chatter about odd-but-readable files
        reader = PdfReader(path)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")                         # many PDFs only restrict printing
            except Exception:
                ok = False
            if not ok:
                raise ValueError("it is password-protected or encrypted")
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
        return "\n\n".join(f"## Page {i}\n{t}" for i, t in enumerate(pages, 1) if t)
    if ext == ".docx":
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)
    if ext == ".pptx":
        import pptx
        out = []
        for i, slide in enumerate(pptx.Presentation(path).slides, 1):
            out.append(f"## Slide {i}")
            for shp in slide.shapes:
                if shp.has_text_frame and shp.text_frame.text.strip():
                    out.append(shp.text_frame.text)
                if getattr(shp, "has_table", False) and shp.has_table:
                    for row in shp.table.rows:
                        out.append(" | ".join(c.text for c in row.cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                out.append("Notes: " + slide.notes_slide.notes_text_frame.text)
        return "\n".join(out)
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"## Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                if any(v is not None for v in row):
                    out.append(" | ".join("" if v is None else str(v) for v in row))
        return "\n".join(out)
    raise ValueError(ext)


def _same_time(a, b):
    """Same modified time, within the 2 s that FAT/exFAT drives round to."""
    return abs(os.stat(a).st_mtime_ns - os.stat(b).st_mtime_ns) < 2_000_000_000


def prepare_materials(project_dir):
    """Make the materials searchable. Grep can't see inside Word, PowerPoint, Excel or PDF
    files (PDF text is compressed), and Read can't open Office files, so their text is copied
    to TEXT_DIR next to the originals. Runs at every start but converts only new or changed
    files: each copy carries its original's modified time, so any difference (an edit, or an
    older version copied over it) converts again. Copies of deleted or renamed files are
    removed, so Claude can't cite a document that is no longer in the folder."""
    out_dir = os.path.join(project_dir, TEXT_DIR)
    todo, keep = [], set()
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if os.path.join(root, d) != out_dir]
        for fn in files:
            if fn.lower().endswith(CONVERT_EXT) and not fn.startswith("~$"):
                src = os.path.join(root, fn)
                dst = os.path.join(out_dir, os.path.relpath(src, project_dir) + ".md")
                keep.add(os.path.normcase(dst))
                if not (os.path.exists(dst) and _same_time(src, dst)):
                    todo.append((src, dst))
    removed = 0
    for root, _, files in os.walk(out_dir, topdown=False):
        for fn in files:
            p = os.path.join(root, fn)
            if fn.lower().endswith(tuple(e + ".md" for e in CONVERT_EXT)) and os.path.normcase(p) not in keep:
                os.remove(p)
                removed += 1
        if root != out_dir and not os.listdir(root):
            os.rmdir(root)
    if removed:
        log(f"  removed the text copies of {removed} file(s) no longer in the knowledge folder")
    if not todo:
        return 0
    log(f"Preparing the knowledge folder ({len(todo)} new or changed file(s))...")
    done = 0
    for src, dst in todo:
        fn = os.path.basename(src)
        log(f"  converting {fn}")
        if os.path.exists(dst):
            os.remove(dst)                                   # never search an outdated copy
        try:
            text = _to_text(src)
        except (ImportError, OSError) as e:                  # fixable or temporary: retry next start
            why = (f"{e.name or 'a library it needs'} is not installed (run setup.ps1)"
                   if isinstance(e, ImportError) else e)
            log(f"  could not convert {fn}: {why}")
            continue
        except Exception as e:                               # unreadable as it is: noted until it changes
            log(f"  could not convert {fn}: {e}")
            text = f"(Could not extract text: {e}.)"
        else:
            if text.strip():
                done += 1
            elif fn.lower().endswith(".pdf"):
                log(f"  could not find text in {fn} (a scanned image?) - Claude can open it but not search it")
                text = SCANNED_NOTE
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(f"# Text extracted from {fn}\n\n{text}\n")
        st = os.stat(src)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))  # the copy carries the original's time
    log(f"Knowledge folder ready ({done} of {len(todo)} file(s) converted to searchable text).")
    return done


def make_client():
    """Direct-API last-resort client: Foundry when configured, else Anthropic."""
    if foundry_enabled():
        return anthropic.AsyncAnthropicFoundry(
            resource=os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE"),
            base_url=os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL"),
            api_key=os.environ.get("ANTHROPIC_FOUNDRY_API_KEY"))
    return anthropic.AsyncAnthropic()


def foundry_enabled():
    return os.environ.get("CLAUDE_CODE_USE_FOUNDRY", "").lower() in ("1", "true")


def now_text():
    return dt.datetime.now().astimezone().strftime("%A, %B %d, %Y, %I:%M %p %Z")


def clean_name(name):
    """Teams rejects guest names with characters such as parentheses."""
    cleaned = re.sub(r"\s+", " ", NAME_BAD_CHARS.sub(" ", name)).strip()
    return cleaned or "Claude Meeting Assistant"


def base_name(name):
    return NAME_SUFFIX.sub("", name or "").strip().lower()


def direct_web_join(link, guest=True):
    """Meeting link -> Teams web-app join URL, without ever loading the launcher page
    (whose app hand-off triggers Chrome's "Open Microsoft Teams?" prompt)."""
    u = urllib.parse.urlparse(link)
    if not u.netloc.endswith(("teams.microsoft.com", "teams.cloud.microsoft", "teams.live.com")):
        return None
    if "launcher" in u.path:
        return direct_join_url(link)
    if not (u.path.startswith("/meet/") or u.path.startswith("/l/meetup-join/")):
        return None
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != "anon"]
    if guest:
        q.append(("anon", "true"))
    qs = urllib.parse.urlencode(q, safe="{}:,\"")
    return f"{u.scheme}://{u.netloc}/_#{u.path}" + (f"?{qs}" if qs else "")


def norm_q(text):
    """Lower-case words only, for comparing caption versions of the same question."""
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower()))


def spoken_version(text, max_words=25, max_sentences=2):
    """Short, speakable form of a chat answer: no source tags, 1-2 sentences (~6 s of speech).
    The first sentence is always kept; the full answer is in the chat."""
    text = SOURCE_PAREN.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", chat_text(text)).strip()
    out, n = [], 0
    for sent in re.split(r"(?<=[.!?])\s+", text):
        w = len(sent.split())
        if out and (n + w > max_words or len(out) >= max_sentences):
            break
        out.append(sent)
        n += w
    return " ".join(out).strip()


def azure_tts(text, voice, resource, key, endpoint=None, timeout=20):
    """Azure AI Speech text-to-speech -> 24 kHz mono WAV bytes."""
    url = endpoint or f"https://{resource}.cognitiveservices.azure.com/tts/cognitiveservices/v1"
    ssml = (f"<speak version='1.0' xml:lang='en-US'><voice name='{voice}'>"
            f"{xml_escape(text)}</voice></speak>")
    req = urllib.request.Request(url, data=ssml.encode("utf-8"), method="POST", headers={
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
        "User-Agent": "claude-teams-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def make_silence_wav(path, seconds=2):
    """Chromium's fake mic plays this file - so even an unmuted bot sends silence."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * 16000 * seconds)


def teams_web_url(url):
    """Force the browser join flow (Recall's parameter set) instead of 'open the app'."""
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    q.update(msLaunch="false", type="meetup-join", directDl="true",
             enableMobilePage="true", suppressPrompt="true")
    return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))


def direct_join_url(launcher_url):
    """Teams launcher URL -> direct web-app pre-join URL, or None if not a launcher URL."""
    u = urllib.parse.urlparse(launcher_url)
    if "launcher" not in u.path:
        return None
    inner = dict(urllib.parse.parse_qsl(u.query)).get("url")
    if not inner or not inner.startswith("/"):
        return None
    if "anon=true" not in inner:
        inner += ("&" if "?" in inner else "?") + "anon=true"
    return f"{u.scheme}://{u.netloc}{inner}"


def _loc(page, spec):
    kind = spec[0]
    if kind == "css":
        return page.locator(spec[1])
    if kind == "role":
        return page.get_by_role(spec[1], name=re.compile(spec[2], re.I))
    if kind == "placeholder":
        return page.get_by_placeholder(re.compile(spec[1], re.I))
    if kind == "text":
        return page.get_by_text(re.compile(spec[1], re.I))
    raise ValueError(spec)


async def first_visible(page, specs, timeout_s):
    deadline = time.monotonic() + timeout_s
    while True:
        for s in specs:
            try:
                l = _loc(page, s).first
                if await l.is_visible():
                    return l
            except Exception:
                pass
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.4)


# JS injected after join: scans caption nodes on an interval (survives Teams re-rendering
# the container), assigns each caption element a stable id, emits only on text change.
SCRAPER_JS = """
([textSel, authorSel, itemSel]) => {
  if (window.__claudeCaptions) return;
  window.__claudeCaptions = true;
  const ids = new WeakMap(); let n = 0;
  setInterval(() => {
    document.querySelectorAll(textSel).forEach(t => {
      let s = ids.get(t);
      if (!s) { s = { id: ++n, last: '' }; ids.set(t, s); }
      const text = (t.innerText || t.textContent || '').trim();
      if (!text || text === s.last) return;
      s.last = text;
      const item = t.closest(itemSel) || t.parentElement && t.parentElement.parentElement;
      const a = item && item.querySelector(authorSel);
      window.claudeCaption({ id: s.id, name: a ? a.textContent.trim() : 'Unknown', text });
    });
  }, 300);
}
"""


AGENT_PROMPT = """<meeting_transcript>
{transcript}
</meeting_transcript>

Current date and time: {now}

{asker} just asked you in the meeting: {question}

Answer for the Teams meeting chat. Use your project files and tools when the question
needs them. Plain text, 1-4 short sentences unless asked for more. If you used a file or
tool, cite it briefly at the end, e.g. (source: budget_brief.md)."""


class MeetingAgent:
    """A full Claude agent (Claude Agent SDK = the Claude Code harness) per question:
      - instructions.md  -> system prompt
      - project/         -> working dir; read-only Read/Grep/Glob tools
      - mcp.json         -> MCP servers ({"mcpServers": {...}}, Claude Code format)
    Permissions are dontAsk + an explicit allow-list: read-only file tools and the MCP
    servers named in mcp.json. No shell, no writes, no web unless an MCP server adds it."""

    READ_TOOLS = ["Read", "Grep", "Glob"]

    def __init__(self, agent_dir, model, max_turns, budget, timeout_s, project_dir=None):
        self.dir = os.path.abspath(agent_dir)
        self.project = os.path.abspath(project_dir) if project_dir else os.path.join(self.dir, "project")
        self.mcp_path = os.path.join(self.dir, "mcp.json")
        with open(os.path.join(self.dir, "instructions.md"), encoding="utf-8") as f:
            self.instructions = f.read()
        if not os.path.isdir(self.project):
            raise FileNotFoundError(f"Missing project folder: {self.project}")
        self.servers = []
        if os.path.exists(self.mcp_path):
            with open(self.mcp_path, encoding="utf-8") as f:
                self.servers = list(json.load(f).get("mcpServers", {}).keys())
        self.model, self.max_turns, self.budget, self.timeout = model, max_turns, budget, timeout_s
        prepare_materials(self.project)

    def describe(self):
        n = sum(len(fs) for r, _, fs in os.walk(self.project) if TEXT_DIR not in r)
        return (f"instructions.md, {n} file(s) in {self.project}, "
                f"MCP: {', '.join(self.servers) if self.servers else 'none'}, "
                f"model: {self.model} via {'Microsoft Foundry' if foundry_enabled() else 'Anthropic API'}")

    def options(self, use_tools=True, max_turns=None):
        from claude_agent_sdk import ClaudeAgentOptions
        tools = self.READ_TOOLS if use_tools else []
        servers = self.servers if use_tools else []
        return ClaudeAgentOptions(
            system_prompt=self.instructions,
            cwd=self.project,
            tools=tools,                                            # built-ins: read-only
            allowed_tools=tools + [f"mcp__{n}" for n in servers],
            mcp_servers=self.mcp_path if servers else {},
            strict_mcp_config=True,                                 # ONLY mcp.json servers
            permission_mode="dontAsk",                              # anything else denied
            setting_sources=[],                                     # ignore user/global settings
            env=self._env(),
            model=self.model,
            max_turns=max_turns or self.max_turns,
            max_budget_usd=self.budget)

    def _env(self):
        env = {"CLAUDE_CODE_ENABLE_CFC": "0",                       # no built-in browser server
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}      # no telemetry/error reporting
        if foundry_enabled():
            # Pin every internal model role to the one Foundry deployment, so the engine
            # never asks for a model that isn't deployed.
            for role in ("OPUS", "SONNET", "HAIKU"):
                env[f"ANTHROPIC_DEFAULT_{role}_MODEL"] = self.model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = self.model
        return env

    async def _run(self, prompt, use_tools=True, max_turns=None):
        from claude_agent_sdk import query, AssistantMessage, ResultMessage, ToolUseBlock
        tools, result = [], None
        async for m in query(prompt=prompt, options=self.options(use_tools, max_turns)):
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, ToolUseBlock):
                        tools.append(b.name)
                        log(f"  agent tool: {b.name} {json.dumps(b.input)[:120]}")
            elif isinstance(m, ResultMessage):
                result = m
        if result is None or result.is_error or not (result.result or "").strip():
            raise RuntimeError(f"agent error: {getattr(result, 'subtype', 'no result')} "
                               f"{getattr(result, 'errors', '')}")
        return result.result.strip(), tools, result.total_cost_usd

    async def ask(self, transcript, asker, question):
        prompt = AGENT_PROMPT.format(transcript=transcript, asker=asker, question=question,
                                     now=now_text())
        return await asyncio.wait_for(self._run(prompt), timeout=self.timeout)

    async def complete(self, transcript, task, timeout=None):
        """Single-shot, no tools: summaries and the fallback when tools/MCP fail."""
        prompt = f"<meeting_transcript>\n{transcript}\n</meeting_transcript>\n\n{task}"
        text, _, _ = await asyncio.wait_for(
            self._run(prompt, use_tools=False, max_turns=2), timeout=timeout or self.timeout)
        return text


class MeetingBot:
    def __init__(self, args):
        self.a = args
        self.client = make_client()
        self.transcript = []           # (line, private): the bot's only memory - see memory()
        self.caps = {}                 # caption id -> state
        self.pending = None            # question being collected: {speaker, text, t, started}
        self.recent_q = []             # (speaker, normalised question, time) for de-duplication
        self.last_answer = None        # last answer posted to the meeting, for "say that out loud"
        self.chat_lock = asyncio.Lock()
        self.speak_lock = asyncio.Lock()
        self.ended = asyncio.Event()
        self.a.name = clean_name(self.a.name)
        self.my_name = base_name(self.a.name)
        self.voice_mode = self.a.voice
        self.last_other_caption = 0.0
        self.listening = False
        self.frame_path = os.path.join(args.outdir, "live_view.jpg")
        self.context = ""
        if args.context:
            with open(args.context, encoding="utf-8") as f:
                self.context = f.read()
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        os.makedirs(args.outdir, exist_ok=True)
        self.path = os.path.join(args.outdir, f"meeting_transcript_{stamp}.md")
        self.private_path = os.path.join(args.outdir, f"operator_private_{stamp}.md")
        self.debug_dir = os.path.join(args.outdir, "bot_debug")
        global LOG_FILE
        LOG_FILE = os.path.join(args.outdir, f"bot_{stamp}.log")     # first: the agent's file
        self.agent = None                                            # preparation is logged too
        if not args.no_agent:
            self.agent = MeetingAgent(args.agent_dir, args.model, args.agent_max_turns,
                                      args.agent_budget, args.agent_timeout, args.project)

    # ------------------------------------------------------------------ transcript
    def add_line(self, who, text, private=False):
        line = f"[{dt.datetime.now():%H:%M:%S}] {who}: {text}"
        self.transcript.append((line, private))
        with open(self.private_path if private else self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def memory(self, private=False):
        """The transcript the model sees. Private Q&A (the operator's private questions) only
        goes back into private answers, never into anything attendees see."""
        return "\n".join(line for line, p in self.transcript if private or not p)

    async def on_caption(self, source, data):
        cid, name, text = data["id"], data["name"] or "Unknown", data["text"]
        if base_name(name) == self.my_name or "meeting assistant" in base_name(name):
            return                                   # the bot hearing itself
        self.last_other_caption = time.monotonic()
        st = self.caps.get(cid)
        if st is None:
            st = self.caps[cid] = {"name": name, "text": text, "t": time.monotonic(),
                                   "emitted_words": 0, "dirty": True}
        else:
            st.update(name=name, text=text, t=time.monotonic(), dirty=True)

    async def finalizer(self):
        """A caption is final once it stops changing for --stable seconds. Teams keeps
        appending to the same element if the speaker continues, so only new words emit."""
        while not self.ended.is_set():
            now = time.monotonic()
            for st in list(self.caps.values()):
                if st["dirty"] and now - st["t"] >= self.a.stable:
                    st["dirty"] = False
                    words = st["text"].split()
                    new = " ".join(words[st["emitted_words"]:]).strip()
                    st["emitted_words"] = len(words)
                    if new:
                        log(f"{st['name']}: {new}")
                        self.add_line(st["name"], new)
                        await self.check_wake(st["name"], new)
            p = self.due_question()
            if p:
                self.dispatch(p)
            await asyncio.sleep(0.25)

    async def check_wake(self, speaker, text):
        """Start or extend the question being collected. It is answered by due_question()
        once the asker stops talking, so a pause mid-question doesn't split it."""
        now = time.monotonic()
        m = WAKE.search(text) or WAKE_START.search(text)
        p = self.pending
        if p and p["speaker"] == speaker:
            if m:
                q2 = text[m.end():].strip()
                if norm_q(q2).startswith(norm_q(p["text"])):     # Teams re-rendered the line
                    p.update(text=q2, t=now)
                    return
                self.dispatch(p)                                  # a new question follows
            else:
                if norm_q(text) and norm_q(text) not in norm_q(p["text"]):
                    p["text"] = (p["text"] + " " + text).strip()
                p["t"] = now
                return
        if not m:
            return
        self.pending = {"speaker": speaker, "text": text[m.end():].strip(), "t": now, "started": now}
        log("  >> wake phrase heard, waiting for the question to finish...")

    def speaker_busy(self, speaker):
        return any(st["dirty"] and st["name"] == speaker for st in self.caps.values())

    def due_question(self, now=None):
        """The collected question once its asker has been quiet long enough, else None."""
        p = self.pending
        if not p:
            return None
        now = time.monotonic() if now is None else now
        words = len(p["text"].split())
        if words == 0:                                   # bare "Hey Claude": wait for the rest
            if now - p["started"] > 15:
                self.pending = None
            return None
        if self.speaker_busy(p["speaker"]):
            return None
        wait = self.a.question_wait_short if p["text"].rstrip().endswith("?") and words >= 3 \
            else self.a.question_wait
        return p if now - p["t"] >= wait else None

    def dispatch(self, p):
        self.pending = None
        q, now = p["text"].strip(), time.monotonic()
        key = norm_q(q)
        self.recent_q = [r for r in self.recent_q if now - r[2] < 30]
        for spk, k, _ in self.recent_q:
            if spk == p["speaker"] and (k.startswith(key) or key.startswith(k)):
                log(f"  (duplicate of a question just asked - skipped: {q})")
                return
        self.recent_q.append((p["speaker"], key, now))
        self.spawn(self.answer(q, p["speaker"], to_chat=True))

    def spawn(self, coro):
        asyncio.get_running_loop().create_task(coro)

    # ------------------------------------------------------------------ Claude
    def _system(self):
        return SYSTEM + (f"\n\n<background>\n{self.context}\n</background>" if self.context else "")

    def _messages(self, tail, private=False):
        convo = self.memory(private)
        return [{"role": "user", "content": [
            {"type": "text", "text": f"<transcript>\n{convo}\n</transcript>",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": tail}]}]

    async def claude(self, tail, max_tokens=600, private=False):
        if self.agent:
            try:
                return await self.agent.complete(self.memory(private), tail,
                                                 timeout=max(self.a.agent_timeout, 180))
            except Exception as e:
                log(f"  agent completion failed ({str(e)[:120]}) - trying direct API")
        resp = await self.client.messages.create(
            model=self.a.model, max_tokens=max_tokens, system=self._system(),
            messages=self._messages(tail, private))
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def is_repeat_request(self, question):
        return bool(self.last_answer) and bool(REPEAT_ASK.match(question or ""))

    async def answer(self, question, asker, to_chat):
        log(f"Q ({asker}): {question}")
        private = not to_chat          # asked privately; --console-only answers stay public
        to_chat = to_chat and not self.a.console_only
        if to_chat and self.is_repeat_request(question):
            if self.voice_mode == "off":
                await self.post_chat("Claude: voice answers are turned off for this meeting.")
            else:
                log("  repeating the last answer out loud")
                await self.speak(spoken_version(self.last_answer))
            return
        ans = None
        if self.agent:
            ack = None
            if to_chat and self.a.ack_after > 0:
                async def acknowledge():
                    await asyncio.sleep(self.a.ack_after)
                    await self.post_chat(f"Claude: working on {asker}'s question...")
                ack = asyncio.get_running_loop().create_task(acknowledge())
            try:
                ans, tools, cost = await self.agent.ask(self.memory(private), asker, question)
                log(f"  agent done: tools={tools or 'none'} cost=${cost or 0:.3f}")
            except Exception as e:
                log(f"  agent failed ({type(e).__name__}: {str(e)[:160]}) - falling back "
                    f"to transcript-only answer")
            finally:
                if ack:
                    ack.cancel()
        if ans is None:
            try:
                ans = await self.claude(f"Current date and time: {now_text()}\n{asker} asks: {question}",
                                        private=private)
            except Exception as e:
                log(f"Claude API error: {e}")
                return
        print(f"\n{'=' * 70}\nCLAUDE: {ans}\n{'=' * 70}\n", flush=True)
        self.add_line("Claude", f"(Q from {asker}: {question}) {ans}", private)
        if to_chat:
            await self.post_chat(f"Claude: {ans}")
            self.last_answer = ans
        if to_chat and self.wants_voice(question):
            await self.speak(spoken_version(ans))

    # ------------------------------------------------------------------ voice
    def wants_voice(self, question):
        if self.voice_mode == "always":
            return True
        return self.voice_mode == "asked" and bool(VOICE_ASK.search(question or ""))

    def speech_config(self):
        resource = os.environ.get("SPEECH_RESOURCE") or os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE")
        key = os.environ.get("SPEECH_KEY") or os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
        return resource, key, os.environ.get("SPEECH_ENDPOINT")

    async def tts(self, text):
        resource, key, endpoint = self.speech_config()
        if not key or not (resource or endpoint):
            raise RuntimeError("no Speech resource/key (set SPEECH_RESOURCE + SPEECH_KEY)")
        return await asyncio.to_thread(azure_tts, text, self.a.voice_name, resource, key, endpoint)

    async def wait_for_floor(self, quiet_s=1.5, max_s=10):
        """Don't talk over people: wait until captions have been quiet for quiet_s."""
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            if time.monotonic() - self.last_other_caption >= quiet_s:
                return True
            await asyncio.sleep(0.25)
        return False

    MIC_SELECTORS = ("#mic-button", '[data-tid="microphone-button"]', '[data-tid="toggle-mute"]',
                     'button[aria-label*="mute" i]', 'button[aria-label*="microphone" i]',
                     'button[aria-label*="mic" i]')

    async def mic_button(self):
        for sel in self.MIC_SELECTORS:
            try:
                b = self.page.locator(sel).first
                if await b.count() and await b.is_visible():
                    return b
            except Exception:
                pass
        return None

    async def mic_state(self, b):
        """True = live, False = muted, None = can't tell."""
        try:
            aria = (await b.get_attribute("aria-label") or "").strip().lower()
            if aria.startswith("unmute") or "unmute" in aria:
                return False
            if aria.startswith("mute"):
                return True
            pressed = await b.get_attribute("aria-pressed") or await b.get_attribute("aria-checked")
            if pressed in ("true", "false"):
                return pressed == "true"
        except Exception:
            pass
        return None

    async def log_toolbar(self):
        """Once per meeting: record the call-control buttons, for diagnosing UI changes."""
        try:
            items = await self.page.evaluate("""() => [...document.querySelectorAll('button')]
                .filter(b => b.offsetWidth && /mic|mute|camera|video/i.test(
                    (b.id || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('data-tid') || '')))
                .map(b => `${b.id || '-'}|${b.getAttribute('data-tid') || '-'}|${b.getAttribute('aria-label') || '-'}`)""")
            log(f"  call controls: {items[:8]}")
        except Exception:
            pass

    async def set_mic(self, on):
        want = "live" if on else "muted"
        b = await self.mic_button()
        state = await self.mic_state(b) if b else None
        if state is on:
            return True
        if b:
            await self.robust_click(b, "mic")
            await asyncio.sleep(0.5)
            b = await self.mic_button()
            if b and await self.mic_state(b) is on:
                log(f"  mic {want}")
                return True
        try:                                         # Teams shortcut: toggle mute
            await self.page.keyboard.press("Control+Shift+M")
            await asyncio.sleep(0.5)
        except Exception:
            pass
        b = await self.mic_button()
        state = await self.mic_state(b) if b else None
        if state is on:
            log(f"  mic {want} (shortcut)")
            return True
        log(f"  WARNING: could not confirm mic {want} (state={state}, "
            f"label={(await b.get_attribute('aria-label')) if b else 'no button'})")
        return False

    async def speak(self, text):
        if not text:
            return
        async with self.speak_lock:
            try:
                wav = await self.tts(text)
            except Exception as e:
                log(f"  voice skipped - text-to-speech failed: {str(e)[:150]}")
                return
            if not await self.wait_for_floor():
                log("  voice skipped - people kept talking (answer is in chat)")
                return
            try:
                await self.set_mic(True)
                secs = await self.page.evaluate("b => window.__claudeSpeak(b)",
                                                base64.b64encode(wav).decode())
                log(f"  spoke {secs:.1f}s: {text[:80]}")
            except Exception as e:
                log(f"  voice playback failed: {str(e)[:150]}")
            finally:
                await asyncio.sleep(0.3)
                await self.set_mic(False)

    async def summarize(self, to_chat=False):
        if not self.memory():
            log("No transcript yet.")
            return
        try:
            text = await self.claude(SUMMARY_PROMPT, max_tokens=4000)
        except Exception as e:
            log(f"Claude API error: {e}")
            return
        out = self.path.replace("transcript", "summary")
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        log(f"Summary saved: {os.path.abspath(out)}")
        if to_chat:
            wrap = await self.claude(CHAT_SUMMARY_PROMPT, max_tokens=500)
            await self.post_chat(f"Claude - meeting wrap-up:\n{wrap}")

    # ------------------------------------------------------------------ Teams UI
    async def debug(self, step):
        os.makedirs(self.debug_dir, exist_ok=True)
        base = os.path.join(self.debug_dir, f"{dt.datetime.now():%H%M%S}_{step}")
        try:
            await self.page.screenshot(path=base + ".png", full_page=True)
            with open(base + ".html", "w", encoding="utf-8") as f:
                f.write(await self.page.content())
            log(f"  debug saved: {base}.png/.html")
        except Exception:
            pass

    async def manual(self, step, instruction, check, timeout_s=180):
        """Fallback when a selector misses: save debug, ask the operator, wait for state."""
        await self.debug(step)
        if self.a.headless:
            return False
        where = "the Meeting view tab" if self.a.live_view and self.a.browser == "embedded" \
            else "the bot's browser window"
        log(f"ACTION NEEDED: {instruction} (in {where}). Waiting...")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await check():
                log("  thanks - continuing")
                return True
            await asyncio.sleep(1)
        return False

    async def click(self, key, timeout_s=10):
        l = await first_visible(self.page, SEL[key], timeout_s)
        if l:
            return await self.robust_click(l, key)
        return False

    async def robust_click(self, l, what):
        """Normal click; if something covers the target, clear overlays and retry."""
        try:
            await l.click(timeout=5000)
            return True
        except Exception as e:
            why = next((ln.strip() for ln in str(e).splitlines() if "intercepts pointer events" in ln),
                       str(e).splitlines()[0])
            log(f"  click on {what} blocked ({why[:160]}) - clearing overlays")
        for name in (r"^\s*close\s*$", r"^\s*dismiss\s*$", r"^\s*got it\s*$", r"^\s*not now\s*$"):
            try:
                b = self.page.get_by_role("button", name=re.compile(name, re.I)).first
                if await b.count() and await b.is_visible():
                    await b.click(timeout=1500)
            except Exception:
                pass
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        for how, fn in (("normal", lambda: l.click(timeout=3000)),
                        ("script", lambda: l.evaluate("e => e.click()")),
                        ("force", lambda: l.click(timeout=3000, force=True))):
            try:
                await fn()
                log(f"  clicked {what} ({how})")
                return True
            except Exception:
                pass
        log(f"  could not click {what}")
        return False

    async def switch_off(self, key):
        l = await first_visible(self.page, SEL[key], 2)
        if not l:
            return
        try:
            on = await l.is_checked()
        except Exception:
            state = (await l.get_attribute("aria-checked")) or (await l.get_attribute("aria-pressed"))
            on = state == "true"
        label = key.replace("_switch", "")
        if not on:
            return
        for attempt in range(2):
            if not on:
                break
            if attempt == 0:
                await self.robust_click(l, key)
            else:
                try:
                    await l.evaluate("e => e.click()")
                except Exception:
                    pass
            await asyncio.sleep(0.4)
            try:
                on = await l.is_checked()
            except Exception:
                on = False
        if on:
            log(f"  WARNING: {label} still on before joining (it is muted again once inside)")
        else:
            log(f"  {label} off before joining")

    async def join(self):
        p = self.page
        log("Opening meeting link...")
        # One navigation, no load wait: the /meet/ short links bounce through a launcher
        # that tries to hand off to the desktop app, and "load" may never fire.
        direct = direct_web_join(self.a.url, guest=self.a.guest)
        if direct:
            log("  going straight to Teams web join (no launcher)")
            await self.nav(direct)
        else:
            await self.nav(teams_web_url(self.a.url))
            log(f"  landed on {p.url[:100]}")
            via_launcher = direct_join_url(p.url)
            if via_launcher:
                log("  skipping launcher -> Teams web join")
                await self.nav(via_launcher)
        if "launcher" in p.url or await first_visible(p, SEL["continue_browser"], 3):
            await self.leave_launcher()

        name = await first_visible(p, SEL["name_input"], 60 if self.a.guest else 20)
        if name:
            await name.fill(self.a.name)
            log(f"  name: {self.a.name}")
        elif self.a.guest:
            await self.debug("no_name_field")
        await self.switch_off("mic_switch")
        await self.switch_off("cam_switch")
        await self.join_ready()

        if not await self.click("join_now", timeout_s=60):
            ok = await self.manual("join_button", "click 'Join now'",
                                   lambda: self.in_meeting_or_lobby())
            if not ok:
                raise RuntimeError("Could not find the Join button (see bot_debug/).")
        log("Join clicked.")

    async def join_ready(self, wait_s=10):
        """Join stays disabled while Teams rejects something (e.g. the name). Say why."""
        j = await first_visible(self.page, SEL["join_now"], 15)
        if not j:
            return
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                if not await j.is_disabled():
                    return
            except Exception:
                return
            await asyncio.sleep(0.5)
        body = await self.body_text()
        hint = next((ln.strip() for ln in body.splitlines()
                     if re.search(r"can only include|not allowed|required|invalid", ln, re.I)), "")
        log(f"  Join is disabled{': ' + hint if hint else ''}")
        await self.debug("join_disabled")

    async def leave_launcher(self, max_s=60):
        """Click 'Continue on this browser' until the launcher is really gone."""
        start_url = self.page.url
        deadline = time.monotonic() + max_s
        clicks = 0
        while time.monotonic() < deadline:
            btn = await first_visible(self.page, SEL["continue_browser"], 0)
            if not btn:
                if clicks or self.page.url != start_url:
                    log(f"  launcher passed after {clicks} click(s)")
                    return True
                await asyncio.sleep(1)                       # not rendered yet
                continue
            try:
                await btn.click()
                clicks += 1
                if clicks == 1:
                    log("  chose 'Continue on this browser'")
            except Exception:
                pass
            for _ in range(8):                               # give it 4s to react
                await asyncio.sleep(0.5)
                if self.page.url != start_url or not await first_visible(
                        self.page, SEL["continue_browser"], 0):
                    log(f"  launcher passed after {clicks} click(s)")
                    return True
        await self.debug("launcher_stuck")
        return False

    async def nav(self, url):
        try:
            await self.page.goto(url, wait_until="commit", timeout=20000)
        except Exception as e:
            log(f"  navigation note: {str(e).splitlines()[0][:140]}")

    async def in_meeting_or_lobby(self):
        return bool(await first_visible(self.page, SEL["in_meeting"], 0)) or \
            bool(LOBBY_RE.search(await self.body_text()))

    async def body_text(self):
        try:
            return await self.page.inner_text("body", timeout=2000)
        except Exception:
            return ""

    async def wait_admitted(self):
        deadline = time.monotonic() + self.a.lobby_timeout * 60
        said = False
        rejoin_at = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.ended.is_set():
                log("Leave requested before admission - not joining.")
                return
            if not said and time.monotonic() > rejoin_at:
                rejoin_at = deadline                         # only once
                j = await first_visible(self.page, SEL["join_now"], 0)
                if j:
                    log("  still on the pre-join screen - clicking Join again (script)")
                    try:
                        await j.evaluate("e => e.click()")
                    except Exception as e:
                        log(f"  rejoin click failed: {str(e)[:100]}")
            if await first_visible(self.page, SEL["in_meeting"], 0):
                log("In the meeting.")
                return
            if not said and LOBBY_RE.search(await self.body_text()):
                log("Waiting in the lobby - the organizer needs to admit the bot.")
                said = True
            await asyncio.sleep(2)
        await self.debug("lobby_timeout")
        raise RuntimeError("Not admitted before --lobby-timeout.")

    async def ensure_silent(self):
        """Safety net: if the bot's mic or camera came in on, turn them off."""
        await self.set_mic(False)
        for sel, prefix, label in (("#video-button", "turn camera off", "camera"),):
            try:
                b = self.page.locator(sel).first
                if await b.count():
                    aria = (await b.get_attribute("aria-label") or "").strip().lower()
                    if aria.startswith(prefix):
                        await b.click()
                        log(f"  turned {label} off")
            except Exception:
                pass

    async def captions_visible(self):
        try:
            return await self.page.locator(CAPTIONS_CONTAINER).count() > 0
        except Exception:
            return False

    async def menu_items(self):
        try:
            return await self.page.evaluate("""() => [...document.querySelectorAll(
                '[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio]')]
                .map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())
                .filter(Boolean)""")
        except Exception:
            return []

    async def wait_captions(self, how, seconds=10):
        for _ in range(int(seconds * 2)):
            if await self.captions_visible():
                log(f"Live captions on ({how}).")
                return True
            await asyncio.sleep(0.5)
        return False

    async def enable_captions(self):
        if await self.captions_visible():
            return True
        # 1. Teams keyboard shortcut for live captions.
        try:
            await self.page.keyboard.press("Alt+Shift+C")
            if await self.wait_captions("keyboard shortcut", 5):
                return True
        except Exception:
            pass
        # 2. More menu: a captions item directly, or under Language and speech.
        for _ in range(2):
            if await self.click("more", 8):
                await asyncio.sleep(1)
                log(f"  More menu: {await self.menu_items()}")
                if await self.click("captions_on", 3) and await self.wait_captions("More menu"):
                    return True
                if await self.click("lang_speech", 3):
                    await asyncio.sleep(1)
                    log(f"  Language and speech menu: {await self.menu_items()}")
                    if await self.click("captions_on", 5) and await self.wait_captions("Language and speech"):
                        return True
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(1)
        return await self.manual(
            "captions", "turn on live captions (More > Language and speech > Show live captions)",
            self.captions_visible)

    async def post_chat(self, text):
        text = chat_text(text)[:3500]
        async with self.chat_lock:
            box = await first_visible(self.page, SEL["chat_box"], 1)
            if not box:
                await self.click("chat_button", 6)
                box = await first_visible(self.page, SEL["chat_box"], 8)
            if not box:
                await self.debug("chat_box")
                log("  [chat post failed - answer is on the console only]")
                return False
            await box.click()
            for i, line in enumerate(text.split("\n")):
                if i:
                    await self.page.keyboard.press("Shift+Enter")
                if line:
                    await self.page.keyboard.insert_text(line)
            await self.page.keyboard.press("Enter")
            log("  [posted to meeting chat]")
            return True

    async def in_call(self):
        for spec in SEL["in_meeting"]:
            try:
                if await _loc(self.page, spec).count():
                    return True
            except Exception:
                pass
        return False

    async def watch_end(self):
        """Ended = the Leave control has been gone for 3 checks in a row (or the page
        closed). Spoken or typed words can't trigger it, unlike matching page text."""
        misses, cap_gone = 0, 0
        while not self.ended.is_set():
            await asyncio.sleep(self.a.end_check)
            try:
                if self.page.is_closed():
                    misses = 99
                else:
                    misses = 0 if await self.in_call() else misses + 1
            except Exception:
                misses += 1
            if misses >= 3:
                log("Meeting ended or bot removed.")
                self.ended.set()
                return
            # Captions watchdog: Teams occasionally drops captions (reconnects, layout
            # changes). Re-enable rather than silently going deaf.
            if misses == 0 and not await self.captions_visible():
                cap_gone += 1
                if cap_gone >= 2:
                    log("Captions disappeared - re-enabling...")
                    if await self.enable_captions():
                        cap_gone = 0
            else:
                cap_gone = 0

    async def console(self):
        loop = asyncio.get_running_loop()
        q = asyncio.Queue()

        def reader():
            # No console (background/scheduled run, test harness) -> just stop reading.
            try:
                for line in sys.stdin:
                    loop.call_soon_threadsafe(q.put_nowait, line)
            except Exception:
                pass

        threading.Thread(target=reader, daemon=True).start()
        while not self.ended.is_set():
            cmd = (await q.get()).strip()
            if cmd:
                try:
                    await self.handle_command(cmd)
                except Exception as e:
                    log(f"  command failed ({cmd[:30]}): {str(e)[:120]}")

    async def handle_command(self, cmd):
        low = cmd.lower()
        if low.startswith(("/click ", "/type ", "/key ", "/scroll ")):
            return await self.page_input(cmd)
        if not self.listening and low not in ("/leave",):
            log("  (not in the meeting yet - only clicks, typing and /leave work now)")
            return
        if low == "/leave":
            self.ended.set()
        elif low == "/summary":
            self.spawn(self.summarize())
        elif low == "/post-summary":
            self.spawn(self.summarize(to_chat=True))
        elif low.startswith("/speak "):
            self.spawn(self.speak(cmd[7:]))
        elif low.startswith("/voice "):
            mode = low[7:].strip()
            if mode in ("off", "asked", "always"):
                self.voice_mode = mode
                log(f"  voice mode: {mode}")
        elif low.startswith("/say "):
            self.spawn(self.post_chat(cmd[5:]))
        elif low.startswith("/chat "):
            self.spawn(self.answer(cmd[6:], "Operator", to_chat=True))
        else:
            self.spawn(self.answer(cmd, "Operator", to_chat=False))

    async def page_input(self, cmd):
        """Operator input from the app's Meeting view: viewport coordinates / keys."""
        verb, _, arg = cmd.partition(" ")
        verb = verb.lower()
        if verb == "/click":
            x, y = (float(v) for v in arg.split()[:2])
            await self.page.mouse.click(x, y)
            log(f"  operator click at {int(x)},{int(y)}")
        elif verb == "/type":
            await self.page.keyboard.type(arg, delay=20)
            log(f"  operator typed {len(arg)} characters")
        elif verb == "/key":
            await self.page.keyboard.press(arg.strip())
            log(f"  operator key {arg.strip()}")
        elif verb == "/scroll":
            parts = arg.split()
            x, y, dy = float(parts[0]), float(parts[1]), float(parts[2])
            await self.page.mouse.move(x, y)
            await self.page.mouse.wheel(0, dy)
        await self.write_frame()

    async def live_view(self):
        """Live frames for the app's Meeting view tab (latest frame only, ~1 fps)."""
        while not self.ended.is_set():
            await self.write_frame()
            await asyncio.sleep(self.a.frame_interval)

    async def write_frame(self):
        if not self.a.live_view:
            return
        try:
            if self.page.is_closed():
                return
            data = await self.page.screenshot(type="jpeg", quality=70, timeout=5000)
            tmp = self.frame_path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.frame_path)
        except Exception:
            pass

    async def leave(self):
        try:
            l = await first_visible(self.page, SEL["in_meeting"], 2)
            if l:
                await l.click()
        except Exception:
            pass

    # ------------------------------------------------------------------ main
    async def run(self, pw):
        wav = os.path.join(self.a.outdir, "silence.wav")
        make_silence_wav(wav)
        self.browser = await pw.chromium.launch(
            headless=self.a.headless,
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                  f"--use-file-for-fake-audio-capture={os.path.abspath(wav)}",
                  "--autoplay-policy=no-user-gesture-required",
                  "--disable-dev-shm-usage",
                  # keep rendering, timers and audio running while the window is hidden
                  "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                  "--disable-background-timer-throttling"]
                 + (["--window-position=-32000,-32000"]
                    if self.a.browser == "embedded" and not self.a.headless else [])
                 # the bot hears the meeting through captions, so it never needs to play the
                 # meeting's audio; playing it on the meeting PC's speakers causes an echo
                 + ([] if self.a.hear_meeting else ["--mute-audio"]))
        log("Meeting audio playback: " + ("ON (--hear-meeting)" if self.a.hear_meeting
                                          else "muted on this PC (prevents echo)"))
        use_auth = not self.a.guest and os.path.exists(self.a.auth)
        if not self.a.guest and not use_auth:
            log(f"No {self.a.auth} found - joining as guest '{self.a.name}'. "
                f"Run --login first to join as the service account.")
            self.a.guest = True
        ctx = await self.browser.new_context(
            viewport={"width": 1366, "height": 860},
            storage_state=self.a.auth if use_auth else None,
            permissions=["microphone", "camera"])
        await ctx.add_init_script(INIT_JS)
        self.page = await ctx.new_page()
        await self.page.expose_binding("claudeCaption", self.on_caption)
        # Any JS dialog (alert/confirm/beforeunload) must never stall the bot.
        self.page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

        early = [asyncio.create_task(self.console()), asyncio.create_task(self.live_view())]
        try:
            await self.join()
            await self.wait_admitted()
        except BaseException:
            for t in early:
                t.cancel()
            raise
        if self.ended.is_set():
            for t in early:
                t.cancel()
            await self.leave()
            await self.browser.close()
            return
        await self.log_toolbar()
        await self.ensure_silent()
        if not await self.enable_captions():
            raise RuntimeError("Live captions unavailable - the bot cannot hear the meeting.")
        await self.page.evaluate(SCRAPER_JS, [CAPTION_TEXT, CAPTION_AUTHOR, CAPTION_ITEM])
        log(f"Listening. Transcript -> {os.path.abspath(self.path)}")
        log(f"Agent: {self.agent.describe() if self.agent else 'off (transcript-only answers)'}")
        log(f"Voice: {self.voice_mode} ({self.a.voice_name})")
        if self.a.intro and not self.a.console_only:
            await self.post_chat(
                "Claude meeting assistant has joined and is reading Teams live captions to "
                "keep a transcript. Ask a question by saying \"Hey Claude, ...\" - answers post here. "
                "Add \"out loud\" to hear the answer spoken.")

        self.listening = True
        tasks = early + [asyncio.create_task(t) for t in (self.finalizer(), self.watch_end())]
        interrupted = False
        try:
            await self.ended.wait()
        except asyncio.CancelledError:
            interrupted = True          # Ctrl+C: still write transcript + summary
            log("Interrupted - wrapping up (transcript + summary)...")
        finally:
            for t in tasks:
                t.cancel()
        await self.wrap_up(post=not interrupted)

    async def wrap_up(self, post=True):
        try:
            post = post and self.a.post_summary and not self.page.is_closed() \
                and await self.in_call()
            await self.summarize(to_chat=post)
        except Exception as e:
            log(f"Summary failed: {e}")
        await self.leave()
        log(f"Transcript: {os.path.abspath(self.path)}")
        try:
            await self.browser.close()
        except Exception:
            pass


async def login(args):
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=False)
        ctx = await b.new_context()
        p = await ctx.new_page()
        await p.goto("https://teams.microsoft.com/")
        print("\nSign in to Teams in the browser window as the bot's account (complete MFA).\n"
              "When Teams has fully loaded, press Enter here...", flush=True)
        await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
        await ctx.storage_state(path=args.auth)
        print(f"Saved sign-in to {args.auth}. Treat this file like a password.")
        await b.close()


async def preflight(args):
    """--check: verify everything except the meeting itself."""
    ok = True

    def res(name, good, detail=""):
        nonlocal ok
        ok &= good
        print(f"  [{'PASS' if good else 'FAIL'}] {name}{' - ' + detail if detail else ''}")

    print("Preflight:")
    res("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    try:
        async with async_playwright() as pw:
            b = await pw.chromium.launch(headless=True)
            pg = await b.new_page()
            await pg.set_content("<p>ok</p>")
            await b.close()
        res("Chromium launches", True)
    except Exception as e:
        res("Chromium launches", False, f"{e} -> run: python -m playwright install chromium")
    if foundry_enabled():
        key = os.environ.get("ANTHROPIC_FOUNDRY_API_KEY") or os.environ.get("ANTHROPIC_FOUNDRY_AUTH_TOKEN")
        res("Microsoft Foundry configured",
            bool((os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE") or os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL")) and key),
            f"resource={os.environ.get('ANTHROPIC_FOUNDRY_RESOURCE') or os.environ.get('ANTHROPIC_FOUNDRY_BASE_URL')}, "
            f"deployment={args.model}")
    else:
        key = os.environ.get("ANTHROPIC_API_KEY")
        res("ANTHROPIC_API_KEY set", bool(key))
    if key:
        try:
            r = await make_client().messages.create(
                model=args.model, max_tokens=5,
                messages=[{"role": "user", "content": "Reply with OK"}])
            res(f"Claude API + model {args.model}", True, r.content[0].text.strip())
        except Exception as e:
            res(f"Claude API + model {args.model}", False, str(e)[:200])
    if not args.no_agent:
        try:
            ag = MeetingAgent(args.agent_dir, args.model, args.agent_max_turns,
                              args.agent_budget, args.agent_timeout, args.project)
            res("Agent config", True, ag.describe())
            if key:
                try:
                    ans, tools, cost = await ag.ask(
                        "(no meeting yet)", "Preflight",
                        "List the files in your project folder and the MCP tools you can use. "
                        "Use your tools to check, then answer in two sentences.")
                    res("Agent run (files + MCP)", True,
                        f"tools used: {sorted(set(tools)) or 'none'}; ${cost or 0:.3f}\n        -> {ans[:300]}")
                except Exception as e:
                    res("Agent run (files + MCP)", False, str(e)[:300])
        except Exception as e:
            res("Agent config", False, str(e))
    if args.voice != "off":
        resource = os.environ.get("SPEECH_RESOURCE") or os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE")
        skey = os.environ.get("SPEECH_KEY") or os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
        try:
            t0 = time.monotonic()
            wav = await asyncio.to_thread(azure_tts, "Preflight check.", args.voice_name, resource,
                                          skey, os.environ.get("SPEECH_ENDPOINT"))
            res(f"Azure AI Speech ({args.voice_name})", len(wav) > 1000,
                f"{len(wav)} bytes in {int((time.monotonic() - t0) * 1000)} ms, resource={resource}")
        except Exception as e:
            res(f"Azure AI Speech ({args.voice_name})", False, str(e)[:200])
    has_auth = os.path.exists(args.auth)
    print(f"  [INFO] Saved sign-in ({args.auth}): "
          f"{'present - will join as that account' if has_auth else 'absent - will join as guest'}")
    if args.context:
        res(f"Context file {args.context}", os.path.exists(args.context))
    print("READY" if ok else "NOT READY - fix the FAIL lines above")
    return ok


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Claude Teams meeting bot")
    p.add_argument("--url", help="Teams meeting join link")
    p.add_argument("--login", action="store_true", help="sign in once and save teams_auth.json")
    p.add_argument("--check", action="store_true", help="preflight: browser, API key, model")
    p.add_argument("--auth", default="teams_auth.json", help="saved sign-in state")
    p.add_argument("--guest", action="store_true", help="join anonymously as --name")
    p.add_argument("--name", default="Claude Meeting Assistant",
                   help="display name (letters, numbers, spaces, - ' . _ @ only)")
    p.add_argument("--voice", choices=["off", "asked", "always"], default="asked",
                   help="speak answers: never, when asked ('out loud'), or always")
    p.add_argument("--voice-name", default="en-US-AndrewNeural", help="Azure AI Speech voice")
    p.add_argument("--browser", choices=["window", "embedded"], default="window",
                   help="embedded: browser runs off-screen and the app shows it in its Meeting view")
    p.add_argument("--no-live-view", dest="live_view", action="store_false",
                   help="don't write live_view.jpg frames")
    p.add_argument("--frame-interval", type=float, default=1.0, help="seconds between live frames")
    p.add_argument("--context", help="agenda/background file given to Claude")
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--headless", action="store_true", help="no visible browser (disables manual fallback)")
    p.add_argument("--console-only", action="store_true", help="never post to chat")
    p.add_argument("--no-intro", dest="intro", action="store_false", help="skip the join announcement")
    p.add_argument("--no-post-summary", dest="post_summary", action="store_false")
    p.add_argument("--stable", type=float, default=1.2, help="seconds unchanged = caption final")
    p.add_argument("--hear-meeting", action="store_true",
                   help="play the meeting's audio on this PC (off by default: avoids echo)")
    p.add_argument("--question-wait", type=float, default=1.8,
                   help="seconds the asker must be quiet before a question is answered")
    p.add_argument("--question-wait-short", type=float, default=0.6,
                   help="quiet time needed when the caption already ends with '?'")
    p.add_argument("--lobby-timeout", type=float, default=20, help="minutes to wait for admission")
    p.add_argument("--end-check", type=float, default=5, help="seconds between end-of-meeting checks")
    p.add_argument("--agent-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent"),
                   help="folder with instructions.md, project/, mcp.json")
    p.add_argument("--project", default=None,
                   help="pre-meeting materials folder (default: <agent-dir>/project)")
    p.add_argument("--no-agent", action="store_true", help="transcript-only answers (no tools)")
    p.add_argument("--agent-max-turns", type=int, default=12)
    p.add_argument("--agent-budget", type=float, default=0.75, help="max USD per question")
    p.add_argument("--agent-timeout", type=float, default=120, help="seconds per question")
    p.add_argument("--ack-after", type=float, default=8,
                   help="post 'working on it' in chat if the agent takes longer (0=off)")
    p.add_argument("--outdir", default=".")
    return p.parse_args(argv)


async def amain(args):
    if args.login:
        return await login(args)
    if args.check:
        sys.exit(0 if await preflight(args) else 1)
    if not args.url:
        sys.exit("--url is required (or --login).")
    if not (os.environ.get("ANTHROPIC_API_KEY") or foundry_enabled()):
        sys.exit("Set ANTHROPIC_API_KEY, or configure Microsoft Foundry (see foundry.env.ps1).")
    async with async_playwright() as pw:
        bot = MeetingBot(args)
        try:
            await bot.run(pw)
        except Exception:
            log("CRASH:\n" + traceback.format_exc())
            try:
                await bot.debug("crash")
            except Exception:
                pass
            raise


if __name__ == "__main__":
    try:
        asyncio.run(amain(parse_args()))
    except KeyboardInterrupt:
        print("\nStopped.")

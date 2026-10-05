#!/usr/bin/env python3
"""Claude-Teams-Bot - Windows desktop app for the Claude Teams meeting bot.

Control tab: meeting link, bot name (pre-filled), knowledge folder (the files the bot is
grounded in) and voice mode; Join meeting / Check setup; a live activity feed of what the
bot hears and answers; and a box to ask Claude privately or have it say something out loud.
Meeting view tab: the bot's browser, live - click and type into it when a step needs a hand.
Colours, fonts and widget styles live in app_style.py.

Launch: double-click the "Claude-Teams-Bot" desktop shortcut (created by
install_shortcut.ps1), or run  .venv\\Scripts\\pythonw.exe app.py
"""
import datetime as dt
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app_style import (ACCENT, ACCENT_SOFT, BORDER, BORDER_STRONG, DANGER, MUTED, SUBTLE, SUCCESS,
                       SURFACE, TEXT, VIEW_BG, WARN_BG, WARN_FG, WARN_MARK, StatusPill, Theme,
                       enable_dpi_awareness, icon_image)

try:
    from PIL import Image, ImageTk
except Exception:                                    # view tab shows a hint instead
    Image = ImageTk = None

APP_NAME = "Claude-Teams-Bot"
HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(HERE, "teams_meeting_bot.py")
DEFAULT_NAME = "Claude Meeting Assistant"
DEFAULT_KB = os.path.join(HERE, "agent", "project")
SETTINGS = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Claude-Teams-Bot",
                        "settings.json")
VOICE_CHOICES = {"When asked (\"out loud\")": "asked", "Always": "always", "Off": "off"}
VOICE_SEGMENTS = (("When asked", "When asked (\"out loud\")"), ("Always", "Always"), ("Off", "Off"))
VOICE_HELP = {"asked": "Speaks an answer only when the question says “out loud”.",
              "always": "Speaks every answer in the meeting, as well as posting it in the chat.",
              "off": "Answers go to the meeting chat only."}
LINK_HELP = "Paste the Teams join link. It starts with https://teams.microsoft.com/"
KB_HELP = "This meeting’s agenda, briefs and decks (PDF, Word, PowerPoint, Excel, text). Claude cites them."
ASK_HELP_IDLE = "Available once the bot is in the meeting."
ASK_HELP_LIVE = ("Ask privately: only you see the answer.   Say out loud: Claude speaks your text, "
                 "word for word, in the meeting.")
VIEW_HINT = "The bot’s browser, live. Click in the picture to click in the meeting, or type below."
TEAMS_LINK = re.compile(r"^https://teams\.(microsoft\.com|cloud\.microsoft|live\.com)/\S+$", re.I)
NAME_BAD_CHARS = re.compile(r"[^A-Za-z0-9 \-'._@]")
ENV_LINE = re.compile(r"""^\s*\$env:(\w+)\s*=\s*(["'])(.*?)\2\s*(#.*)?$""")

STATUS_RULES = [            # (log text, status, tone) - first match wins
    ("CRASH", "Stopped with an error – see Activity", "error"),
    ("ACTION NEEDED", "Needs you", "action"),
    ("Transcript:", "Finished – transcript and summary saved", "done"),
    ("Meeting ended", "Meeting ended – writing summary", "busy"),
    ("Listening.", "In the meeting – listening for “Hey Claude”", "live"),
    ("In the meeting.", "In the meeting – turning on captions", "live"),
    ("Waiting in the lobby", "In the lobby – admit the bot in Teams", "action"),
    ("Join clicked", "Joining – admit the bot from the lobby", "action"),
    ("Opening meeting link", "Opening the meeting…", "busy"),
]

# Activity feed. Each bot stdout line is inserted verbatim (copy/paste and the tests see
# exactly what the bot printed); classify_line() picks how it looks.
TS_LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] ")
SPEECH = re.compile(r"^([^\W\d_][\w .,'()@-]{0,60}?): \S")
NOT_SPEAKERS = {"Agent", "Voice", "Transcript", "Summary saved", "Summary failed",
                "Claude API error", "Meeting audio playback", "ACTION NEEDED", "CRASH"}
FENCE = re.compile(r"^={20,}$")                      # the bot prints answers between ==== lines
SOURCE_PAREN = re.compile(r"\(\s*sources?:[^)]*\)", re.I)     # same pattern as the bot's
DETAIL_WARN = re.compile(r"WARNING|failed|could not|voice skipped", re.I)
WHERE = re.compile(r"\s*\(in (.+?)\)\.?\s*Waiting\.*\s*$")
# Lines whose text comes from people or the model never change the status or banner.
PEOPLE = {"speech", "question", "answer", "answer_more", "fence_open", "fence_close", "gap"}
DETAILS = {"detail", "detail_keep", "warn"}          # incl. tool inputs and spoken answer text


def clean_name(name):
    return re.sub(r"\s+", " ", NAME_BAD_CHARS.sub(" ", name)).strip()


def load_env_file(path):
    """Read $env:NAME = "value" lines (foundry.env.ps1) without running PowerShell."""
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                m = ENV_LINE.match(line)
                if m:
                    env[m.group(1)] = m.group(3)
    return env


def bot_python():
    """python.exe next to the running interpreter (the app itself runs under pythonw)."""
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


def build_command(link, name, kb, voice, outdir, python=None):
    """The bot's browser runs off-screen and shows in the Meeting view. For a real browser
    window (troubleshooting, DevTools), run the bot with run.ps1 instead."""
    cmd = [python or bot_python(), "-u", BOT, "--url", link, "--name", name,
           "--voice", voice, "--outdir", outdir, "--browser", "embedded"]
    if kb:
        cmd += ["--project", kb]
    return cmd


def canvas_to_page(cx, cy, layout):
    """Map a click on the scaled view back to browser-viewport pixels, or None if outside."""
    if not layout:
        return None
    scale, ox, oy, w, h = layout
    x, y = (cx - ox) / scale, (cy - oy) / scale
    if 0 <= x < w and 0 <= y < h:
        return round(x), round(y)
    return None


def fit_layout(img_w, img_h, box_w, box_h):
    """Scale + offsets that fit the frame into the canvas, keeping its aspect ratio."""
    scale = min(box_w / img_w, box_h / img_h)
    w, h = int(img_w * scale), int(img_h * scale)
    return scale, (box_w - w) // 2, (box_h - h) // 2, img_w, img_h


def classify_line(line, state=""):
    """How a bot stdout line shows in the activity feed: returns (kind, next_state).
    state carries Claude's answers across lines: "answer" between the ==== fences, "after"
    right behind the closing fence. kind None = not shown."""
    s = line.strip()
    if FENCE.match(s):
        return ("fence_close", "after") if state == "answer" else ("fence_open", "")
    if line.startswith("CLAUDE:"):
        return "answer", "answer"
    if state == "answer":
        return "answer_more", "answer"
    if not s:
        return ("gap" if state == "after" else None), ""
    m = TS_LINE.match(line)
    body = line[m.end():] if m else ""
    if m and body.startswith("  "):                  # the bot indents technical details
        return ("warn" if DETAIL_WARN.search(body) else "detail"), ""
    if m and body.startswith("Q ("):
        return "question", ""
    sp = SPEECH.match(body)                          # before the markers below, so a caption
    if sp and sp.group(1) not in NOT_SPEAKERS:       # that says "CRASH" stays a caption
        return "speech", ""
    if "ACTION NEEDED" in line:
        return "action", ""
    if "CRASH" in line or re.match(r"(Traceback|File \"|\w+(Error|Exception)\b)", s):
        return "error", ""
    for prefix, kind in (("[PASS]", "pass"), ("[FAIL]", "fail"), ("[INFO]", "info"),
                         ("->", "info"), ("NOT READY", "fail")):
        if s.startswith(prefix):
            return kind, ""
    if s == "READY":
        return "ready", ""
    if not m:
        return "plain", ""
    if re.search(r"\berror\b|failed", body, re.I):
        return "error", ""
    return "event", ""


def keep_visible(line):
    """Technical details (the bot's indented log lines) are hidden unless "Show technical
    details" is ticked. Return True for details the operator should always see."""
    # TODO: decide which details stay on screen mid-meeting. Candidates:
    #   "[posted to meeting chat]"     delivery confirmation for each answer
    #   "mic live" / "mic muted"       the bot's own microphone state
    #   "spoke 3.9s: ..."              what was actually said out loud
    #   "agent tool: Read {...}"       what Claude is reading (shows file names on screen)
    return False


def feed_segments(line, kind):
    """Split a line into (text, tags) runs. Joined, the runs are always the original line."""
    if kind in ("answer", "answer_more"):
        segs, rest = [], line
        if kind == "answer":
            segs.append(("CLAUDE:", ("eyebrow",)))
            rest = line[len("CLAUDE:"):]
        pos = 0
        for m in SOURCE_PAREN.finditer(rest):
            segs += [(rest[pos:m.start()], ()), (m.group(0), ("source",))]
            pos = m.end()
        segs.append((rest[pos:], ()))
        return [s for s in segs if s[0]]
    m = TS_LINE.match(line)
    if not m:
        return [(line, ())]
    gap = "tsgap_warn" if kind == "action" else "tsgap"
    segs = [("[", ("hide",)), (m.group(1), ("ts",)), ("]", (gap,))]
    rest = line[m.end() - 1:]                        # keeps the space after "]"
    if kind == "speech":
        name, _, said = rest.partition(": ")
        segs += [(name + ":", ("speaker",)), (" " + said, ())]
    elif kind == "question":
        head, _, asked = rest.partition("): ")
        segs += [(head + "):", ("qhead",)), (" " + asked, ())]
    else:
        segs.append((rest, ()))
    return segs


def action_text(line):
    """"... ACTION NEEDED: click 'Join now' (in the Meeting view tab). Waiting..." ->
    ("Click 'Join now'", "the Meeting view tab")."""
    msg = line.split("ACTION NEEDED:", 1)[-1].strip()
    m = WHERE.search(msg)
    what = (msg[:m.start()] if m else msg).strip().rstrip(".")
    return what[:1].upper() + what[1:], (m.group(1) if m else "")


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception:
        pass


class App:
    def __init__(self, root, python=None):
        self.root, self.python = root, python
        self.proc, self.outdir, self.mode = None, None, None
        self.lines = queue.Queue()
        self.theme = Theme(root)
        self.px, self.f = self.theme.px, self.theme.f
        root.title(APP_NAME)
        try:
            root.iconbitmap(os.path.join(HERE, "claude_teams_bot.ico"))
        except Exception:
            pass
        px = self.px
        w = min(px(1060), root.winfo_screenwidth() - px(40))
        h = min(px(840), root.winfo_screenheight() - px(90))
        root.geometry(f"{w}x{h}")
        root.minsize(min(px(860), w), min(px(620), h))
        s = load_settings()
        self.link = tk.StringVar(value=s.get("link", ""))
        self.name = tk.StringVar(value=s.get("name", DEFAULT_NAME))
        self.kb = tk.StringVar(value=s.get("kb", DEFAULT_KB))
        voice = s.get("voice")
        self.voice = tk.StringVar(value=voice if voice in VOICE_CHOICES else next(iter(VOICE_CHOICES)))
        self.status = tk.StringVar(value="Ready")
        self.ask_text = tk.StringVar()
        self.show_details = tk.BooleanVar(value=s.get("show_details", False))
        self.type_text = tk.StringVar()
        self.frame_mtime = 0
        self.layout = None
        self.photo = None
        self.feed_state = ""
        self.feed_empty = True
        self.view_placeholder = False
        self.banner_info = None                      # (what to do, where) while a step needs a hand
        self._build()
        self.set_status("Ready", "idle")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(150, self.pump)

    # ---------------------------------------------------------------- layout
    def _build(self):
        px = self.px
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        self._build_header()
        self._build_banner()
        self.tabs = ttk.Notebook(self.root, style="Tabless.TNotebook")
        self.tabs.grid(row=3, column=0, sticky="nsew")
        page = ttk.Frame(self.tabs, padding=(px(20), px(16)))
        self.view_tab = ttk.Frame(self.tabs, padding=(px(20), px(16)))
        self.tabs.add(page, text="Control")
        self.tabs.add(self.view_tab, text="Meeting view")
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self.sync_tabs())
        self.tabs.enable_traversal()                 # Ctrl+Tab switches pages
        self._build_control(page)
        self._build_view(self.view_tab)
        self.sync_tabs()

    def _build_header(self):
        px = self.px
        bar = ttk.Frame(self.root, style="Header.TFrame", padding=(px(20), 0))
        bar.grid(row=0, column=0, sticky="ew")
        tk.Frame(self.root, bg=BORDER, height=1).grid(row=1, column=0, sticky="ew")
        bar.columnconfigure(3, weight=1)
        self.logo = icon_image(os.path.join(HERE, "claude_teams_bot.ico"), px(26))
        if self.logo:
            ttk.Label(bar, image=self.logo, style="Header.TLabel").grid(row=0, column=0, padx=(0, px(10)))
        ttk.Label(bar, text=APP_NAME, style="Title.Header.TLabel").grid(row=0, column=1, sticky="w")
        tabs = ttk.Frame(bar, style="Header.TFrame")
        tabs.grid(row=0, column=2, sticky="sw", padx=(px(32), 0))
        self.tab_choice = tk.IntVar(value=0)
        self.tab_marks = []
        for i, label in enumerate(("Control", "Meeting view")):
            ttk.Radiobutton(tabs, text=label, value=i, variable=self.tab_choice, style="Tab.TRadiobutton",
                            command=lambda i=i: self.tabs.select(i)).grid(row=0, column=i, padx=(0, px(4)))
            mark = tk.Frame(tabs, height=px(3), bg=SURFACE)
            mark.grid(row=1, column=i, sticky="ew", padx=(px(6), px(10)))
            self.tab_marks.append(mark)
        self.pill = StatusPill(bar, self.theme)
        self.pill.grid(row=0, column=4, sticky="e", pady=px(12))

    def _build_banner(self):
        px = self.px
        self.banner = tk.Frame(self.root, bg=WARN_BG)
        self.banner.grid(row=2, column=0, sticky="ew")
        tk.Frame(self.banner, bg=WARN_MARK, width=px(4)).pack(side="left", fill="y")
        body = tk.Frame(self.banner, bg=WARN_BG)
        body.pack(side="left", fill="both", expand=True, padx=(px(16), px(20)), pady=px(10))
        ttk.Label(body, text="Action needed", style="Title.Banner.TLabel").pack(side="left")
        self.banner_btn = ttk.Button(body, text="Open Meeting view",
                                     command=lambda: self.tabs.select(self.view_tab))
        self.banner_btn.pack(side="right")
        self.banner_text = ttk.Label(body, text="", style="Banner.TLabel")
        self.banner_text.pack(side="left", fill="x", expand=True, padx=(px(12), px(12)))
        body.bind("<Configure>", lambda e: self.banner_text.configure(
            wraplength=max(px(200), e.width - px(330))))
        self.banner.grid_remove()

    def card(self, parent, row, pady=0, pad=(18, 14)):
        """White panel with a 1px outline; returns the padded inner frame."""
        outer = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=BORDER)
        outer.grid(row=row, column=0, sticky="nsew", pady=pady)
        inner = ttk.Frame(outer, style="Card.TFrame", padding=(self.px(pad[0]), self.px(pad[1])))
        inner.pack(fill="both", expand=True)
        return inner

    def _build_control(self, page):
        px = self.px
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        self._build_setup(self.card(page, 0, pady=(0, px(12))))
        self._build_feed(self.card(page, 1, pady=(0, px(12)), pad=(18, 12)))
        self._build_composer(self.card(page, 2))

    def _build_setup(self, c):
        px = self.px
        c.columnconfigure(0, weight=1)
        self.setup_title = ttk.Label(c, text="Meeting setup", style="Section.Card.TLabel")
        self.setup_title.grid(row=0, column=0, sticky="w", pady=(0, px(10)))

        body = self.setup_body = ttk.Frame(c, style="Card.TFrame")
        body.grid(row=1, column=0, sticky="ew")
        body.columnconfigure(0, minsize=px(150))
        body.columnconfigure(1, weight=1)

        def field(row, label, var, button=None):
            ttk.Label(body, text=label, style="Field.Card.TLabel").grid(row=row, column=0, sticky="w")
            e = ttk.Entry(body, textvariable=var)
            e.grid(row=row, column=1, columnspan=1 if button else 2, sticky="ew", pady=(px(2), 0))
            if button:
                button.grid(row=row, column=2, sticky="ew", padx=(px(8), 0), pady=(px(2), 0))
            return e

        def help_text(row, text):
            lbl = ttk.Label(body, text=text, style="Help.Card.TLabel")
            lbl.grid(row=row, column=1, columnspan=2, sticky="w", pady=(px(4), px(12)))
            return lbl

        self.link_entry = field(0, "Meeting link", self.link)
        self.link_help = help_text(1, LINK_HELP)
        self.name_entry = field(2, "Bot name", self.name)
        help_text(3, "How the bot appears in the meeting. Letters, numbers, spaces and - ' . _ @")
        self.browse_btn = ttk.Button(body, text="Browse\u2026", command=self.browse)
        self.kb_entry = field(4, "Knowledge folder", self.kb, self.browse_btn)
        self.kb_help = help_text(5, KB_HELP)
        self.link.trace_add("write", lambda *a: self.clear_error(self.link_help, self.link_entry, LINK_HELP))
        self.kb.trace_add("write", lambda *a: self.clear_error(self.kb_help, self.kb_entry, KB_HELP))

        ttk.Label(body, text="Speak answers", style="Field.Card.TLabel").grid(row=6, column=0, sticky="w")
        seg = tk.Frame(body, bg=BORDER_STRONG)       # shows through 1px gaps: outline + dividers
        seg.grid(row=6, column=1, sticky="w")
        self.voice_buttons = []
        for i, (text, value) in enumerate(VOICE_SEGMENTS):
            b = ttk.Radiobutton(seg, text=text, value=value, variable=self.voice,
                                style="Segment.TRadiobutton", command=self.voice_changed)
            b.grid(row=0, column=i, padx=(1, 1 if i == len(VOICE_SEGMENTS) - 1 else 0), pady=1)
            self.voice_buttons.append(b)
        self.voice_help = help_text(7, "")
        self.voice_changed()

        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(px(4), 0))
        actions.columnconfigure(2, weight=1)
        self.join_btn = ttk.Button(actions, text="Join meeting", style="Accent.TButton", command=self.start)
        self.join_btn.grid(row=0, column=0)
        self.check_btn = ttk.Button(actions, text="Check setup", command=self.check)
        self.check_btn.grid(row=0, column=1, padx=(px(8), 0))
        self.form_inputs = [self.link_entry, self.name_entry, self.kb_entry,
                            self.browse_btn, *self.voice_buttons]

        # While a meeting runs, the form collapses to this summary and the Leave button.
        self.summary_row = ttk.Frame(c, style="Card.TFrame")
        self.summary_row.grid(row=1, column=0, sticky="ew")
        self.summary_row.columnconfigure(0, weight=1)
        facts = ttk.Frame(self.summary_row, style="Card.TFrame")
        facts.grid(row=0, column=0, sticky="w")
        self.summary_values = {}
        for i, key in enumerate(("Meeting", "Bot", "Knowledge", "Voice")):
            pad = (0 if i == 0 else px(28), 0)
            ttk.Label(facts, text=key, style="Help.Card.TLabel").grid(row=0, column=i, sticky="w", padx=pad)
            v = ttk.Label(facts, text="", style="Value.Card.TLabel")
            v.grid(row=1, column=i, sticky="w", padx=pad)
            self.summary_values[key] = v
        self.leave_btn = ttk.Button(self.summary_row, text="Leave meeting", style="Danger.TButton",
                                    command=self.confirm_leave, state="disabled")
        self.leave_btn.grid(row=0, column=1, sticky="e")
        self.summary_row.grid_remove()

    def _build_feed(self, c):
        px, f = self.px, self.f
        c.columnconfigure(0, weight=1)
        c.rowconfigure(1, weight=1)
        head = ttk.Frame(c, style="Card.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(8)))
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="Activity", style="Section.Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(head, text="Show technical details", variable=self.show_details,
                        style="Card.TCheckbutton", command=self.details_changed).grid(
            row=0, column=1, padx=(0, px(16)))
        ttk.Button(head, text="Open meeting files", command=self.open_outdir).grid(row=0, column=2)
        self.log = tk.Text(c, wrap="word", height=10, state="disabled", bg=SURFACE, fg=TEXT,
                           bd=0, highlightthickness=0, padx=px(2), pady=px(2), font=f.body,
                           selectbackground=ACCENT_SOFT, selectforeground=TEXT,
                           inactiveselectbackground=ACCENT_SOFT)
        sb = ttk.Scrollbar(c, command=self.log.yview, style="Idle.Thin.Vertical.TScrollbar")

        def yscroll(first, last):                    # thumb only when there is something to scroll
            idle = float(first) <= 0 and float(last) >= 1
            sb.configure(style="Idle.Thin.Vertical.TScrollbar" if idle else "Thin.Vertical.TScrollbar")
            sb.set(first, last)
        self.log.configure(yscrollcommand=yscroll)
        self.log.grid(row=1, column=0, sticky="nsew")
        sb.grid(row=1, column=1, sticky="ns", padx=(px(6), 0))
        self._feed_tags()
        self._feed_placeholder()

    def _feed_tags(self):
        px, f, tag = self.px, self.f, self.log.tag_configure
        hang = px(2) + f.mono.measure("00:00:00]") + f.body.measure(" ")   # wrap under the text
        line = dict(lmargin1=px(2), lmargin2=hang, spacing1=px(3), spacing3=px(3))
        card = dict(background=ACCENT_SOFT, lmargincolor=ACCENT_SOFT, rmargincolor=ACCENT_SOFT,
                    lmargin1=px(14), lmargin2=px(14), rmargin=px(14))
        tag("speech", **line)
        tag("event", **line, foreground="#3d4550")
        tag("plain", **line)
        tag("app", **line, foreground=ACCENT)
        tag("question", **dict(line, spacing1=px(8)))
        tag("warn", **line, foreground=WARN_FG)
        tag("error", **line, foreground=DANGER)
        tag("pass", **line, foreground=SUCCESS)
        tag("fail", **line, foreground=DANGER, font=f.strong)
        tag("info", **line, foreground=MUTED)
        tag("ready", **line, foreground=SUCCESS, font=f.strong)
        tag("detail", **dict(line, spacing1=px(1), spacing3=px(1)), font=f.small, foreground=SUBTLE,
            elide=not self.show_details.get())
        tag("detail_keep", **dict(line, spacing1=px(1), spacing3=px(1)), font=f.small, foreground=SUBTLE)
        tag("action", background=WARN_BG, lmargincolor=WARN_BG, rmargincolor=WARN_BG,
            foreground=WARN_FG, font=f.strong, lmargin1=px(10), lmargin2=px(10) + hang,
            spacing1=px(8), spacing3=px(8))
        tag("fence_open", font=f.tiny, foreground=SURFACE, spacing1=px(4))
        tag("answer", **card, spacing1=px(10), spacing3=px(2))
        tag("answer_more", **card, spacing1=px(2), spacing3=px(2))
        tag("fence_close", **card, font=f.tiny, foreground=ACCENT_SOFT, spacing3=px(6))
        tag("gap", font=f.tiny, spacing1=px(4))
        tag("empty_title", font=f.section, foreground=TEXT, justify="center", spacing1=px(48))
        tag("empty", foreground=MUTED, justify="center", spacing1=px(6), lmargin1=px(40),
            lmargin2=px(40), rmargin=px(40))
        # Run-level tags (created last, so they win over the line styles above).
        tag("ts", font=f.mono, foreground=SUBTLE)
        tag("tsgap", font=f.mono, foreground=SURFACE)          # invisible "]" = a gap
        tag("tsgap_warn", font=f.mono, foreground=WARN_BG)
        tag("hide", elide=True)
        tag("speaker", font=f.strong, foreground=TEXT)
        tag("qhead", font=f.strong, foreground=ACCENT)
        tag("eyebrow", font=f.strong_small, foreground=ACCENT)
        tag("source", font=f.small, foreground=MUTED)

    def _feed_placeholder(self):
        self.log.configure(state="normal")
        self.log.insert("end", "Nothing here yet\n", ("empty_title",))
        self.log.insert("end", "Paste the meeting link and choose Join meeting. On a PC that hasn't "
                               "run the bot before, choose Check setup first.\nIn the meeting, anyone "
                               "can say \u201cHey Claude, \u2026\u201d. The answer appears in the meeting "
                               "chat and here.\n", ("empty",))
        self.log.configure(state="disabled")
        self.feed_empty = True

    def _build_composer(self, c):
        px = self.px
        c.columnconfigure(0, weight=1)
        ttk.Label(c, text="Ask Claude", style="Section.Card.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, px(8)))
        self.ask_entry = ttk.Entry(c, textvariable=self.ask_text, state="disabled")
        self.ask_entry.grid(row=1, column=0, sticky="ew")
        self.ask_entry.bind("<Return>", lambda e: self.send_private())
        self.ask_btn = ttk.Button(c, text="Ask privately", style="Accent.TButton",
                                  command=self.send_private, state="disabled")
        self.ask_btn.grid(row=1, column=1, padx=(px(8), 0))
        self.say_btn = ttk.Button(c, text="Say out loud", command=self.send_speak, state="disabled")
        self.say_btn.grid(row=1, column=2, padx=(px(8), 0))
        self.ask_help = ttk.Label(c, text=ASK_HELP_IDLE, style="Help.Card.TLabel")
        self.ask_help.grid(row=2, column=0, columnspan=3, sticky="w", pady=(px(6), 0))

    def _build_view(self, v):
        px = self.px
        v.columnconfigure(0, weight=1)
        v.rowconfigure(1, weight=1)
        self.view_hint = ttk.Label(v, style="Hint.TLabel", text=VIEW_HINT)
        self.view_hint.grid(row=0, column=0, sticky="w", pady=(0, px(10)))
        frame = tk.Frame(v, bg=VIEW_BG, highlightthickness=1, highlightbackground=BORDER)
        frame.grid(row=1, column=0, sticky="nsew")
        self.canvas = tk.Canvas(frame, bg=VIEW_BG, highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.view_click)
        self.canvas.bind("<MouseWheel>", self.view_wheel)                    # Windows
        self.canvas.bind("<Button-4>", lambda e: self.view_wheel(e, -120))   # X11
        self.canvas.bind("<Button-5>", lambda e: self.view_wheel(e, 120))
        self.canvas.bind("<Configure>", lambda e: self.redraw_frame(force=True))
        bar = self.card(v, 2, pady=(px(12), 0), pad=(16, 10))
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Type into the meeting", style="Field.Card.TLabel").grid(
            row=0, column=0, padx=(0, px(12)))
        self.view_entry = ttk.Entry(bar, textvariable=self.type_text, state="disabled")
        self.view_entry.grid(row=0, column=1, sticky="ew")
        self.view_entry.bind("<Return>", lambda ev: self.view_type())
        self.view_buttons = [ttk.Button(bar, text="Type", style="Key.TButton", command=self.view_type)]
        for label, key in (("Enter", "Enter"), ("Esc", "Escape"), ("Tab", "Tab"),
                           ("Backspace", "Backspace")):
            self.view_buttons.append(ttk.Button(bar, text=label, style="Key.TButton",
                                                command=lambda k=key: self.view_key(k)))
        for i, b in enumerate(self.view_buttons):
            b.grid(row=0, column=i + 2, padx=(px(8) if i < 2 else px(4), 0))
            b.configure(state="disabled")
        if Image is None:
            self.view_hint.configure(text="Meeting view needs Pillow: .venv\\Scripts\\python -m pip "
                                          "install pillow")

    def sync_tabs(self):
        i = self.tabs.index(self.tabs.select())
        self.tab_choice.set(i)
        for j, mark in enumerate(self.tab_marks):
            mark.configure(bg=ACCENT if j == i else SURFACE)
        self.refresh_banner()

    def frame_file(self):
        return os.path.join(self.outdir, "live_view.jpg") if self.outdir else None

    def draw_view_placeholder(self):
        c, px = self.canvas, self.px
        c.delete("all")
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        c.create_text(w / 2, h / 2 - px(12), text="The meeting appears here once the bot joins",
                      fill="#d6dae0", font=self.f.section)
        c.create_text(w / 2, h / 2 + px(14), text="Click in the picture to click in the meeting",
                      fill="#9aa3ae", font=self.f.small)
        self.view_placeholder = True

    def redraw_frame(self, force=False):
        path = self.frame_file()
        if Image is None or not path or not os.path.exists(path):
            if force or not self.view_placeholder:
                self.draw_view_placeholder()
            return
        try:
            mt = os.path.getmtime(path)
            if mt == self.frame_mtime and not force:
                return
            img = Image.open(path)
            img.load()
        except Exception:
            return                                   # mid-write; next tick
        self.frame_mtime = mt
        self.view_placeholder = False
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        self.layout = fit_layout(img.width, img.height, cw, ch)
        scale, ox, oy, _, _ = self.layout
        shown = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                           Image.BILINEAR)
        self.photo = ImageTk.PhotoImage(shown)
        self.canvas.delete("all")
        self.canvas.create_image(ox, oy, anchor="nw", image=self.photo)

    def view_click(self, e):
        pt = canvas_to_page(e.x, e.y, self.layout)
        if pt and self.send(f"/click {pt[0]} {pt[1]}"):
            r = self.px(7)
            self.canvas.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline="#ffb300",
                                    width=self.px(2))

    def view_wheel(self, e, delta=None):
        pt = canvas_to_page(e.x, e.y, self.layout)
        if pt:
            dy = delta if delta is not None else -int(e.delta)
            self.send(f"/scroll {pt[0]} {pt[1]} {dy}")

    def view_type(self):
        t = self.type_text.get()
        if t and self.send(f"/type {t}"):
            self.type_text.set("")

    def view_key(self, key):
        self.send(f"/key {key}")

    # ---------------------------------------------------------------- actions
    def browse(self):
        d = filedialog.askdirectory(title="Folder with the meeting's materials",
                                    initialdir=self.kb.get() or HERE)
        if d:
            self.kb.set(os.path.normpath(d))

    def voice_changed(self):
        self.voice_help.configure(text=VOICE_HELP[VOICE_CHOICES.get(self.voice.get(), "asked")])

    def details_changed(self):
        at_end = self.log.yview()[1] >= 0.999
        self.log.tag_configure("detail", elide=not self.show_details.get())
        if at_end:                                   # stay with the newest lines
            self.log.see("end")
        self.remember(show_details=self.show_details.get())

    def field_error(self, help_label, entry, message):
        help_label.configure(text=message, style="Error.Card.TLabel")
        entry.configure(style="Invalid.TEntry")
        entry.focus_set()

    def clear_error(self, help_label, entry, text):
        if str(entry.cget("style")) == "Invalid.TEntry":
            help_label.configure(text=text, style="Help.Card.TLabel")
            entry.configure(style="TEntry")

    def validate(self):
        link = self.link.get().strip()
        if not TEAMS_LINK.match(link):
            self.field_error(self.link_help, self.link_entry, "That isn't a Teams meeting link. "
                             "Paste the whole link - it starts with https://teams.microsoft.com/")
            return None
        name = clean_name(self.name.get()) or DEFAULT_NAME
        if name != self.name.get().strip():
            self.name.set(name)
            self.append(f"Bot name adjusted to \"{name}\" (Teams allows letters, numbers, "
                        f"spaces and - ' . _ @ only).", "app")
        kb = self.kb.get().strip()
        if kb and not os.path.isdir(kb):
            self.field_error(self.kb_help, self.kb_entry, f"Folder not found: {kb}")
            return None
        return link, name, kb, VOICE_CHOICES.get(self.voice.get(), "asked")

    def env(self):
        env = dict(os.environ)
        env.update(load_env_file(os.path.join(HERE, "foundry.env.ps1")))
        dep = env.get("CLAUDE_FOUNDRY_DEPLOYMENT")
        env["PYTHONIOENCODING"] = "utf-8"
        return env, dep

    def launch(self, cmd, mode="meeting"):
        env, dep = self.env()
        if dep and "--model" not in cmd:
            cmd += ["--model", dep]
        flags = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     bufsize=1, creationflags=flags)
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()
        self.set_running(True, mode)

    def remember(self, **values):
        s = load_settings()
        s.update(values)
        save_settings(s)

    def start(self):
        if self.proc:
            return
        v = self.validate()
        if not v:
            return
        link, name, kb, voice = v
        self.remember(link=link, name=name, kb=kb, voice=self.voice.get())
        self.outdir = os.path.join(HERE, "meetings", dt.datetime.now().strftime("%Y-%m-%d_%H%M"))
        self.append(f"Starting: {name} -> meeting; knowledge folder: {kb or '(none)'}; "
                    f"voice: {voice}", "app")
        self.frame_mtime, self.layout = 0, None
        self.draw_view_placeholder()
        self.launch(build_command(link, name, kb, voice, self.outdir, self.python))

    def check(self):
        if self.proc:
            return
        kb = self.kb.get().strip()
        cmd = [self.python or bot_python(), "-u", BOT, "--check",
               "--voice", VOICE_CHOICES.get(self.voice.get(), "asked")]
        if kb and os.path.isdir(kb):
            cmd += ["--project", kb]
        self.append("Checking setup (browser, model, knowledge folder, MCP, voice)...", "app")
        self.launch(cmd, mode="check")

    def send(self, line):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
                return True
            except Exception:
                pass
        return False

    def send_private(self):
        q = self.ask_text.get().strip()
        if q and self.send(q):
            self.append(f"You (private): {q}", "app")
            self.ask_text.set("")

    def send_speak(self):
        t = self.ask_text.get().strip()
        if t and self.send(f"/speak {t}"):
            self.append(f"Saying out loud: {t}", "app")
            self.ask_text.set("")

    def confirm_leave(self):
        if messagebox.askyesno(APP_NAME, "Leave the meeting now?\n\nClaude posts a short wrap-up in "
                               "the meeting chat, then saves the transcript and summary.",
                               parent=self.root):
            self.leave()

    def leave(self):
        if self.send("/leave"):
            self.append("Leaving - posting the wrap-up and writing the summary...", "app")
            self.leave_btn.configure(state="disabled")
            self.set_status("Leaving – writing the summary…", "busy")
            self.root.after(90000, self.force_stop)

    def force_stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.append("Bot did not exit in time - stopped it.", "warn")

    def open_outdir(self):
        d = self.outdir if self.outdir and os.path.isdir(self.outdir) else os.path.join(HERE, "meetings")
        os.makedirs(d, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(d)
        else:
            self.append(f"Meeting files: {d}", "app")

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(APP_NAME, "The bot is still in the meeting. Leave the "
                                                 "meeting and close?"):
                return
            self.send("/leave")
            try:
                self.proc.wait(timeout=60)
            except Exception:
                self.proc.terminate()
        self.root.destroy()

    # ---------------------------------------------------------------- output
    def reader(self, proc):
        for line in proc.stdout:
            self.lines.put(line.rstrip("\n"))
        proc.wait()
        self.lines.put(None)

    def pump(self):
        try:
            while True:
                line = self.lines.get_nowait()
                if line is None:
                    self.process_ended()
                else:
                    self.show_line(line)
        except queue.Empty:
            pass
        self.redraw_frame()
        self.root.after(150, self.pump)

    def show_line(self, line):
        kind, self.feed_state = classify_line(line, self.feed_state)
        if kind is None:
            return
        if kind == "detail" and keep_visible(line):
            kind = "detail_keep"
        self._insert(feed_segments(line, kind), kind)
        if kind in PEOPLE:
            return
        if kind in DETAILS:
            if "thanks - continuing" in line:        # the step that needed a hand is done
                self.hide_banner()
            return
        if kind == "action":
            what, where = action_text(line)
            if "Meeting view" in where:
                self.tabs.select(self.view_tab)      # first, so the banner words fit the page
            self.show_banner(what, where)
        s = line.strip()
        if s == "READY":
            self.set_status("Setup looks good – ready to join", "live")
        elif s.startswith("NOT READY"):
            self.set_status("Setup needs attention – see Activity", "error")
        for key, text, tone in STATUS_RULES:
            if key in line:
                self.set_status(text, tone)
                if key != "ACTION NEEDED":
                    self.hide_banner()
                break

    def process_ended(self):
        code = self.proc.returncode if self.proc else 0
        status = self.status.get()
        if self.mode == "check":
            self.append(f"Setup check finished (exit code {code}).", "app" if code == 0 else "warn")
            if status.startswith("Checking"):
                self.set_status("Setup check stopped – see Activity", "error")
        else:
            self.append(f"Bot stopped (exit code {code}).", "app" if code == 0 else "warn")
            if not status.startswith("Finished"):
                self.set_status(*(("Stopped – see Activity", "error") if code else ("Stopped", "done")))
        self.proc = None
        self.hide_banner()
        self.set_running(False)

    def append(self, text, tag=None):
        self._insert([(text, ())], tag or "plain")

    def _insert(self, segs, line_tag):
        log = self.log
        at_end = log.yview()[1] >= 0.999             # only follow new lines if already at the end
        log.configure(state="normal")
        if self.feed_empty:
            log.delete("1.0", "end")
            self.feed_empty = False
        for text, tags in segs:
            log.insert("end", text, (line_tag, *tags))
        log.insert("end", "\n", (line_tag,))
        log.configure(state="disabled")
        if at_end:
            log.see("end")

    def show_banner(self, what, where):
        self.banner_info = (what, where)
        self.refresh_banner()
        self.banner.grid()

    def refresh_banner(self):
        """Say where to act relative to the page being looked at; offer the jump if needed."""
        if not self.banner_info:
            return
        what, where = self.banner_info
        in_view = "Meeting view" in where
        on_view = self.tabs.index(self.tabs.select()) == 1
        if in_view:
            text = f"{what} in the meeting picture below." if on_view else f"{what} in the Meeting view."
        else:
            text = f"{what} in {where}." if where else f"{what}."
        self.banner_text.configure(text=text)
        if in_view and not on_view:
            self.banner_btn.pack(side="right", before=self.banner_text)
        else:
            self.banner_btn.pack_forget()

    def hide_banner(self):
        self.banner_info = None
        self.banner.grid_remove()

    def set_status(self, text, tone="idle"):
        self.status.set(text)
        self.pill.set(text, tone)

    def update_summary(self):
        link = re.sub(r"^https?://", "", self.link.get().strip())
        voice = next((t for t, v in VOICE_SEGMENTS if v == self.voice.get()), "When asked")
        values = {"Meeting": link if len(link) <= 36 else link[:35] + "\u2026",
                  "Bot": clean_name(self.name.get()) or DEFAULT_NAME,
                  "Knowledge": os.path.basename(os.path.normpath(self.kb.get().strip())) or "(none)",
                  "Voice": voice}
        for key, label in self.summary_values.items():
            label.configure(text=values[key])

    def set_running(self, running, mode="meeting"):
        self.mode = mode if running else None
        meeting = running and mode == "meeting"
        if meeting:
            self.update_summary()
            self.setup_body.grid_remove()
            self.summary_row.grid()
        else:
            self.summary_row.grid_remove()
            self.setup_body.grid()
        self.setup_title.configure(text="This meeting" if meeting else "Meeting setup")
        for w in self.form_inputs:
            w.configure(state="disabled" if running else "normal")
        self.join_btn.configure(state="disabled" if running else "normal")
        self.check_btn.configure(state="disabled" if running else "normal")
        self.leave_btn.configure(state="normal" if meeting else "disabled")
        for w in (self.ask_entry, self.ask_btn, self.say_btn, self.view_entry, *self.view_buttons):
            w.configure(state="normal" if meeting else "disabled")
        self.ask_help.configure(text=ASK_HELP_LIVE if meeting else ASK_HELP_IDLE)
        if running:
            self.set_status("Checking setup…" if mode == "check" else "Starting…", "busy")


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

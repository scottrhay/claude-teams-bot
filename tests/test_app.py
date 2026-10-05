"""Desktop app tests. Unit tests run wherever tkinter exists; the `gui` ones drive the real
window, hidden, with no bot process. The GUI end-to-end test (real window + real bot + mock
meeting + real Claude) runs when RUN_GUI_TESTS=1."""
import os
import sys
import time

import pytest

tk = pytest.importorskip("tkinter")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def new_root():
    """tk.Tk() started the way pythonw starts the app: with no stdin/stdout/stderr handles.
    Otherwise Tcl wraps pytest's per-test capture files in its std channels, once per thread,
    and keeps them after pytest closes the files. When Windows reuses one of those handle
    values for a file Tcl opens later, Tcl takes it for its old write-only stdout and the read
    fails at random ("Can't find a usable tk.tcl", "couldn't read file ... init.tcl: No error")."""
    if sys.platform != "win32":
        return tk.Tk()
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32")                  # own copy: argtypes stay local
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    ids = [n & 0xFFFFFFFF for n in (-10, -11, -12)]  # STD_INPUT / OUTPUT / ERROR_HANDLE
    saved = [k32.GetStdHandle(n) for n in ids]
    for n in ids:
        k32.SetStdHandle(n, None)
    try:
        return tk.Tk()
    finally:
        for n, h in zip(ids, saved):
            k32.SetStdHandle(n, h)


def test_env_file_parsing(tmp_path):
    f = tmp_path / "foundry.env.ps1"
    f.write_text('\ufeff# comment\n$env:CLAUDE_CODE_USE_FOUNDRY    = "1"\n'
                 "$env:ANTHROPIC_FOUNDRY_RESOURCE = 'res-1'   # the <name> in https://<name>.x\n"
                 '$env:CLAUDE_FOUNDRY_DEPLOYMENT    = "claude-opus-5-5"                # your DEPLOYMENT\n'
                 "  $env:X=\"a b\"\nnot a line\n",
                 encoding="utf-8")
    assert app.load_env_file(str(f)) == {"CLAUDE_CODE_USE_FOUNDRY": "1",
                                         "ANTHROPIC_FOUNDRY_RESOURCE": "res-1",
                                         "CLAUDE_FOUNDRY_DEPLOYMENT": "claude-opus-5-5", "X": "a b"}
    assert app.load_env_file(str(tmp_path / "missing.ps1")) == {}


def test_link_name_and_command():
    assert app.TEAMS_LINK.match("https://teams.microsoft.com/meet/210136600936824?p=abc")
    assert not app.TEAMS_LINK.match("teams.microsoft.com/meet/1")
    assert app.clean_name("Claude (Meeting Assistant)") == "Claude Meeting Assistant"
    cmd = app.build_command("https://teams.microsoft.com/meet/1?p=x", "Bot", r"C:\kb", "asked",
                            r"C:\out", python="py")
    assert cmd[:3] == ["py", "-u", app.BOT]
    assert cmd[cmd.index("--project") + 1] == r"C:\kb" and cmd[cmd.index("--voice") + 1] == "asked"
    assert "--project" not in app.build_command("u", "b", "", "off", "o", python="py")


@pytest.mark.skipif(os.environ.get("RUN_GUI_TESTS") != "1", reason="set RUN_GUI_TESTS=1")
def test_gui_end_to_end(tmp_path):
    bot_python = os.environ.get("BOT_PYTHON") or sys.executable
    root = new_root()
    a = app.App(root, python=bot_python)
    url = "file://" + os.path.join(HERE, "mock_teams.html")
    result = {"done": False, "steps": []}
    t0 = time.monotonic()

    def log_text():
        return a.log.get("1.0", "end")

    def step():
        txt, el = log_text(), time.monotonic() - t0
        st = result["steps"]
        if "started" not in st:
            a.outdir = str(tmp_path)
            a.launch(app.build_command(url, "Claude Meeting Assistant", app.DEFAULT_KB, "off",
                                       str(tmp_path), bot_python) + ["--guest", "--headless",
                                                                     "--frame-interval", "0.5"])
            st.append("started")
        elif "listening" not in st and "listening" in a.status.get().lower():
            st.append("listening")
            a.ask_text.set("In one short sentence, who has spoken so far?")
            a.send_private()
        elif "listening" in st and "answered" not in st and "CLAUDE:" in txt:
            st.append("answered")
        elif "answered" in st and "viewed" not in st and a.layout:
            st.append("viewed")                         # a live frame is on the Meeting view
            a.tabs.select(a.view_tab)
            root.update()
            scale, ox, oy, w, h = a.layout
            a.view_click(type("E", (), {"x": ox + int(20 * scale), "y": oy + int(20 * scale)})())
        elif "viewed" in st and "left" not in st and ("Meeting ended" in txt or el > 60):
            st.append("left")
            a.leave()
        elif a.proc is None and "started" in st and "left" in st:
            result["done"] = True
            root.quit()
            return
        if el > 170:
            root.quit()
            return
        root.after(500, step)

    root.after(300, step)
    root.mainloop()
    txt = log_text()
    status = a.status.get()
    root.destroy()
    assert "listening" in result["steps"], txt[-2000:]
    assert "answered" in result["steps"], txt[-2000:]
    assert "viewed" in result["steps"], txt[-2000:]
    import re
    assert re.search(r"operator click at (19|20|21),(19|20|21)", txt), txt[-2000:]
    assert result["done"] and status.startswith("Finished"), (status, txt[-1500:])
    assert any(n.startswith("meeting_summary_") for n in os.listdir(tmp_path))


def test_view_geometry_maps_clicks_to_page():
    lay = app.fit_layout(1366, 860, 683, 600)          # half size, centred vertically
    assert abs(lay[0] - 0.5) < 1e-9 and lay[1] == 0 and lay[2] == (600 - 430) // 2
    assert app.canvas_to_page(0, lay[2], lay) == (0, 0)
    assert app.canvas_to_page(683 - 1, lay[2] + 430 - 1, lay) == (1364, 858)
    assert app.canvas_to_page(10, 5, lay) is None                    # letterbox area
    assert app.canvas_to_page(10, 10, None) is None
    cmd = app.build_command("u", "b", "", "off", "o", python="py")
    assert cmd[cmd.index("--browser") + 1] == "embedded"
    assert app.build_command("u", "b", "", "off", "o", python="py", browser="window")[-1] == "window"


# ---------------------------------------------------------------- activity feed
BOT_OUTPUT = [                                         # (line as the bot prints it, feed kind)
    ("[13:52:31] Opening meeting link...", "event"),
    ("[13:52:39]   name: Claude Meeting Assistant", "detail"),
    ("[13:52:55] ACTION NEEDED: click 'Join now' (in the Meeting view tab). Waiting...", "action"),
    ("[13:53:02]   thanks - continuing", "detail"),
    (r"[13:53:13] Agent: instructions.md, 2 file(s) in C:\kb, MCP: microsoft-learn", "event"),
    ("[13:53:21] Dana Ruiz: We need to cut the Q4 budget by ten percent.", "speech"),
    ("[13:53:22] Mark Chen: ACTION NEEDED: a CRASH course. Listening.", "speech"),
    ("[13:53:46] Q (Dana Ruiz): what did Mark commit to?", "question"),
    ('[13:53:48]   agent tool: Grep {"pattern": "Meeting ended"}', "detail"),
    ("[13:53:49]   WARNING: voice skipped (no speech key)", "warn"),
    ("", None),
    ("=" * 70, "fence_open"),
    ("CLAUDE: Mark owns the renegotiation. (source: transcript)", "answer"),
    ("It is due October 15.", "answer_more"),
    ("=" * 70, "fence_close"),
    ("", "gap"),
    ("", None),
    ("[13:54:10] Claude API error: overloaded", "error"),
    ("[13:59:00] CRASH:", "error"),
    ("Traceback (most recent call last):", "error"),
    ("ValueError: bad frame", "error"),
    ("  [PASS] Browser", "pass"),
    ("  [FAIL] Model - 401", "fail"),
    ("NOT READY - fix the FAIL lines above", "fail"),
    ("READY", "ready"),
    ("Preflight:", "plain"),
]


def test_feed_classifies_bot_output():
    state = ""
    for line, want in BOT_OUTPUT:
        kind, state = app.classify_line(line, state)
        assert kind == want, line


def test_feed_segments_keep_each_line_verbatim():
    state = ""
    for line, _ in BOT_OUTPUT:
        kind, state = app.classify_line(line, state)
        if kind:
            assert "".join(text for text, _ in app.feed_segments(line, kind)) == line
    speech = app.feed_segments("[13:53:21] Dana Ruiz: We need a plan.", "speech")
    assert speech == [("[", ("hide",)), ("13:53:21", ("ts",)), ("]", ("tsgap",)),
                      (" Dana Ruiz:", ("speaker",)), (" We need a plan.", ())]
    assert app.feed_segments("CLAUDE: Mark owns it. (source: transcript)", "answer") == [
        ("CLAUDE:", ("eyebrow",)), (" Mark owns it. ", ()), ("(source: transcript)", ("source",))]
    assert app.action_text(BOT_OUTPUT[2][0]) == ("Click 'Join now'", "the Meeting view tab")
    assert app.action_text("[13:52:55] ACTION NEEDED: sign in again.") == ("Sign in again", "")


def test_knowledge_folder_messages_show_in_activity(tmp_path, make_pdf, capsys):
    """The bot's own messages while it prepares the knowledge folder, as Activity shows them:
    start and finish always visible, per-file progress under technical details, problems visible."""
    import teams_meeting_bot as tb
    make_pdf(tmp_path / "budget.pdf", "Plant 3 network refresh")
    make_pdf(tmp_path / "scan.pdf", None)
    tb.prepare_materials(str(tmp_path))
    kinds = [app.classify_line(line)[0] for line in capsys.readouterr().out.splitlines()]
    assert kinds == ["event", "detail", "detail", "warn", "event"]


@pytest.fixture
def gui(tmp_path, monkeypatch):
    """The real window, hidden, with settings in a temp file. No bot process is started."""
    monkeypatch.setattr(app, "SETTINGS", str(tmp_path / "settings.json"))
    try:
        root = new_root()
    except tk.TclError as e:                          # e.g. Linux CI without $DISPLAY
        if sys.platform == "win32":
            raise
        pytest.skip(f"no display: {e}")
    root.withdraw()
    yield app.App(root, python="python")
    root.destroy()


def test_feed_drives_status_and_banner_but_people_do_not(gui):
    a, fed = gui, []

    def show(*lines):
        for line in lines:
            fed.append(line)
            a.show_line(line)

    a.set_running(True)
    assert a.status.get() == "Starting…"
    show("[13:52:31] Opening meeting link...", BOT_OUTPUT[2][0])
    assert a.status.get() == "Needs you" and a.banner.grid_info()
    assert a.tabs.index(a.tabs.select()) == 1                        # jumped to the Meeting view
    assert a.banner_text.cget("text") == "Click 'Join now' in the meeting picture below."
    a.tabs.select(0)
    a.root.update()
    assert a.banner_text.cget("text") == "Click 'Join now' in the Meeting view."
    # Captions, tool inputs and spoken answers can contain any words; none of them is a bot event.
    show("[13:52:58] Mark Chen: CRASH. Listening. Transcript: Meeting ended",
         '[13:52:59]   agent tool: Grep {"pattern": "Meeting ended|Transcript:"}',
         "[13:53:00]   spoke 1.2s: Transcript: we are Listening.")
    assert a.status.get() == "Needs you" and a.banner_info
    show("[13:53:02]   thanks - continuing")
    assert a.banner_info is None and not a.banner.grid_info()
    show(r"[13:53:13] Listening. Transcript -> C:\m\t.md")
    assert a.status.get() == "In the meeting – listening for “Hey Claude”"
    show("", "=" * 70, "CLAUDE: Mark owns it. (source: transcript)", "=" * 70, "", "",
         "[13:59:00] Meeting ended or bot removed.", r"[13:59:05] Transcript: C:\m\t.md")
    assert a.status.get() == "Finished – transcript and summary saved"

    state, expected = "", ""                          # styling never changes the text itself
    for line in fed:
        kind, state = app.classify_line(line, state)
        expected += line + "\n" if kind else ""
    assert a.log.get("1.0", "end-1c") == expected

    assert a.log.tk.getboolean(a.log.tag_cget("detail", "elide"))    # details hidden by default
    a.show_details.set(True)
    a.details_changed()
    assert not a.log.tk.getboolean(a.log.tag_cget("detail", "elide"))
    assert app.load_settings()["show_details"] is True


def test_setup_form_validates_inline_and_collapses_while_running(gui, tmp_path):
    a = gui
    link = "https://teams.microsoft.com/meet/2401987654321?p=abc"
    a.link.set("teams meeting tomorrow")
    assert a.validate() is None                                      # inline error, no dialog
    assert str(a.link_entry.cget("style")) == "Invalid.TEntry"
    assert a.link_help.cget("text").startswith("That isn't a Teams meeting link")
    a.link.set(link)                                                 # typing clears the error
    assert str(a.link_entry.cget("style")) == "TEntry" and a.link_help.cget("text") == app.LINK_HELP
    a.kb.set(str(tmp_path / "missing"))
    assert a.validate() is None and str(a.kb_entry.cget("style")) == "Invalid.TEntry"
    a.kb.set(str(tmp_path))
    assert a.validate() == (link, app.DEFAULT_NAME, str(tmp_path), "asked")

    a.set_running(True)                                              # a meeting: summary + Leave
    assert a.summary_row.grid_info() and not a.setup_body.grid_info()
    assert a.summary_values["Meeting"].cget("text") == "teams.microsoft.com/meet/2401987654…"
    assert str(a.leave_btn.cget("state")) == "normal" and str(a.ask_entry.cget("state")) == "normal"
    a.set_running(False)
    a.set_running(True, mode="check")                                # a setup check keeps the form
    assert a.setup_body.grid_info() and str(a.leave_btn.cget("state")) == "disabled"
    assert a.status.get() == "Checking setup…"
    a.show_line("NOT READY - fix the FAIL lines above")
    a.process_ended()
    assert a.status.get() == "Setup needs attention – see Activity" and a.mode is None

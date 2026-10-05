"""The agent's lock-down, tested in the real engine: the claude.exe bundled with the pinned Agent
SDK runs against a local fake model server. No model calls, no network.

The fake model plays a manipulated model: it asks for files outside the knowledge folder, and
the tests check what the engine let through. Each test starts claude.exe (a few seconds).
Skipped when the bundled engine is missing (run setup.ps1).
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
import teams_meeting_bot as tb  # noqa: E402

pytestmark = pytest.mark.skipif(not tb.bundled_engine(),
                                reason="no bundled Claude Code engine (run setup.ps1)")

MARK = "Zebra-Quartz-7316"          # only in the meeting transcript the bot sends
SECRET = "Sapphire-Lantern-8842"    # only in a file outside the knowledge folder
INSIDE = "Q4 capex is 2.4M."        # in the knowledge folder


def tool_use(n, name, **inp):
    return {"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": inp}


def tool_names(req):
    return {t.get("name") for t in req.get("tools") or []}


def tool_results(req):
    """Every tool result in the conversation so far (the engine may add messages after them)."""
    return [c for m in req.get("messages") or [] if isinstance(m.get("content"), list)
            for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result"]


def result_text(r):
    c = r.get("content")
    return " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(c or "")


class FakeModel(ThreadingHTTPServer):
    """Just enough of the Messages API for the engine. script(request) returns the reply's
    content blocks; every request the engine sends is kept in .requests."""
    daemon_threads = True

    def __init__(self, script, input_tokens=100):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script, self.requests, self.input_tokens = script, [], input_tokens
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(b"{}")

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if "count_tokens" in self.path:
            return self._send(b'{"input_tokens": 100}')
        self.server.requests.append(req)
        blocks = self.server.script(req)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        usage = {"input_tokens": self.server.input_tokens, "output_tokens": 10}
        message = {"id": "msg_fake", "type": "message", "role": "assistant", "model": req.get("model"),
                   "content": blocks, "stop_reason": stop, "stop_sequence": None, "usage": usage}
        if not req.get("stream"):
            return self._send(json.dumps(message).encode())
        events = [("message_start", {"type": "message_start",
                                     "message": {**message, "content": [], "stop_reason": None}})]
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": b["text"]}
            else:
                start = {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}
            events += [("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}),
                       ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
                       ("content_block_stop", {"type": "content_block_stop", "index": i})]
        events += [("message_delta", {"type": "message_delta", "usage": {"output_tokens": 10},
                                      "delta": {"stop_reason": stop, "stop_sequence": None}}),
                   ("message_stop", {"type": "message_stop"})]
        self._send("".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode(),
                   "text/event-stream")


@pytest.fixture
def meeting(tmp_path, monkeypatch):
    """A knowledge folder, a secret file next to it, and an empty engine home (the engine's
    ~/.claude), with the engine pointed at the fake model. Returns run(script, question)."""
    agent_dir = tmp_path / "agent"
    (agent_dir / "project").mkdir(parents=True)
    (agent_dir / "instructions.md").write_text("You are the meeting assistant.", encoding="utf-8")
    (agent_dir / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    (agent_dir / "project" / "budget.md").write_text(INSIDE, encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret.txt").write_text(SECRET, encoding="utf-8")
    (tmp_path / "engine-home").mkdir()
    for k in [k for k in os.environ if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-the-local-model")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "engine-home"))

    def run(script, question="what is the Q4 capex?", max_turns=12, budget=2.0, input_tokens=100,
            model_name="claude-sonnet-5-5"):
        model = FakeModel(script, input_tokens)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", model.url)
        agent = run.agent = tb.MeetingAgent(str(agent_dir), model_name, max_turns, budget, 90)
        try:
            return model, agent, asyncio.run(agent.ask(f"[10:00:00] Dana Ruiz: {MARK}", "Dana Ruiz", question))
        finally:
            model.shutdown()
    run.secret_file = tmp_path / "elsewhere" / "secret.txt"
    run.inside_file = agent_dir / "project" / "budget.md"
    run.home = tmp_path / "engine-home"
    return run


def bot_question(req):
    return MARK in json.dumps(req.get("messages")) and "Read" in tool_names(req)


def settled_files(home, quiet=2.0, limit=10.0):
    """The engine writes its session file as it exits, just after the SDK returns: wait until
    its home folder stops changing, then list the files in it."""
    def snapshot():
        return {(p, p.stat().st_size) for p in home.rglob("*") if p.is_file()}
    last, changed, deadline = snapshot(), time.monotonic(), time.monotonic() + limit
    while time.monotonic() < deadline and time.monotonic() - changed < quiet:
        time.sleep(0.25)
        if (now := snapshot()) != last:
            last, changed = now, time.monotonic()
    return [p for p, _ in last]


def test_engine_offers_only_the_read_only_tools(meeting):
    model, _, _ = meeting(lambda req: [{"type": "text", "text": "ok"}])
    asked = [r for r in model.requests if bot_question(r)]
    assert asked and tool_names(asked[0]) == {"Read", "Grep", "Glob"}


def test_engine_denies_reads_outside_the_knowledge_folder(meeting):
    secret, inside = meeting.secret_file, meeting.inside_file

    def script(req):
        if tool_results(req):
            return [{"type": "text", "text": "Done."}]
        if bot_question(req):                              # what a manipulated model would try
            return [tool_use(1, "Read", file_path=str(secret)),
                    tool_use(2, "Grep", pattern="Sapphire", path=str(secret.parent)),
                    tool_use(3, "Glob", pattern="*.txt", path=str(secret.parent)),
                    tool_use(4, "Read", file_path=str(inside))]
        return [{"type": "text", "text": "ok"}]
    model, _, (answer, tools, _) = meeting(script)
    results = {r["tool_use_id"]: result_text(r) for req in model.requests for r in tool_results(req)}
    assert answer == "Done." and sorted(tools) == ["Glob", "Grep", "Read", "Read"]
    assert INSIDE in results["toolu_4"]                    # the knowledge folder still works
    assert SECRET not in json.dumps(model.requests)        # the secret never reached the model
    assert "secret.txt" not in results["toolu_2"] + results["toolu_3"]   # nor its name


def test_at_mentions_in_meeting_text_are_not_expanded(meeting):
    """Prompts are built from captions and earlier answers; an @path in them stays text."""
    model, _, _ = meeting(lambda req: [{"type": "text", "text": "ok"}],
                          question=f"what does @{meeting.secret_file} say?")
    assert SECRET not in json.dumps(model.requests)


def test_engine_keeps_no_copy_of_the_meeting(meeting):
    meeting(lambda req: [{"type": "text", "text": "ok"}])
    copies = [str(p) for p in settled_files(meeting.home)
              if MARK in p.read_text(encoding="utf-8", errors="ignore")]
    assert copies == []


def test_turn_limit_ends_the_run_with_an_error(meeting):
    """A model that never stops calling tools: the engine stops it, and the bot's error path
    (a transcript-only answer) takes over."""
    def script(req):
        if "Glob" in tool_names(req):
            return [tool_use(len(req["messages"]), "Glob", pattern="*.md")]
        return [{"type": "text", "text": "ok"}]
    with pytest.raises(Exception, match="maximum number of turns"):
        meeting(script, max_turns=3)


def test_spend_limit_ends_the_run_with_an_error_and_is_counted(meeting):
    """A Foundry deployment name is a model the engine has no price for: it must still be
    priced (the engine uses a default rate), or the limit would never trip and the log would
    show no spend."""
    def script(req):
        if "Glob" in tool_names(req):
            return [tool_use(len(req["messages"]), "Glob", pattern="*.md")]
        return [{"type": "text", "text": "ok"}]
    with pytest.raises(Exception, match="maximum budget"):
        meeting(script, budget=0.05, input_tokens=200_000, model_name="my-deployment")
    assert meeting.agent.spent > 0.05          # the stopped run still shows in the meeting's spend


def test_engine_notices_reach_the_log_not_the_meeting_feed(meeting, capsys):
    """For a model name it doesn't know, the engine prints a notice on stderr. It must arrive
    as a log detail, not as a bare line in the app's Activity feed."""
    meeting(lambda req: [{"type": "text", "text": "ok"}], model_name="my-deployment")
    assert "  engine: [claude-code:unrecognized_model]" in capsys.readouterr().out

"""End-to-end tests: real Chromium + mock Teams page; Claude and the agent are faked.
Run with:  python -m pytest -q tests/test_e2e.py"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
import anthropic  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CALLS = []


class _FakeMsgs:
    async def create(self, **kw):
        tail = kw["messages"][0]["content"][1]["text"]
        CALLS.append(tail)

        class B:
            type = "text"
            text = f"DIRECT[{tail[:40]}]"

        class R:
            content = [B()]
        return R()


class _FakeClient:
    def __init__(self, *a, **k):
        self.messages = _FakeMsgs()


anthropic.AsyncAnthropic = _FakeClient
import teams_meeting_bot as tb  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


def sine_wav(seconds=0.8, hz=440):
    import io, math, struct, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / 24000)))
                               for i in range(int(24000 * seconds))))
    return buf.getvalue()


def run_meeting(tmp_path, agent_behavior, extra=(), state=None, during=None):
    """agent_behavior(transcript, asker, question) -> coroutine returning (ans, tools, cost)."""
    CALLS.clear()
    chat, agent_calls, spoken = [], [], []

    class Bot(tb.MeetingBot):
        async def tts(self, text):
            spoken.append(text)
            return sine_wav()

        async def leave(self):
            if state is not None:
                try:
                    state.update(await self.page.evaluate("window.mockState"))
                except Exception:
                    pass
            await super().leave()

        async def post_chat(self, text):
            ok = await super().post_chat(text)
            chat[:] = await self.page.eval_on_selector_all(
                "#chat-log .msg", "els => els.map(e => e.innerText)")
            return ok

    async def main():
        url = "file://" + os.path.join(HERE, "mock_teams.html")
        args = tb.parse_args(["--url", url, "--guest", "--headless", "--end-check", "1",
                              "--ack-after", "1", "--outdir", str(tmp_path), *extra])
        bot = Bot(args)

        async def fake_ask(transcript, asker, question):
            agent_calls.append((asker, question, transcript))
            return await agent_behavior(transcript, asker, question)

        async def fake_complete(transcript, task, timeout=None):
            CALLS.append(task)                 # summaries + fallback answers (no tools)
            return f"DIRECT[{task[:40]}]"
        bot.agent.ask = fake_ask
        bot.agent.complete = fake_complete
        async with async_playwright() as pw:
            side = asyncio.create_task(during(bot)) if during else None
            await asyncio.wait_for(bot.run(pw), timeout=120)
            if side:
                await side
        return bot
    bot = asyncio.run(main())
    bot.spoken = spoken
    return bot, chat, agent_calls


def test_agent_answers_post_to_chat(tmp_path):
    async def agent(transcript, asker, question):
        return f"AGENT answer for {asker} (source: q4_it_budget_brief.md)", ["Grep", "Read"], 0.02
    bot, chat, calls = run_meeting(tmp_path, agent)
    assert [c[:2] for c in calls] == [
        ("Dana Ruiz", "what did Mark commit to and by when?"),
        ("Scott Hay", "Summarize the budget discussion so far.")]
    assert "Mark Chen: I'll own the vendor renegotiation" in calls[0][2]   # transcript passed
    answers = [m for m in chat if m.startswith("Claude: AGENT")]
    assert len(answers) == 2 and "q4_it_budget_brief.md" in answers[0]
    assert not any(m.startswith("Claude: working on") for m in chat)       # fast -> no ack
    assert not [c for c in CALLS if "asks:" in c]                          # no fallback used
    assert os.path.exists(bot.path.replace("transcript", "summary"))


def test_slow_agent_gets_ack_in_chat(tmp_path):
    async def agent(transcript, asker, question):
        await asyncio.sleep(2)
        return "AGENT slow answer", ["mcp__microsoft-learn__microsoft_docs_search"], 0.05
    _, chat, _ = run_meeting(tmp_path, agent)
    assert any(m == "Claude: working on Dana Ruiz's question..." for m in chat)
    assert sum(m == "Claude: AGENT slow answer" for m in chat) == 2


def test_agent_failure_falls_back_to_transcript_answer(tmp_path):
    async def agent(transcript, asker, question):
        raise RuntimeError("MCP server unreachable")
    _, chat, _ = run_meeting(tmp_path, agent)
    assert any("Dana Ruiz asks: what did Mark commit to" in c for c in CALLS)
    assert sum(m.startswith("Claude: DIRECT[") for m in chat) == 2      # meeting still served


def test_voice_answers_are_spoken_into_the_meeting(tmp_path):
    async def agent(transcript, asker, question):
        return "The budget is 2.4 million. (source: q4_it_budget_brief.md)", ["Read"], 0.01
    state = {}
    bot, chat, calls = run_meeting(tmp_path, agent, extra=("--voice", "always"), state=state)
    assert len(calls) == 2                                   # the bot's own caption was ignored
    assert bot.spoken and all("source" not in t for t in bot.spoken)
    assert state["maxLevel"] > 0.02                          # sound reached the meeting mic
    assert state["loudWhileMuted"] is False                  # only while unmuted
    assert state["micLabel"].startswith("Unmute")            # muted again afterwards


def test_live_view_frames_and_operator_click(tmp_path):
    async def agent(transcript, asker, question):
        return "ok", [], 0.0
    seen = {}

    async def during(bot):
        while not bot.listening:                       # wait until in the meeting
            await asyncio.sleep(0.3)
        await asyncio.sleep(1.5)
        with open(bot.frame_path, "rb") as f:
            seen["jpeg"] = f.read(3) == b"\xff\xd8\xff"
        box = await bot.page.locator('[data-tid="more-button"]').bounding_box()
        await bot.handle_command(f"/click {box['x'] + box['width'] / 2} {box['y'] + box['height'] / 2}")
        seen["menu_open"] = await bot.page.locator("#menu").is_visible()
        await bot.handle_command("/key Escape")
        seen["early_cmd_ok"] = True
    run_meeting(tmp_path, agent, extra=("--browser", "embedded", "--frame-interval", "0.5"),
                during=during)
    assert seen == {"jpeg": True, "menu_open": True, "early_cmd_ok": True}

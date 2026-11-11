"""Per-feature replay cases.

Each case reproduces one upstream LLM call shape. Where the upstream function is
self-contained, the real function runs with `get_llm` swapped for the test
client (`run_upstream`); otherwise the case uses upstream's real schema/parser
classes with a representative prompt. A case returns (ok, note) and raises on
transport or parse errors.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.tools import tool

import upstream_loader as up
from client import Session

FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class Case:
    name: str
    feature: str
    shape: str
    fn: Callable[[Session], tuple[bool, str]]
    heavy: bool = False


CASES: list[Case] = []


def case(name: str, feature: str, shape: str, heavy: bool = False):
    def deco(fn):
        CASES.append(Case(name, feature, shape, fn, heavy))
        return fn

    return deco


class _ErrorLog(logging.Handler):
    """Upstream functions often log and swallow LLM errors; surface those as failures."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


@contextlib.contextmanager
def upstream_errors():
    h = _ErrorLog()
    logging.getLogger().addHandler(h)
    try:
        yield h.records
    finally:
        logging.getLogger().removeHandler(h)


def run_upstream(s: Session, module: str, name: str, *args, extra: dict | None = None, **kwargs):
    """Run a real upstream function with get_llm (and usage tracking) replaced."""
    overrides = {"get_llm": s.get_llm, "track_usage": lambda *a, **k: contextlib.nullcontext(), **(extra or {})}
    fn = up.extract(module, [name], overrides)[name]
    with upstream_errors() as errors:
        result = fn(*args, **kwargs)
    if errors:
        raise RuntimeError(f"upstream logged an error: {errors[0][:300]}")
    return result


def german(text: str) -> bool:
    words = set(re.findall(r"[a-zäöüß]+", text.lower()))
    return len(words & {"und", "der", "die", "das", "mit", "für", "ist", "wir", "nicht", "eine", "den", "von", "zu", "am", "im"}) >= 2


# --------------------------------------------------------------------------- chat_extraction
# with_structured_output(Schema) -> response_format json_schema (strict), reasoning_effort low


@case("requires_context", "chat_extraction", "SO json_schema (real fn)")
def _(s):
    yes = run_upstream(s, "utils.llm.chat", "requires_context", "What did Tom and I decide about the Android release last week?")
    no = run_upstream(s, "utils.llm.chat", "requires_context", "Hi, good morning!")
    return yes is True and no is False, f"needs-context={yes}, greeting={no}"


@case("is_omi_question", "chat_extraction", "SO json_schema (real fn)")
def _(s):
    yes = run_upstream(s, "utils.llm.chat", "retrieve_is_an_omi_question", "How do I pair my Omi pendant with a new phone?")
    no = run_upstream(s, "utils.llm.chat", "retrieve_is_an_omi_question", "Remind me to call my mom tomorrow")
    return yes is True and no is False, f"product={yes}, task={no}"


@case("context_dates", "chat_extraction", "SO json_schema, date-time list (real fn)")
def _(s):
    dates = run_upstream(s, "utils.llm.chat", "retrieve_context_dates_by_question", "What did I do last weekend?", "Europe/Berlin")
    ok = len(dates) == 2 and all(isinstance(d, datetime) for d in dates) and dates[0] < dates[1]
    return ok, f"{[d.isoformat() for d in dates]}"


@case("metadata_de", "chat_extraction", "SO json_schema, German input (real fn)")
def _(s):
    text = (
        "Hallo Jonas, kurze Zusammenfassung vom Termin mit Frau Becker von den Stadtwerken München: "
        "Wir liefern die neue Ladesäulen-App bis zum 15. Dezember aus. Bitte schick mir bis Freitag das "
        "Angebot für den Wartungsvertrag. Am Montag um 10 Uhr haben wir ein Folgetreffen in Berlin, "
        "Thema ist die Abrechnung über SAP. Viele Grüße, Anna"
    )
    created = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    meta = run_upstream(s, "utils.llm.chat", "retrieve_metadata_from_text", "uid-test", created, text, "Europe/Berlin")
    people = " ".join(meta.get("people", [])).lower()
    ok = bool(meta.get("topics")) and ("becker" in people or "jonas" in people or "anna" in people)
    return ok, json.dumps({k: meta.get(k) for k in ("people", "topics", "entities", "dates")}, ensure_ascii=False)[:300]


@case("summary_output", "chat_extraction", "SO json_schema, maxLength 200 (OutputMessage)")
def _(s):
    OutputMessage = up.get("utils.llm.chat", "OutputMessage")
    prompt = (
        "You are a supportive friend. Based on this conversation snippet, write one short piece of advice "
        "for the user.\n\nUser: I keep missing my morning runs because I stay up too late scrolling.\n"
        "Friend: Same, it's hard."
    )
    r = s.get_llm("chat_extraction").with_structured_output(OutputMessage).invoke(prompt)
    return 0 < len(r.message) <= 200, repr(r.message[:120])


# --------------------------------------------------------------------------- conversation processing


@case("discard_short", "conv_discard", "text + LenientDiscardParser (real fn)")
def _(s):
    errors: list = []
    trivial = run_upstream(s, "utils.llm.conversation_processing", "should_discard_conversation",
                           "Speaker 0: okay. yeah. hmm sure.", None, 6.0, on_error=errors.append)
    keep = run_upstream(s, "utils.llm.conversation_processing", "should_discard_conversation",
                        "You: Remind me to call the dentist tomorrow at 9 to move my appointment to Friday.",
                        None, 8.0, on_error=errors.append)
    if errors:
        raise RuntimeError(f"discard parser error: {errors[0]}")
    return trivial is True and keep is False, f"trivial->discard={trivial}, task->discard={keep}"


def _notes(s: Session, transcript: str, language: str, started_at: datetime, tz: str):
    """conv_structure one-shot notes: real mount instructions + PydanticOutputParser format, as in
    conversation_processing._get_shaped_conversation_notes (legacy packet, non-rich mode)."""
    d = up.extract("utils.llm.conversation_processing", ["notes_mount", "_parse_notes_model_output"])
    general_title = up.get("utils.llm.conversation_title_people", "general_title_static_instructions")
    StructuredExtraction = up.get("models.structured_extraction", "StructuredExtraction")
    mount = d["notes_mount"]()
    parser = PydanticOutputParser(pydantic_object=StructuredExtraction)
    mount = type(mount)(instructions=general_title(mount.instructions, parser.get_format_instructions()), budget=mount.budget)
    packet = {
        "response_language": language,
        "timezone": tz,
        "started_at": started_at.isoformat(),
        "task_intelligence_capture": False,
        "account_owner_name": "Alex",
        "open_tasks": [],
        "capture_evidence": "FULL TRANSCRIPT\n" + transcript,
        "screen_speaker_bindings": [],
    }
    messages = mount.messages([{"role": "user", "content": json.dumps(packet, ensure_ascii=False)}], explicit_cache=True)
    model = s.get_llm("conv_structure", request_timeout=60.0)
    response = model.invoke(messages)
    content = response.content if isinstance(response.content, str) else "".join(
        p.get("text", "") for p in response.content if isinstance(p, dict))
    return StructuredExtraction.model_validate(d["_parse_notes_model_output"](parser, content))


@case("notes_30min", "conv_structure", "text + PydanticOutputParser, ~30 min transcript", heavy=True)
def _(s):
    transcript = (FIXTURES / "meeting_30min.txt").read_text()
    r = _notes(s, transcript, "en", datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc), "Europe/Berlin")
    n_items, n_events = len(r.action_items or []), len(getattr(r, "events", None) or [])
    ok = bool(r.title) and bool(r.overview) and n_items >= 3
    return ok, f"title={r.title!r}, {len(r.sections or [])} sections, {n_items} action items, {n_events} events"


@case("notes_de", "conv_structure", "text + PydanticOutputParser, German conversation")
def _(s):
    transcript = "\n\n".join([
        "[00:00 - 00:06] You: Also, Samstag ist die Geburtstagsfeier für Mia, oder? Um drei bei uns im Garten.",
        "[00:06 - 00:12] Jana: Genau. Ich hab schon zwölf Kinder eingeladen. Kannst du den Kuchen beim Bäcker Huber bestellen?",
        "[00:12 - 00:19] You: Mach ich, ich ruf morgen früh an. Schokolade mit Erdbeeren, wie letztes Jahr?",
        "[00:19 - 00:27] Jana: Ja, und wir brauchen noch Luftballons und Getränke. Ich geh Freitag nach der Arbeit einkaufen.",
        "[00:27 - 00:35] You: Okay. Und vergiss nicht, Oma am Sonntag anzurufen, sie kann leider nicht kommen, weil ihr Knie wieder weh tut.",
        "[00:35 - 00:42] Jana: Stimmt. Ich schreib ihr heute Abend auch noch eine Nachricht. Wenn es regnet, feiern wir drinnen.",
    ])
    r = _notes(s, transcript, "de", datetime(2026, 11, 10, 17, 0, tzinfo=timezone.utc), "Europe/Berlin")
    text = f"{r.title} {r.overview} " + " ".join(a.description for a in (r.action_items or []))
    ok = german(text) and len(r.action_items or []) >= 1
    return ok, f"title={r.title!r}, {len(r.action_items or [])} action items, german={german(text)}"


@case("app_select", "conv_app_select", "SO json_schema (gpt-5-nano)")
def _(s):
    BestAppSelection = up.get("utils.llm.conversation_processing", "BestAppSelection")
    apps = [
        {"id": "app-fitness", "name": "Workout Coach", "description": "Summarises workouts and training plans"},
        {"id": "app-meeting", "name": "Meeting Minutes", "description": "Turns work meetings into minutes with owners and deadlines"},
        {"id": "app-recipes", "name": "Recipe Saver", "description": "Extracts recipes from cooking conversations"},
    ]
    prompt = (
        "Select the best app for processing this conversation, or an empty string if none fits.\n"
        f"Apps: {json.dumps(apps)}\n\nConversation summary: Weekly product sync. Tom owns the Android crash fix "
        "by Thursday, Priya drafts the newsletter by Monday, demo call booked for the 14th."
    )
    r = s.get_llm("conv_app_select").with_structured_output(BestAppSelection).invoke(prompt)
    return r.app_id == "app-meeting", f"app_id={r.app_id!r}"


@case("external_structure", "external_structure", "text + PydanticOutputParser (real fn)")
def _(s):
    r = run_upstream(
        s, "utils.llm.external_integrations", "get_message_structure",
        "Hey! Can you pick up the dry cleaning before Thursday? Also dinner with the Kims is moved to "
        "Saturday 7pm at Luigi's, I booked a table for four.",
        datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc), "en", "Europe/Berlin",
    )
    ok = bool(r.title) and len(r.action_items or []) >= 1
    return ok, f"title={r.title!r}, {len(r.action_items or [])} action items, {len(r.events or [])} events"


@case("daily_summary", "daily_summary", "plain text, custom prompt (real fn)")
def _(s):
    transcript = (FIXTURES / "meeting_30min.txt").read_text()[:6000]
    out = run_upstream(s, "utils.llm.conversation_processing", "generate_summary_with_prompt",
                       transcript, "Summarize the decisions and open questions as 3-6 bullet points.", "en")
    return len(out) > 80, repr(out[:120])


# --------------------------------------------------------------------------- memories / insights


@case("memories", "memories", "text + PydanticOutputParser (Memories)")
def _(s):
    Memories = up.get("utils.llm.memories", "Memories")
    parser = PydanticOutputParser(pydantic_object=Memories)
    prompt = (
        "Extract durable facts about the user (Alex) from this conversation. Only facts worth remembering for months.\n\n"
        "Alex: I moved to Leipzig in March, still getting used to the trams.\n"
        "Sam: And the new job?\nAlex: Good! I'm the product lead at Harbor now. Also I'm allergic to peanuts, "
        "so no satay for me.\n\n" + parser.get_format_instructions()
    )
    r = parser.invoke(s.get_llm("memories").invoke(prompt))
    return len(r.facts) >= 2, "; ".join(f.content for f in r.facts)[:250]


@case("trends", "trends", "SO json_schema with enums (effort none)")
def _(s):
    ExpectedOutput = up.get("utils.llm.trends", "ExpectedOutput")
    prompt = (
        "Identify the categories, topics and sentiment (type) of the main subjects discussed.\n\n"
        "You: Honestly the new Pixel camera is amazing, but I'm really worried about the interest rates going up again."
    )
    r = s.get_llm("trends").with_structured_output(ExpectedOutput).invoke(prompt)
    return len(r.items) >= 1, "; ".join(f"{i.category.value}/{i.type.value}/{i.topic}" for i in r.items)[:200]


@case("proactive_relevance", "proactive_notification", "SO json_schema + content-part cache breakpoint (real fn)")
def _(s):
    r = run_upstream(
        s, "utils.llm.proactive_notification", "evaluate_relevance",
        "Alex", "Alex is training for a half marathon in April. Alex wants to sleep 8 hours.",
        [{"title": "Run a half marathon under 2 hours"}],
        [{"text": "I think I'll skip my long run again this weekend, I'm just too tired after staying up late.", "is_user": True}],
        [], None, "uid-test",
        extra={"gate_cache_enabled": lambda: True},
    )
    return isinstance(r.is_relevant, bool) and 0 <= r.relevance_score <= 1, f"relevant={r.is_relevant}, score={r.relevance_score}"


@case("what_matters_now", "what_matters_now", "SO json_schema, extra=forbid + length limits")
def _(s):
    JudgmentOutput = up.get("utils.task_intelligence.live_recommendation_judgment", "JudgmentOutput")
    shortlist = [
        {"subject_kind": "task", "subject_id": "t1", "title": "Send Q4 budget to finance (due today 17:00)"},
        {"subject_kind": "task", "subject_id": "t2", "title": "Buy birthday present for Mia (party Saturday)"},
        {"subject_kind": "task", "subject_id": "t3", "title": "Read article about Rust async (no date)"},
    ]
    msgs = [
        SystemMessage("Pick at most 3 items from the shortlist that matter most right now. Use only listed subject ids."),
        HumanMessage(f"Now: Tuesday 15:10. Shortlist: {json.dumps(shortlist)}"),
    ]
    r = s.get_llm("what_matters_now").with_structured_output(JudgmentOutput).invoke(msgs)
    ids = [x.subject_id for x in r.selections]
    # An empty selection is a valid judgment; only invented ids are wrong.
    return set(ids) <= {"t1", "t2", "t3"}, f"selections={ids}"


@case("translation_de_en", "translation", "SO json_schema, German->English (real prompt)")
def _(s):
    LunaTranslationBatch = up.get("utils.translation_core.providers", "LunaTranslationBatch")
    prompt_fn = up.extract("utils.translation_core.providers", ["_translation_prompt"])["_translation_prompt"]
    contents = ["Kannst du morgen den Kuchen beim Bäcker bestellen?", "Oma kommt leider nicht, ihr Knie tut weh."]
    r = s.get_llm("translation").with_structured_output(LunaTranslationBatch).invoke(prompt_fn(contents, "en", "de"))
    texts = [t.model_dump() for t in r.translations]
    flat = json.dumps(texts, ensure_ascii=False).lower()
    return len(texts) == 2 and "cake" in flat and "knee" in flat, json.dumps(texts, ensure_ascii=False)[:250]


# --------------------------------------------------------------------------- vision


def _screenshot_png() -> str:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1280, 800), (30, 30, 36))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1280, 36], fill=(50, 50, 60))
    d.text((12, 10), "harbor-app — src/bluetooth/reconnect.kt — Visual Studio Code", fill=(220, 220, 220))
    code = [
        "fun onReconnect(device: BluetoothDevice) {",
        "    // FIXME: crashes on Android 14 when the GATT callback fires twice",
        "    if (gatt?.connect() == false) {",
        "        logger.warn(\"reconnect failed for ${device.address}\")",
        "        scheduleRetry(backoffMs = 2_000)",
        "    }",
        "}",
    ]
    for i, line in enumerate(code):
        d.text((40, 70 + i * 26), line, fill=(180, 220, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@case("screen_frame_judge", "screen_frame_judge", "SO json_schema + image input (effort none)")
def _(s):
    ScreenFrameJudgement = up.get("models.screen_frame", "ScreenFrameJudgement")
    msg = HumanMessage(content=[
        {"type": "text", "text": "Judge this screen frame for a meeting-notes timeline. Reject anything with credentials, "
                                 "private messages, email, banking, medical data or identifiable people. Caption it (<=160 chars)."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _screenshot_png()}},
    ])
    r = s.get_llm("screen_frame_judge").with_structured_output(ScreenFrameJudgement).invoke([msg])
    return r.outcome in ("approved_clean", "rejected") and bool(r.caption), f"{r.outcome}, badge={r.source_badge}, caption={r.caption[:80]!r}"


@case("openglass_caption", "openglass", "image + max_completion_tokens=150 (effort low)")
def _(s):
    msg = HumanMessage(content=[
        {"type": "text", "text": "Describe what the user is looking at in one sentence."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _screenshot_png()}},
    ])
    r = s.get_llm("openglass").invoke([msg], max_completion_tokens=150)
    text = r.content if isinstance(r.content, str) else str(r.content)
    return len(text.strip()) > 10, f"{text[:120]!r}"


# --------------------------------------------------------------------------- chat / tools / streaming


@tool
def get_conversations_tool(start_date: str, end_date: str, query: str = "") -> str:
    """Fetch the user's recorded conversations between two ISO dates, optionally filtered by a query."""
    return ""


@tool
def search_memories_tool(query: str, limit: int = 5) -> str:
    """Semantic search over facts the user's Omi remembers about them."""
    return ""


@tool
def create_action_item_tool(description: str, due_at: str | None = None) -> str:
    """Create a task / reminder for the user."""
    return ""


@case("chat_agent_tools", "chat_agent", "streaming + function tools, 2 turns (effort none)")
def _(s):
    llm = s.get_llm("chat_agent", streaming=True).bind_tools(
        [get_conversations_tool, search_memories_tool, create_action_item_tool])
    msgs = [
        SystemMessage("You are Omi, the user's memory assistant. Use tools to look things up. Today is 2026-11-10."),
        HumanMessage("What did we decide about the Android crash in Sunday's meeting?"),
    ]
    first = None
    for chunk in llm.stream(msgs):
        first = chunk if first is None else first + chunk
    calls = first.tool_calls if first is not None else []
    if not calls:
        return False, f"no tool call; content={str(first.content)[:120]!r}"
    call = calls[0]
    msgs += [AIMessage(content=first.content or "", tool_calls=calls),
             ToolMessage(tool_call_id=call["id"], content=json.dumps([{
                 "title": "Harbor v2.3 sync", "date": "2026-11-09",
                 "summary": "Tom owns the Android 14 Bluetooth reconnect crash fix, deadline Thursday."}]))]
    answer = ""
    for chunk in llm.stream(msgs):
        answer += chunk.content if isinstance(chunk.content, str) else ""
    ok = "thursday" in answer.lower() or "tom" in answer.lower()
    return ok, f"tool={call['name']}({json.dumps(call['args'])[:80]}), answer={answer[:100]!r}"


@case("chat_stream", "chat_responses", "streaming plain text (effort medium)")
def _(s):
    out = ""
    for chunk in s.get_llm("chat_responses", streaming=True).stream(
            "In two sentences: why do people forget conversations so quickly?"):
        out += chunk.content if isinstance(chunk.content, str) else ""
    return len(out) > 40, repr(out[:120])


@case("session_title", "session_titles", "plain text, cache off (effort none)")
def _(s):
    r = s.get_llm("session_titles").invoke(
        "Write a 2-5 word title for a chat that starts with: 'how do I get my pendant to stop disconnecting?'. Title only.")
    return 0 < len(r.content.split()) <= 8, repr(r.content)


@case("followup", "followup", "plain text (real fn)")
def _(s):
    TranscriptSegment = up.get("models.transcript_segment", "TranscriptSegment")
    segs = [TranscriptSegment(text=t, speaker=f"SPEAKER_0{i % 2}", is_user=i % 2 == 0, start=i * 5.0, end=i * 5.0 + 4)
            for i, t in enumerate([
                "So I finally booked the trip to Japan for April.",
                "Oh nice, where are you going?",
                "Tokyo first, then maybe Kyoto, I really want to see the temples and eat a lot of ramen.",
            ])]
    q = run_upstream(s, "utils.llm.followup", "followup_question_prompt", "uid-test", segs)
    return "?" in q, repr(q[:120])


@case("notes_agent_tools", "conv_structure", "non-streaming function tools + max_completion_tokens 6000")
def _(s):
    @tool
    def submit_notes(title: str, overview: str, action_items: list[str]) -> str:
        """Submit the final conversation note."""
        return "ok"

    llm = s.get_llm("conv_structure").bind_tools([submit_notes], tool_choice="required").bind(max_completion_tokens=6000)
    r = llm.invoke([
        SystemMessage("Write the conversation note and submit it with the submit_notes tool."),
        HumanMessage("You: Remember to renew the car insurance before the 30th. Sam: And book the garage for winter tyres."),
    ])
    calls = r.tool_calls
    ok = bool(calls) and calls[0]["name"] == "submit_notes" and bool(calls[0]["args"].get("title"))
    return ok, f"{calls[0]['args'] if calls else r.content!r}"[:200]

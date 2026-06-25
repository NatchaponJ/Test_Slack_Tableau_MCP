import asyncio
import json
import os
import re
import sys
import threading
from concurrent.futures import Future
from typing import Any

from dotenv import load_dotenv
from flask import Flask
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI
from slack_sdk import WebClient
from slackeventsapi import SlackEventAdapter

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_ENV_PATH)

TABLEAU_SERVER_URL = os.getenv("TABLEAU_SERVER_URL")
TABLEAU_SITE_ID = os.getenv("TABLEAU_SITE_ID", "")
TABLEAU_TOKEN_NAME = os.getenv("TABLEAU_TOKEN_NAME")
TABLEAU_TOKEN_VALUE = os.getenv("TABLEAU_TOKEN_VALUE")
TABLEAU_DATASOURCE_LUID = os.getenv("TABLEAU_DATASOURCE_ID")
TABLEAU_MCP_VERSION = os.getenv("TABLEAU_MCP_VERSION", "1.15.0") # version of mcp-server to use (current available: 1.15.0)

SLACK_TOKEN = os.getenv("SLACK_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL_TOOLS = os.getenv("GROQ_MODEL_TOOLS", "openai/gpt-oss-120b")
GROQ_MODEL_SUMMARY = os.getenv("GROQ_MODEL_SUMMARY", "openai/gpt-oss-20b")

MAX_TOOL_ROUNDS = 5 # เรียก tool ได้ตามรอบที่กำหนด ถ้าเกินจะหยุดละแจ้งว่าตอบไม่ได้
HISTORY_MAX_TURNS = 6 # จำนวนที่เก็บ history (1 เทิร์น = user+assistant 1 คู่) ต่อ channel

_REQUIRED_ENV = {
    "GROQ_API_KEY": GROQ_API_KEY,
    "SLACK_TOKEN": SLACK_TOKEN,
    "SLACK_SIGNING_SECRET": SLACK_SIGNING_SECRET,
    "TABLEAU_SERVER_URL": TABLEAU_SERVER_URL,
    "TABLEAU_TOKEN_NAME": TABLEAU_TOKEN_NAME,
    "TABLEAU_TOKEN_VALUE": TABLEAU_TOKEN_VALUE,
}
_missing = [k for k, v in _REQUIRED_ENV.items() if not v]
if _missing:
    raise RuntimeError(
        "ขาด env var: " + ", ".join(_missing) + f"\n"
        f"กำลังหา .env ที่: {_ENV_PATH}\n"
        "เช็คว่าไฟล์ .env อยู่ตรงนี้จริง และไม่มี quote/space เกินรอบค่า เช่น GROQ_API_KEY=gsk_xxx"
    )
llm_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
slack_client = WebClient(token=SLACK_TOKEN)

app = Flask(__name__)
slack_events_adapter = SlackEventAdapter(SLACK_SIGNING_SECRET, "/slack/events", app)
processed_events: set[str] = set()

_history_lock = threading.Lock()
_channel_history: dict[str, list[dict]] = {}

def get_history(channel: str) -> list[dict]:
    with _history_lock:
        return list(_channel_history.get(channel, []))

def append_history(channel: str, question: str, answer: str) -> None:
    with _history_lock:
        history = _channel_history.setdefault(channel, [])
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        # เก็บแค่ HISTORY_MAX_TURNS เทิร์นล่าสุด (1 เทิร์น = 2 message)
        max_messages = HISTORY_MAX_TURNS * 2
        if len(history) > max_messages:
            del history[: len(history) - max_messages]

def reset_history(channel: str) -> None:
    with _history_lock:
        _channel_history.pop(channel, None)

class MCPManager:
    """
    เปิด event loop หนึ่งตัวใน background thread, สั่ง spawn
    `npx @tableau/mcp-server` เป็น subprocess ผ่าน stdio_client, และเก็บ
    ClientSession ไว้ใช้ซ้ำ มี method `call()` แบบ sync ให้โค้ดส่วนอื่น
    (เช่น Flask handler) เรียกได้โดยไม่ต้องสนใจ asyncio
    """

    def __init__(self, server_params: StdioServerParameters):
        self._server_params = server_params
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: ClientSession | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)

    def start(self, timeout: float = 30.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError("MCP server ไม่ start ภายในเวลาที่กำหนด (timeout)")

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        async with stdio_client(self._server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.25)

    def call(self, coro_factory, timeout: float = 60.0) -> Any:
        """
        รัน coroutine บน background loop แล้วบล็อกรอผลลัพธ์แบบ sync
        coro_factory: callable ที่รับ session แล้ว return coroutine
            เช่น lambda session: session.call_tool("name", {...})
        """
        if self._loop is None or self._session is None:
            raise RuntimeError("MCP session ยังไม่พร้อมใช้งาน")

        future: Future = asyncio.run_coroutine_threadsafe(
            coro_factory(self._session), self._loop
        )
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self._stop.set()

mcp_server_params = StdioServerParameters(
    command="npx.cmd" if sys.platform == "win32" else "npx",
    args=["-y", f"@tableau/mcp-server@{TABLEAU_MCP_VERSION}"],
    env={
        **os.environ,
        "SERVER": TABLEAU_SERVER_URL or "",
        "SITE_NAME": TABLEAU_SITE_ID,
        "PAT_NAME": TABLEAU_TOKEN_NAME or "",
        "PAT_VALUE": TABLEAU_TOKEN_VALUE or "",
        # จำกัดจำนวนแถวที่ tool คืนมาตั้งแต่ต้นทาง กัน response ใหญ่เกิน
        # TPM limit ของ Groq (free/on_demand tier มีแค่ 12000 token/นาที)
        "MAX_RESULT_LIMIT": os.getenv("TABLEAU_MAX_RESULT_LIMIT", "10"),
        "INCLUDE_TOOLS": os.getenv("TABLEAU_INCLUDE_TOOLS", "datasource"),
    },
)

mcp_manager = MCPManager(mcp_server_params)

def simplify_schema(node, _depth: int = 0):
    """
    ตัด key ที่กิน token เยอะแต่ไม่จำเป็นต่อการเรียก tool (description ยาวๆ, title, examples) 
    ทิ้งเก็บไว้แค่ type / properties / required / enum / items ซึ่งจำเป็นจริงๆ
    ต่อความถูกต้องของ JSON Schema สำหรับ function-calling
    """
    if isinstance(node, dict):
        cleaned = {}
        for key, value in node.items():
            if key in ("description", "title", "examples", "$schema", "additionalProperties"):
                continue
            cleaned[key] = simplify_schema(value, _depth + 1)
        return cleaned
    if isinstance(node, list):
        return [simplify_schema(v, _depth + 1) for v in node]
    return node

def get_mcp_tools_for_groq() -> list[dict]:
    """ดึง tool schema จาก MCP server แปลงเป็น format ที่ Groq/OpenAI ต้องการ"""
    result = mcp_manager.call(lambda session: session.list_tools())
    tools = []
    for t in result.tools:
        raw_description = t.description or ""
        short_description = raw_description.split("\n")[0][:200]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": short_description,
                    "parameters": simplify_schema(t.inputSchema) or {"type": "object", "properties": {}},
                },
            }
        )
    return tools

def call_mcp_tool(name: str, arguments: dict) -> tuple[str, bool]:
    """เรียก tool บน MCP server คืน (ข้อความผลลัพธ์, is_error)"""
    result = mcp_manager.call(lambda session: session.call_tool(name, arguments))
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    text = "\n".join(parts) if parts else "(tool คืนค่าว่าง)"
    return text, getattr(result, "isError", False)

SYSTEM_PROMPT = """
คุณคือผู้ช่วยตอบคำถามเกี่ยวกับข้อมูลใน Tableau
คุณมี tools สำหรับค้นหา datasource, ดู field ที่มีอยู่ และ query ข้อมูลจาก Tableau
ถ้าผู้ใช้ไม่ได้บอก datasource ชัดเจน ให้ใช้ datasource ที่มี LUID: {default_luid}
ตอบคำถามให้ตรงประเด็น ใช้ tool เท่าที่จำเป็น และอย่าสมมติข้อมูลที่ไม่ได้มาจาก tool
สำคัญ: เวลา query ข้อมูล ให้ดึงแบบสรุป/aggregate (เช่น SUM, group by) หรือใส่ top
(เช่น top N) เสมอ ห้ามดึงข้อมูลดิบทุกแถวแบบไม่จำกัด เพราะ response จะใหญ่เกินไป
filter เฉพาะ field ที่จำเป็นต่อคำตอบเท่านั้น และถ้าไม่แน่ใจว่ามี field ไหน
ให้เรียก get-datasource-metadata ก่อน
filtertype: Top N, Aggregate, Group by, Filter, Sort
""".strip()


def run_full_workflow_mcp(question: str, tools: list[dict], history: list[dict]) -> str:
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(default_luid=TABLEAU_DATASOURCE_LUID or "ไม่ระบุ"),
        },
        *history,
        {"role": "user", "content": question},
    ]

    for round_num in range(MAX_TOOL_ROUNDS):
        response = llm_client.chat.completions.create(
            model=GROQ_MODEL_TOOLS,
            temperature=0,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            final_text = msg.content or ""
            return summarize_answer(question, final_text)

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in msg.tool_calls
                ],
            }
        )

        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            print(f">>> [round {round_num}] calling tool: {call.function.name} args={args}")
            try:
                tool_result_text, is_error = call_mcp_tool(call.function.name, args)
                print(f"    <- {'ERROR' if is_error else 'result'}: {tool_result_text[:300]!r}")
            except Exception as exc:  # noqa: BLE001
                tool_result_text = f"เกิดข้อผิดพลาดตอนเรียก tool: {exc}"
                print(f"    !! tool exception: {exc}")

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": tool_result_text[:4000],  # กันข้อมูลใหญ่เกินไป
                }
            )

    return "ขออภัยครับ คำถามนี้ซับซ้อนเกินกว่าที่จะตอบได้ในรอบที่กำหนด ลองถามให้เจาะจงขึ้นได้ไหมครับ"


def summarize_answer(question: str, raw_answer: str) -> str:
    """ให้โมเดลเล็กช่วยขัดคำตอบให้เป็นภาษาไทยที่อ่านง่าย"""
    if not raw_answer.strip():
        return "ขออภัยครับ ไม่พบข้อมูลที่ตอบคำถามนี้ได้"

    response = llm_client.chat.completions.create(
        model=GROQ_MODEL_SUMMARY,
        messages=[
            {
                "role": "user",
                "content": (
                    f"คำถาม: {question}\n\n"
                    f"คำตอบดิบจากระบบ: {raw_answer}\n\n"
                    "ช่วยสรุปคำตอบนี้เป็นภาษาไทยที่กระชับ อ่านง่าย "
                    "ไม่ต้องอธิบายขั้นตอนการทำงาน"
                ),
            }
        ],
    )
    return response.choices[0].message.content


def clean_question(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text).strip()


RESET_KEYWORDS = {"reset", "ล้างประวัติ", "เริ่มใหม่", "ลืมเรื่องเดิม"}


@slack_events_adapter.on("app_mention")
def handle_mention(event_data):
    event_id = event_data.get("event_id")
    if event_id in processed_events:
        return
    processed_events.add(event_id)

    event = event_data["event"]
    question = clean_question(event["text"])
    channel = event["channel"]
    print(f">>> [{channel}] คำถาม: {question}")

    if question.lower() in RESET_KEYWORDS:
        reset_history(channel)
        slack_client.chat_postMessage(channel=channel, text="ล้างประวัติการสนทนาในห้องนี้แล้วครับ")
        return

    slack_client.chat_postMessage(channel=channel, text="กำลังประมวลผล รอสักครู่ครับ...")

    def process():
        try:
            tools = get_mcp_tools_for_groq()
            history = get_history(channel)
            answer = run_full_workflow_mcp(question, tools, history)
            append_history(channel, question, answer)
        except Exception as exc:  # noqa: BLE001
            answer = f"ขออภัยครับ เกิดข้อผิดพลาด: {exc}"
        slack_client.chat_postMessage(channel=channel, text=answer)

    threading.Thread(target=process).start()


if __name__ == "__main__":
    print(">>> starting MCP server (npx @tableau/mcp-server)...")
    mcp_manager.start()
    print(">>> MCP server ready, starting Flask app...")
    try:
        app.run(port=3000)
    finally:
        mcp_manager.stop()
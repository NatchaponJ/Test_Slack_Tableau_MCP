import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_ENV_PATH)

TABLEAU_SERVER_URL = os.getenv("TABLEAU_SERVER_URL")
TABLEAU_SITE_ID = os.getenv("TABLEAU_SITE_ID", "")
TABLEAU_TOKEN_NAME = os.getenv("TABLEAU_TOKEN_NAME")
TABLEAU_TOKEN_VALUE = os.getenv("TABLEAU_TOKEN_VALUE")
TABLEAU_DATASOURCE_LUID = os.getenv("TABLEAU_DATASOURCE_ID")
TABLEAU_MCP_VERSION = os.getenv("TABLEAU_MCP_VERSION", "1.15.0") # version of mcp-server to use (current available: 1.15.0)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL_TOOLS = os.getenv("GROQ_MODEL_TOOLS", "openai/gpt-oss-120b")
GROQ_MODEL_SUMMARY = os.getenv("GROQ_MODEL_SUMMARY", "openai/gpt-oss-20b")

MAX_TOOL_ROUNDS = 5 #เรียก tool ได้ตามรอบที่กำหนด ถ้าเกินจะหยุดละแจ้งว่าตอบไม่ได้


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "(ไม่ได้ set)"
    return value[:keep] + "..." + value[-keep:] if len(value) > keep * 2 else "***"


def print_env_summary() -> None:
    print("=== ENV ที่โหลดได้ ===")
    print(f"  .env path        : {_ENV_PATH}  (exists={os.path.exists(_ENV_PATH)})")
    print(f"  TABLEAU_SERVER_URL: {TABLEAU_SERVER_URL}")
    print(f"  TABLEAU_SITE_ID   : {TABLEAU_SITE_ID!r}")
    print(f"  TABLEAU_TOKEN_NAME: {TABLEAU_TOKEN_NAME}")
    print(f"  TABLEAU_TOKEN_VALUE: {mask(TABLEAU_TOKEN_VALUE)}")
    print(f"  GROQ_API_KEY      : {mask(GROQ_API_KEY)}")
    print(f"  mcp-server version: {TABLEAU_MCP_VERSION}")
    print("=" * 30)


def build_server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command="npx.cmd" if sys.platform == "win32" else "npx",
        args=["-y", f"@tableau/mcp-server@{TABLEAU_MCP_VERSION}"],
        env={
            **os.environ,
            "SERVER": TABLEAU_SERVER_URL or "",
            "SITE_NAME": TABLEAU_SITE_ID,
            "PAT_NAME": TABLEAU_TOKEN_NAME or "",
            "PAT_VALUE": TABLEAU_TOKEN_VALUE or "",
            # จำกัดจำนวนแถวที่ tool คืนมาตั้งแต่ต้นทาง กัน response ใหญ่เกิน
            # TPM limit ของ Groq (free/on_demand tier มีแค่ 12000 token/นาที) เลยจำกัดไว้ที่ 10 แถวเป็น default ก่อนถ้าเปลี่ยน ai ค่อยแก้
            "MAX_RESULT_LIMIT": os.getenv("TABLEAU_MAX_RESULT_LIMIT", "10"),
            "INCLUDE_TOOLS": os.getenv("TABLEAU_INCLUDE_TOOLS", "datasource"),
        },
    )


def simplify_schema(node, _depth: int = 0):
    """
    ตัด key ที่กิน token เยอะแต่ไม่จำเป็นต่อการที่ LLM จะเรียก tool ถูก
    (description ยาวๆ, title, examples) ทิ้งแบบ recursive
    เก็บไว้แค่ type / properties / required / enum / items ซึ่งจำเป็นจริงๆ
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


def tools_to_groq_format(mcp_tools) -> list[dict]:
    tools = []
    for t in mcp_tools:
        raw_description = t.description or ""
        # ตัด description ให้เหลือสั้นๆ เอาเฉพาะที่ใช้ได้ให้ ai เลือก tool ถูก จะได้ไม่เปลือง token
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


def tool_result_to_text(result) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts) if parts else "(tool คืนค่าว่าง)"


async def ask_question(session: ClientSession, llm_client: OpenAI, tools: list[dict], question: str, conversation_history: list[dict] = None) -> str:
    system_prompt = (
        "คุณคือผู้ช่วยตอบคำถามเกี่ยวกับข้อมูลใน Tableau\n"
        "คุณมี tools สำหรับค้นหา datasource, ดู field ที่มีอยู่ และ query ข้อมูลจาก Tableau\n"
        f"ถ้าผู้ใช้ไม่ได้บอก datasource ชัดเจน ให้ใช้ datasource ที่มี LUID: {TABLEAU_DATASOURCE_LUID or 'ไม่ระบุ'}\n"
        "ตอบคำถามให้ตรงประเด็น ใช้ tool เท่าที่จำเป็น และอย่าสมมติข้อมูลที่ไม่ได้มาจาก tool\n"
        "สำคัญ: เวลา query ข้อมูล ให้ดึงแบบสรุป/aggregate (เช่น SUM, group by) หรือใส่ top "
        "(เช่น top N) เสมอ ห้ามดึงข้อมูลดิบทุกแถวแบบไม่จำกัด เพราะ response จะใหญ่เกินไป"
        "filter เฉพาะ field ที่จำเป็นต่อคำตอบเท่านั้น และถ้าไม่แน่ใจว่ามี field ไหน ให้เรียก get-datasource-metadata ก่อน"
        "filtertype: Top N, Aggregate, Group by, Filter, Sort"
    )
    
    # เก็บ history ให้ AI รู้ context ของคำถามต่อไป
    if conversation_history is None:
        conversation_history = []
    
    messages = [
        {"role": "system", "content": system_prompt},
    ]
    # เพิ่ม history
    messages.extend(conversation_history)
    # เพิ่มคำถามใหม่
    messages.append({"role": "user", "content": question})

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
            if not final_text.strip():
                return "ขออภัยครับ ไม่พบข้อมูลที่ตอบคำถามนี้ได้"
            summary = llm_client.chat.completions.create(
                model=GROQ_MODEL_SUMMARY,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"คำถาม: {question}\n\nคำตอบดิบ: {final_text}\n\n"
                            "ช่วยสรุปเป็นภาษาไทยที่กระชับ อ่านง่าย"
                        ),
                    }
                ],
            )
            return summary.choices[0].message.content

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments},
                    }
                    for c in msg.tool_calls
                ],
            }
        )

        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            print(f"  [round {round_num}] -> calling tool: {call.function.name}({args})")
            try:
                result = await session.call_tool(call.function.name, args)
                tool_text = tool_result_to_text(result)
                is_error = getattr(result, "isError", False)
                preview = tool_text[:500].replace("\n", " ")
                print(f"      <- {'ERROR' if is_error else 'result'}: {preview}")
            except Exception as exc:
                tool_text = f"เกิดข้อผิดพลาดตอนเรียก tool: {exc}"
                print(f"  !! tool error: {exc}")

            messages.append({"role": "tool", "tool_call_id": call.id, "content": tool_text[:4000]})

    return "ขออภัยครับ คำถามนี้ซับซ้อนเกินกว่าจะตอบได้ในรอบที่กำหนด"


async def main() -> None:
    print_env_summary()

    if not GROQ_API_KEY:
        print("!! ขาด GROQ_API_KEY ใน .env หยุดทำงาน")
        return

    server_params = build_server_params()
    print(f">>> spawning: {server_params.command} {' '.join(server_params.args)}")

    llm_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            print(">>> initializing MCP session...")
            await session.initialize()
            print(">>> MCP session OK, listing tools...")

            tools_result = await session.list_tools()
            print(f">>> พบ {len(tools_result.tools)} tools:")
            for t in tools_result.tools:
                print(f"   - {t.name}")

            if "--tools-only" in sys.argv:
                print(">>> --tools-only mode, จบการทดสอบ")
                return

            groq_tools = tools_to_groq_format(tools_result.tools)
            tools_char_len = len(json.dumps(groq_tools, ensure_ascii=False))
            print(
                f">>> ขนาด tools schema ที่ต้องส่งไปทุก request: ~{tools_char_len} ตัวอักษร "
                f"(~{tools_char_len // 4} token โดยประมาณ)"
            )

            cli_args = [a for a in sys.argv[1:] if not a.startswith("--")]
            if cli_args:
                question = " ".join(cli_args)
                print(f"\n>>> คำถาม: {question}")
                answer = await ask_question(session, llm_client, groq_tools, question, conversation_history=[])
                print(f"\n=== คำตอบ ===\n{answer}\n")
                return

            print("\n>>> เข้าโหมดถามตอบ (พิมพ์ 'exit' เพื่อออก)")
            conversation_history = []  # เก็บประวัติการสนทนา
            while True:
                question = input("\nคำถาม> ").strip()
                if question.lower() in ("exit", "quit", "q"):
                    break
                if not question:
                    continue
                try:
                    answer = await ask_question(session, llm_client, groq_tools, question, conversation_history)
                    print(f"\n=== คำตอบ ===\n{answer}")
                    
                    # เพิ่มคำถาม-คำตอบไปใน history เพื่อให้ถามต่อได้
                    conversation_history.append({"role": "user", "content": question})
                    conversation_history.append({"role": "assistant", "content": answer})
                except Exception as exc:
                    print(f"!! เกิดข้อผิดพลาด: {exc}")


if __name__ == "__main__":
    asyncio.run(main())

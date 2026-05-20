import asyncio
import sys
from dotenv import load_dotenv
import os
from slack_sdk import WebClient
from flask import Flask
from slackeventsapi import SlackEventAdapter
import re
import threading
import json
from openai import OpenAI

# ── import ใหม่แทน tableauhyperapi ──────────────────────────────────────────
from tableau_rest import TableauCloudClient, query_tableau_cloud
# ────────────────────────────────────────────────────────────────────────────

load_dotenv()

TABLEAU_TOKEN_NAME    = os.getenv("TABLEAU_TOKEN_NAME")
TABLEAU_TOKEN_VALUE   = os.getenv("TABLEAU_TOKEN_VALUE")
TABLEAU_SITE_ID       = os.getenv("TABLEAU_SITE_ID")
TABLEAU_SERVER_URL    = os.getenv("TABLEAU_SERVER_URL")
TABLEAU_DATASOURCE_ID = os.getenv("TABLEAU_DATASOURCE_ID")

SLACK_TOKEN           = os.getenv("SLACK_TOKEN")
SLACK_SIGNING_SECRET  = os.getenv("SLACK_SIGNING_SECRET")

# ── สร้าง Tableau client ครั้งเดียวตอน startup (ไม่ต้อง login ซ้ำทุก request) ──
tableau_client = TableauCloudClient(
    server_url=TABLEAU_SERVER_URL,
    site_id=TABLEAU_SITE_ID,
    token_name=TABLEAU_TOKEN_NAME,
    token_value=TABLEAU_TOKEN_VALUE
)
tableau_client.login()  # login ครั้งแรก token อยู่ได้หลายชั่วโมง
# ─────────────────────────────────────────────────────────────────────────────

client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)
slack_client = WebClient(token=SLACK_TOKEN)

app = Flask(__name__)
slack_events_adapter = SlackEventAdapter(SLACK_SIGNING_SECRET, "/slack/events", app)

processed_events = set()

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def summarize_answer(question, data):
    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "user", "content": f"Data:\n{data}\n\nQuestion: {question}\nAnswer in Thai."}
        ]
    )
    return response.choices[0].message.content


def run_full_workflow(question):
    try:
        # ขั้น 1: Groq แปลงคำถามเป็น SQL (เหมือนเดิมทุกอย่าง)
        sql_response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": f"""
                Convert this question to SQL for Tableau datasource.
                Table name: "Extract"."Extract"
                Columns: "Row ID", "Order ID", "Order Date", "Ship Date", "Ship Mode",
                "Customer ID", "Customer Name", "Segment", "Country/Region", "City",
                "State", "Postal Code", "Region", "Product ID", "Category", "Sub-Category",
                "Product Name", "Sales", "Quantity", "Discount", "Profit"

                Question: {question}

                STRICT RULES:
                - Use double quotes for column names: "Product Name", "Sales"
                - Use double quotes for table: "Extract"."Extract"
                - NO backticks (`) anywhere
                - Use FETCH FIRST N ROWS ONLY instead of LIMIT N
                - Date format: DATE '2019-01-01'

                Return ONLY the SQL query, no explanation.
            """}]
        )
        sql = sql_response.choices[0].message.content.strip()
        sql = sql.replace("```sql", "").replace("```", "").strip()
        print(">>> Generated SQL:", sql)

        # ขั้น 2: ── เปลี่ยนตรงนี้เท่านั้น ──────────────────────────────────
        # เดิม: results = query_with_sql(sql, hyper_path=os.getenv("HYPER_FILE_PATH"))
        # ใหม่:
        results = query_tableau_cloud(
            sql=sql,
            datasource_luid=TABLEAU_DATASOURCE_ID,
            client=tableau_client
        )
        # ─────────────────────────────────────────────────────────────────────
        print(">>> Query results:", results[:5])

        # ขั้น 3: AI สรุปคำตอบ (เหมือนเดิมทุกอย่าง)
        return summarize_answer(question, json.dumps(results[:50], ensure_ascii=False))

    except Exception as e:
        return f"ขออภัยครับ เกิดข้อผิดพลาด: {str(e)}"


def clean_question(text):
    return re.sub(r'<@[A-Z0-9]+>', '', text).strip()


@slack_events_adapter.on("app_mention")
def handle_mention(event_data):
    print(">>> ได้รับ event แล้ว:", event_data)

    event_id = event_data.get("event_id")
    if event_id in processed_events:
        print(">>> duplicate event ข้ามไป")
        return
    processed_events.add(event_id)

    event = event_data["event"]
    question = clean_question(event["text"])
    channel = event["channel"]
    print(f">>> คำถาม: {question}, channel: {channel}")

    slack_client.chat_postMessage(
        channel=channel,
        text="กำลังประมวลผล รอสักครู่ครับ..."
    )

    def process():
        answer = run_full_workflow(question)
        slack_client.chat_postMessage(channel=channel, text=answer)

    threading.Thread(target=process).start()


if __name__ == "__main__":
    app.run(port=3000)

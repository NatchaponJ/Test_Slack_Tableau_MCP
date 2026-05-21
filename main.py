import asyncio
from curses import raw
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

from tableau_rest import TableauCloudClient, query_tableau_cloud

load_dotenv()

TABLEAU_TOKEN_NAME    = os.getenv("TABLEAU_TOKEN_NAME")
TABLEAU_TOKEN_VALUE   = os.getenv("TABLEAU_TOKEN_VALUE")
TABLEAU_SITE_ID       = os.getenv("TABLEAU_SITE_ID")
TABLEAU_SERVER_URL    = os.getenv("TABLEAU_SERVER_URL")
TABLEAU_DATASOURCE_ID = os.getenv("TABLEAU_DATASOURCE_ID")
SLACK_TOKEN           = os.getenv("SLACK_TOKEN")
SLACK_SIGNING_SECRET  = os.getenv("SLACK_SIGNING_SECRET")

tableau_client = TableauCloudClient(
    server_url=TABLEAU_SERVER_URL,
    site_id=TABLEAU_SITE_ID,
    token_name=TABLEAU_TOKEN_NAME,
    token_value=TABLEAU_TOKEN_VALUE
)
tableau_client.login()

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

DATE_FUNCTION_PATTERNS = {
    r"YEAR\((.+?)\)": ("YEAR", r"\1"),
    r"MONTH\((.+?)\)": ("MONTH", r"\1"),
    r"QUARTER\((.+?)\)": ("QUARTER", r"\1"),
    r"WEEK\((.+?)\)": ("WEEK", r"\1"),
    r"DAY\((.+?)\)": ("DAY", r"\1"),
}

def normalize_field(f: dict) -> dict:
    """แปลง fieldCaption ที่ AI ใส่ function ปนมา เช่น YEAR(Order Date) → function: YEAR"""
    caption = f.get("fieldCaption", "")
    for pattern, (func, field_group) in DATE_FUNCTION_PATTERNS.items():
        match = re.match(pattern, caption)
        if match:
            f["fieldCaption"] = match.expand(field_group).strip()
            f["function"] = func  # override function ให้ถูก
            break
    return f

def fix_query(query):
    allowed_field_keys = {"fieldCaption", "function", "fieldAlias",
                          "sortDirection", "sortPriority", "binSize", "maxDecimalPlaces"}
    valid_sort_directions = {"ASC", "DESC"}
    valid_functions = {
        "SUM", "AVG", "COUNT", "COUNTD", "MIN", "MAX",
        "YEAR", "QUARTER", "MONTH", "WEEK", "DAY",
        "TRUNC_YEAR", "TRUNC_QUARTER", "TRUNC_MONTH", "TRUNC_WEEK", "TRUNC_DAY"
    }

    fixed_fields = []
    for f in query.get("fields", []):
        fixed = {k: v for k, v in f.items() if k in allowed_field_keys}
        if "sortDirection" in fixed and fixed["sortDirection"] not in valid_sort_directions:
            del fixed["sortDirection"]
        if "function" in fixed and fixed["function"] not in valid_functions:
            del fixed["function"]
        fixed_fields.append(fixed)

    fixed_filters = []
    for f in query.get("filters", []):
        filter_type = f.get("filterType")
        valid_types = {"SET", "TOP", "MATCH", "QUANTITATIVE_NUMERICAL", "QUANTITATIVE_DATE", "DATE"}
        if filter_type not in valid_types:
            continue

        if "field" in f and isinstance(f["field"], dict):
            f["field"] = normalize_field(f["field"])

        if filter_type == "TOP":
            if f.get("direction") not in ["TOP", "BOTTOM"]:
                f["direction"] = "TOP"
            if "howMany" in f:
                f["howMany"] = int(f["howMany"])
            if "fieldToMeasure" in f and isinstance(f["fieldToMeasure"], str):
                f["fieldToMeasure"] = {"fieldCaption": f["fieldToMeasure"], "function": "SUM"}
        fixed_filters.append(f)

    return {
        "fields": fixed_fields,
        "filters": fixed_filters
    }

SYSTEM_PROMPT = """
You are a field specification generator for Tableau VizQL Data Service queries.

AVAILABLE COLUMNS:
- Measures: {measures}
- Dimensions: {dimensions}
- Calculations: {calculations}

YOUR TASK:
Convert natural language queries into a JSON array of field specifications.

FORMAT RULES:
- Measures: {{"fieldCaption": "COLUMN_NAME", "function": "SUM", "maxDecimalPlaces": 2}}
- Dimensions: {{"fieldCaption": "COLUMN_NAME"}}
- Date grouping: {{"fieldCaption": "Order Date", "function": "MONTH"}}

ALLOWED FUNCTIONS:
- Measures: SUM, AVG, COUNT, COUNTD, MIN, MAX
- Dates: YEAR, QUARTER, MONTH, WEEK, DAY, TRUNC_YEAR, TRUNC_QUARTER, TRUNC_MONTH

SORTING RULES (optional):
- sortDirection must be exactly "ASC" or "DESC" — never empty string
- sortPriority must be a unique integer (1, 2, 3...)

STRICT RULES:
- Return ONLY a JSON array, no explanation, no markdown
- Never include keys other than: fieldCaption, function, fieldAlias, sortDirection, sortPriority, maxDecimalPlaces
- Always include at least one field

CRITICAL: fieldCaption must ALWAYS be the raw column name only.
NEVER put functions inside fieldCaption.

FILTER RULES:
- To compare specific years, use SET filter on YEAR of Order Date
- filterType "SET" requires: field, values (array), exclude (boolean)
- field in filter must use {{"fieldCaption": "Order Date", "function": "YEAR"}}

WRONG: 
{{
  "fields": [
  {{"fieldCaption": "YEAR(Order Date)", "function": "MAX"}}
  ],
    "filters": []
}}
RIGHT: 
{{
  "fields": [
  {{"fieldCaption": "Order Date", "function": "YEAR"}}
    ],
    "filters": []
}}

WRONG: 
{{
  "fields": [
  {{"fieldCaption": "SUM(Sales)"}}
    ],
    "filters": []
}}
RIGHT: {{"fieldCaption": "Sales", "function": "SUM"}}

EXAMPLES:
"เดือนและปีที่ขายดีสุด" →
{{
  "fields": [
  {{"fieldCaption": "Order Date", "function": "YEAR", "sortPriority": 1}},
  {{"fieldCaption": "Order Date", "function": "MONTH", "sortPriority": 2}},
  {{"fieldCaption": "Sales", "function": "SUM", "sortDirection": "DESC", "sortPriority": 3}}
    ],
  "filters": []
}}

"Sales by Region" → 
{{
  "fields": [
  {{"fieldCaption": "Region"}},
  {{"fieldCaption": "Sales", "function": "SUM", "maxDecimalPlaces": 2, "sortDirection": "DESC", "sortPriority": 1}}
  ],
  "filters": []
}}

"ยอดขายแต่ละเดือน" → 
{{
  "fields": [
  {{"fieldCaption": "Order Date", "function": "MONTH", "sortDirection": "ASC", "sortPriority": 1}},
  {{"fieldCaption": "Sales", "function": "SUM", "maxDecimalPlaces": 2}}
  ],
  "filters": []
}}

"เปรียบเทียบยอดขายปี 2019 กับ 2020" →
{{
  "fields": [
    {{"fieldCaption": "Order Date", "function": "YEAR", "sortDirection": "ASC", "sortPriority": 1}},
    {{"fieldCaption": "Sales", "function": "SUM", "sortDirection": "DESC", "sortPriority": 2}}
  ],
  "filters": [
    {{
      "field": {{"fieldCaption": "Order Date", "function": "YEAR"}},
      "filterType": "SET",
      "values": [2019, 2020],
      "exclude": false
    }}
  ]
}}

"ยอดขายปี 2020 แยกตามเดือน" →
{{
  "fields": [
    {{"fieldCaption": "Order Date", "function": "MONTH", "sortDirection": "ASC", "sortPriority": 1}},
    {{"fieldCaption": "Sales", "function": "SUM"}}
  ],
  "filters": [
    {{
      "field": {{"fieldCaption": "Order Date", "function": "YEAR"}},
      "filterType": "SET",
      "values": [2020],
      "exclude": false
    }}
  ]
}}

"ปีไหนขายดีสุด" →
{{
  "fields": [
    {{"fieldCaption": "Order Date", "function": "YEAR", "sortPriority": 1}},
    {{"fieldCaption": "Sales", "function": "SUM", "sortDirection": "DESC", "sortPriority": 2}}
  ],
  "filters": []
}}

"สินค้าที่ขายดีสุด" →
{{
  "fields": [
    {{"fieldCaption": "Product Name"}},
    {{"fieldCaption": "Sales", "function": "SUM", "sortDirection": "DESC", "sortPriority": 1}}
  ],
  "filters": []
}}

Return ONLY this JSON format, no explanation, no markdown:
{{
  "fields": [...],
  "filters": [...]
}}

"""

def natural_language_to_query(question, measures, dimensions, calculations=""):
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(
                    measures=measures,
                    dimensions=dimensions,
                    calculations=calculations
                )
            },
            {"role": "user", "content": question}
        ]
    )
    raw = response.choices[0].message.content
    print(">>> RAW AI output:", raw)
    start = raw.find('{')
    count = 0
    end = start
    for i, c in enumerate(raw[start:]):
        if c == '{': count += 1
        elif c == '}': count -= 1
        if count == 0:
            end = start + i + 1
            break

    parsed = json.loads(raw[start:end].strip())
    fields = [normalize_field(f) for f in parsed.get("fields", [])]
    filters = parsed.get("filters", [])
    return fix_query({"fields": fields, "filters": filters})


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
        measures = ["Sales", "Profit", "Quantity", "Discount"]
        dimensions = ["Product Name", "Category", "Sub-Category",
                      "Order Date", "Region", "Segment", "Customer Name", "City"]
        
        print(">>> formatting prompt...")
        prompt = SYSTEM_PROMPT.format(measures=measures, dimensions=dimensions, calculations="")
        print(">>> prompt OK")

        query = natural_language_to_query(question, measures, dimensions)
        print(">>> Generated query:", json.dumps(query, indent=2))

        results = query_tableau_cloud(
            query=query,
            datasource_luid=TABLEAU_DATASOURCE_ID,
            client=tableau_client
        )
        print(">>> Query results:", results[:5])

        return summarize_answer(question, json.dumps(results[:50], ensure_ascii=False))

    except Exception as e:
        return f"ขออภัยครับ เกิดข้อผิดพลาด: {str(e)}"


def clean_question(text):
    return re.sub(r'<@[A-Z0-9]+>', '', text).strip()


@slack_events_adapter.on("app_mention")
def handle_mention(event_data):
    event_id = event_data.get("event_id")
    if event_id in processed_events:
        return
    processed_events.add(event_id)

    event = event_data["event"]
    question = clean_question(event["text"])
    channel = event["channel"]
    print(f">>> คำถาม: {question}")

    slack_client.chat_postMessage(channel=channel, text="กำลังประมวลผล รอสักครู่ครับ...")

    def process():
        answer = run_full_workflow(question)
        slack_client.chat_postMessage(channel=channel, text=answer)

    threading.Thread(target=process).start()


if __name__ == "__main__":
    app.run(port=3000)

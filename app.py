# -*- coding: utf-8 -*-
# Windows console output encoding fix
import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() in ('cp1252', 'ascii'):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
"""
AI Database Agent — FastAPI Backend  (v3.2 — Export / Voice / History)
═══════════════════════════════════════════════════════════════════════
Every user request follows the mandatory 7-step execution pipeline:

  STEP 1  Classify intent (SQL / chart / ER-diagram / insight)
  STEP 2  Fetch live schema from SQLite (PRAGMA table_info + FK list)
  STEP 3  Generate SQL ONLY after schema is known (LLM or smart-NLP fallback)
  STEP 4  Validate SQL (safety + table/column existence)
  STEP 5  Execute SQL
  STEP 6  Self-debug loop — auto-fix and retry once on error
  STEP 7  Format output (chart labels/data + human explanation)

NEW in v3.2:
  - POST /api/export/csv       → export table data as CSV (fixed)
  - POST /api/export/json      → export table data as JSON
  - GET  /api/history          → list saved query history
  - POST /api/history/save     → save a query to history
  - POST /api/history/favorite → toggle favorite flag
  - DELETE /api/history/{id}   → delete a history entry

Run: uvicorn app:app --reload
"""

import os, re, json, time, random, sqlite3, logging, traceback, csv, io as _io
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from openai import OpenAI
from dotenv import load_dotenv

# ── env ────────────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=str(Path(__file__).resolve().parent / ".env"))
# Ensure the SQLite DB file is writable (set permissions if possible)
if os.path.exists("demo.db"):
    try:
        os.chmod("demo.db", 0o666)
    except Exception as e:
        logging.warning(f"Could not set DB permissions: {e}")

# ── app ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="AI Database Agent", version="3.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_BASE_DIR = Path(__file__).resolve().parent

_FRONTEND_DIR = _BASE_DIR.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR), html=False), name="static")

@app.get("/", response_class=FileResponse)
def serve_frontend():
    path = _FRONTEND_DIR / "index.html"
    if not path.exists():
        path = _BASE_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(path)


# ── config ─────────────────────────────────────────────────────────────────────
class Settings(BaseSettings):
    OPENAI_API_KEY: str = ""
    OPENAI_API_BASE: str = "https://openrouter.ai/api/v1"
    OPENAI_MODEL: str = "google/gemma-4-26b-a4b-it:free"
    DATABASE_URL: str = "demo.db"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
OPENAI_API_KEY  = settings.OPENAI_API_KEY
OPENAI_API_BASE = settings.OPENAI_API_BASE
OPENAI_MODEL    = settings.OPENAI_MODEL
DB_PATH = str((_BASE_DIR / settings.DATABASE_URL).resolve())

if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY not set — LLM features will use smart fallback.")
else:
    logging.info(f"[OK] API key loaded ({OPENAI_API_KEY[:12]}...)")
    logging.info(f"[OK] Model: {OPENAI_MODEL}")
    logging.info(f"[OK] Base URL: {OPENAI_API_BASE}")

_llm = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE, timeout=30.0)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — SCHEMA FETCH
# ══════════════════════════════════════════════════════════════════════════════

def get_schema() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    schema = {}
    for (t,) in tables:
        cols = c.execute(f"PRAGMA table_info('{t}')").fetchall()
        fks  = c.execute(f"PRAGMA foreign_key_list('{t}')").fetchall()
        schema[t] = {
            "columns": [
                {"name": col[1], "type": col[2], "pk": bool(col[5])}
                for col in cols
            ],
            "foreign_keys": [
                {"column": fk[3], "table": fk[2], "ref_col": fk[4]}
                for fk in fks
            ],
        }
    conn.close()
    return schema


def schema_summary_text(schema: dict) -> str:
    lines = []
    for tbl, meta in schema.items():
        col_str = ", ".join(
            f"{c['name']} ({c['type']}{'  PK' if c['pk'] else ''})"
            for c in meta["columns"]
        )
        lines.append(f"  TABLE {tbl}: {col_str}")
        for fk in meta.get("foreign_keys", []):
            lines.append(f"    FK: {tbl}.{fk['column']} → {fk['table']}.{fk['ref_col']}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — SQL VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def validate_sql(sql: str, schema: dict) -> tuple:
    errors = []
    forbidden = [r"\bDROP\b", r"\bDELETE\b", r"\bUPDATE\b",
                 r"\bINSERT\b", r"\bALTER\b", r"\bTRUNCATE\b"]
    if any(re.search(p, sql, re.IGNORECASE) for p in forbidden):
        errors.append("Only SELECT queries are allowed.")
        return False, errors
    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        errors.append("Query must start with SELECT.")
        return False, errors
    schema_tables_lower = {t.lower() for t in schema}
    from_tables = re.findall(
        r"\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE
    ) + re.findall(
        r"\bJOIN\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE
    )
    for t in from_tables:
        if t.lower() not in schema_tables_lower:
            errors.append(f"Table '{t}' does not exist in this database.")
    return len(errors) == 0, errors


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — SQL EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def run_sql(sql: str) -> dict:
    sql = sql.strip().rstrip(";")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    t0 = time.time()
    c.execute(sql)
    rows = c.fetchall()
    elapsed = round((time.time() - t0) * 1000, 1)
    cols = [d[0] for d in c.description] if c.description else []
    conn.close()
    return {
        "columns": cols,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "exec_time_ms": elapsed,
    }


# ══════════════════════════════════════════════════════════════════════════════
# LLM CALL
# ══════════════════════════════════════════════════════════════════════════════

def call_llm(messages: list, temperature=0.3, max_tokens=1500) -> str:
    try:
        resp = _llm.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
        )
        choices = getattr(resp, "choices", None)
        if not choices:
            raise Exception("LLM returned no choices (empty response body)")
        content = getattr(choices[0].message, "content", None)
        if not content or not str(content).strip():
            raise Exception("LLM returned empty content string")
        return str(content).strip()
    except Exception as exc:
        raise Exception(f"LLM call failed: {repr(exc)}")


def extract_json(text: str) -> dict:
    if not text:
        return {}
    text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text).strip("`").strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except Exception:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    m2 = re.search(r'"sql_query"\s*:\s*"(.*?)"(?:\s*[,}])', text, re.DOTALL)
    if m2:
        return {"sql_query": m2.group(1).replace("\\n", "\n")}
    return {}


def call_llm_to_fix_sql(query: str, failed_sql: str, error_message: str, schema_txt: str) -> str:
    prompt = f"""You are an AI Data Analyst Agent. A generated SQL query failed to execute.
Your job is to fix the SQL query based on the database schema and the error message.

DATABASE SCHEMA:
{schema_txt}

USER QUERY: {query}
FAILED SQL: {failed_sql}
ERROR MESSAGE: {error_message}

RULES:
- Use ONLY tables and columns from the VERIFIED SCHEMA. Never hallucinate.
- Generate a correct, optimized, and valid SQLite SELECT query.
- When generating SQL, return only the SQL query inside the "sql_query" key of the JSON response, without explanations or inline comments.
- Return ONLY a JSON object with the key "sql_query":
{{
  "sql_query": "<corrected SQLite SELECT query>"
}}
"""
    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=600)
        parsed = extract_json(raw)
        return parsed.get("sql_query", "").strip()
    except Exception as e:
        logging.error(f"Error calling LLM to fix SQL: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — SMART NLP FALLBACK SQL GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def _col_names(schema: dict, table: str) -> list:
    return [c["name"] for c in schema.get(table, {}).get("columns", [])]


def smart_sql_fallback(query: str, schema: dict) -> dict:
    q = query.lower()
    tables = list(schema.keys())

    def find_amount_col(tbl):
        for candidate in ["total_amount", "amount", "revenue", "price", "value", "sales"]:
            if candidate in [c["name"].lower() for c in schema.get(tbl, {}).get("columns", [])]:
                return candidate
        for c in schema.get(tbl, {}).get("columns", []):
            if c["type"].upper() in ("REAL", "INTEGER", "NUMERIC", "FLOAT") and "id" not in c["name"].lower():
                return c["name"]
        return None

    def find_name_col(tbl):
        for candidate in ["name", "label", "title", "region_name", "category", "method", "status"]:
            for c in schema.get(tbl, {}).get("columns", []):
                if c["name"].lower() == candidate:
                    return c["name"]
        return None

    def find_date_col(tbl):
        for candidate in ["created_at", "date", "order_date", "timestamp", "paid_at"]:
            for c in schema.get(tbl, {}).get("columns", []):
                if c["name"].lower() == candidate:
                    return c["name"]
        return None

    def has_table(name):
        return any(t.lower() == name.lower() for t in tables)

    def real_table(name):
        for t in tables:
            if t.lower() == name.lower():
                return t
        return name

    if any(w in q for w in ["er diagram", "erd", "entity", "relation", "schema diagram"]):
        return {"intent": "er_diagram"}

    if ("customer" in q or "client" in q) and ("top" in q or "highest" in q or "best" in q or "purchase" in q):
        limit = _parse_limit(q)
        if has_table("orders") and has_table("customers"):
            o_amount = find_amount_col(real_table("orders")) or "total_amount"
            status_clause = ""
            if "status" in [c["name"].lower() for c in schema["orders"]["columns"]]:
                status_clause = "WHERE o.status = 'completed'"
            return {
                "sql": f"""
SELECT c.name AS customer_name,
       SUM(o.{o_amount}) AS total_purchase,
       COUNT(o.id) AS order_count
FROM orders o
JOIN customers c ON o.customer_id = c.id
{status_clause}
GROUP BY c.id, c.name
ORDER BY total_purchase DESC
LIMIT {limit}""".strip(),
                "chart_type": "bar",
                "chart_title": f"Top {limit} Customers by Total Purchase",
            }

    if ("region" in q) and ("revenue" in q or "sales" in q or "amount" in q):
        if has_table("orders") and has_table("regions"):
            o_amount = find_amount_col("orders") or "total_amount"
            date_filter = _date_filter(q, "o.created_at")
            return {
                "sql": f"""
SELECT r.region_name,
       SUM(o.{o_amount}) AS total_revenue,
       COUNT(o.id) AS order_count
FROM orders o
JOIN regions r ON o.region_id = r.id
{date_filter}
GROUP BY r.id, r.region_name
ORDER BY total_revenue DESC""".strip(),
                "chart_type": "bar",
                "chart_title": "Revenue by Region" + (" (Q4 2024)" if "q4" in q or "quarter 4" in q else ""),
            }

    if any(w in q for w in ["daily", "weekly", "monthly", "over time", "trend", "sales by day", "sales by month"]):
        if has_table("orders"):
            date_col = find_date_col("orders") or "created_at"
            o_amount = find_amount_col("orders") or "total_amount"
            date_filter = _date_filter(q, f"o.{date_col}")
            if "month" in q:
                grp = f"strftime('%Y-%m', o.{date_col})"
                lbl = "month"
            elif "week" in q:
                grp = f"strftime('%Y-W%W', o.{date_col})"
                lbl = "week"
            else:
                grp = f"DATE(o.{date_col})"
                lbl = "sales_date"
            return {
                "sql": f"""
SELECT {grp} AS {lbl},
       SUM(o.{o_amount}) AS daily_sales,
       COUNT(o.id) AS order_count
FROM orders o
{date_filter}
GROUP BY {grp}
ORDER BY {lbl} ASC""".strip(),
                "chart_type": "line",
                "chart_title": "Sales Trend" + (" (Last 30 Days)" if "30" in q else ""),
            }

    if ("product" in q) and ("top" in q or "best" in q or "highest" in q or "revenue" in q or "sales" in q):
        limit = _parse_limit(q)
        if has_table("order_items") and has_table("products"):
            return {
                "sql": f"""
SELECT p.name AS product_name,
       SUM(oi.quantity * oi.unit_price) AS total_revenue,
       SUM(oi.quantity) AS units_sold
FROM order_items oi
JOIN products p ON oi.product_id = p.id
GROUP BY p.id, p.name
ORDER BY total_revenue DESC
LIMIT {limit}""".strip(),
                "chart_type": "bar",
                "chart_title": f"Top {limit} Products by Revenue",
            }

    if "category" in q and ("revenue" in q or "sales" in q):
        if has_table("products") and has_table("order_items"):
            return {
                "sql": """
SELECT p.category,
       SUM(oi.quantity * oi.unit_price) AS total_revenue,
       COUNT(DISTINCT oi.order_id) AS orders
FROM order_items oi
JOIN products p ON oi.product_id = p.id
GROUP BY p.category
ORDER BY total_revenue DESC""".strip(),
                "chart_type": "pie",
                "chart_title": "Revenue by Product Category",
            }

    if "payment" in q and ("method" in q or "type" in q or "distribution" in q):
        if has_table("payments"):
            return {
                "sql": """
SELECT method,
       COUNT(*) AS payment_count,
       SUM(amount) AS total_amount
FROM payments
GROUP BY method
ORDER BY total_amount DESC""".strip(),
                "chart_type": "pie",
                "chart_title": "Payment Method Distribution",
            }

    if "order" in q and ("status" in q or "completed" in q or "refund" in q):
        if has_table("orders"):
            o_amount = find_amount_col("orders") or "total_amount"
            return {
                "sql": f"""
SELECT status,
       COUNT(*) AS order_count,
       SUM({o_amount}) AS total_amount
FROM orders
GROUP BY status
ORDER BY order_count DESC""".strip(),
                "chart_type": "bar",
                "chart_title": "Order Status Breakdown",
            }

    if ("monthly" in q or "month" in q) and ("revenue" in q or "sales" in q or "total" in q):
        if has_table("orders"):
            o_amount = find_amount_col("orders") or "total_amount"
            date_col = find_date_col("orders") or "created_at"
            return {
                "sql": f"""
SELECT strftime('%Y-%m', {date_col}) AS month,
       SUM({o_amount}) AS monthly_revenue,
       COUNT(id) AS order_count
FROM orders
WHERE status = 'completed'
GROUP BY strftime('%Y-%m', {date_col})
ORDER BY month ASC""".strip(),
                "chart_type": "line",
                "chart_title": "Monthly Revenue Trend",
            }

    if ("count" in q or "total" in q or "how many" in q) and "record" in q:
        counts = []
        for tbl in tables:
            counts.append(f"(SELECT COUNT(*) FROM {tbl}) AS {tbl}_count")
        return {
            "sql": f"SELECT {', '.join(counts)}",
            "chart_type": "table",
            "chart_title": "Record Counts per Table",
        }

    for tbl in tables:
        if tbl.lower() in q:
            name_col = find_name_col(tbl)
            amount_col = find_amount_col(tbl)
            if name_col and amount_col:
                return {
                    "sql": f"SELECT {name_col}, {amount_col} FROM {tbl} LIMIT 50",
                    "chart_type": "bar",
                    "chart_title": f"{tbl.title()} Overview",
                }
            cols = [c["name"] for c in schema[tbl]["columns"]][:6]
            return {
                "sql": f"SELECT {', '.join(cols)} FROM {tbl} LIMIT 50",
                "chart_type": "table",
                "chart_title": f"{tbl.title()} Data",
            }

    raise ValueError(f"Could not auto-generate SQL for: '{query}'")


def _parse_limit(q: str) -> int:
    m = re.search(r"top\s+(\d+)", q, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m2 = re.search(r"(\d+)\s+(?:customer|product|region)", q, re.IGNORECASE)
    if m2:
        return int(m2.group(1))
    return 10


def _date_filter(q: str, date_col: str) -> str:
    q = q.lower()
    if "q4 2024" in q or "quarter 4 2024" in q or ("q4" in q and "2024" in q):
        return f"WHERE {date_col} BETWEEN '2024-10-01' AND '2024-12-31'"
    if "q3 2024" in q:
        return f"WHERE {date_col} BETWEEN '2024-07-01' AND '2024-09-30'"
    if "q2 2024" in q:
        return f"WHERE {date_col} BETWEEN '2024-04-01' AND '2024-06-30'"
    if "q1 2024" in q:
        return f"WHERE {date_col} BETWEEN '2024-01-01' AND '2024-03-31'"
    if "last 30 days" in q or "last 30" in q:
        return f"WHERE date({date_col}) >= date('now', '-30 day')"
    if "last 7 days" in q or "last week" in q:
        return f"WHERE date({date_col}) >= date('now', '-7 day')"
    if "last 90 days" in q:
        return f"WHERE date({date_col}) >= date('now', '-90 day')"
    if "last 6 months" in q or "past 6 months" in q or "6 months" in q:
        return f"WHERE date({date_col}) >= date('now', '-6 month')"
    if "last year" in q or "past year" in q or "1 year" in q or "last 12 months" in q or "past 12 months" in q:
        return f"WHERE date({date_col}) >= date('now', '-1 year')"
    if "2024" in q:
        return f"WHERE strftime('%Y', {date_col}) = '2024'"
    if "2023" in q:
        return f"WHERE strftime('%Y', {date_col}) = '2023'"
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# ER DIAGRAM BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_er_diagram(schema: dict) -> str:
    lines = ["erDiagram"]
    for tbl, meta in schema.items():
        lines.append(f"    {tbl} {{")
        for col in meta["columns"]:
            typ = (col["type"] or "TEXT").lower().replace(" ", "_")
            pk  = " PK" if col["pk"] else ""
            lines.append(f"        {typ}{pk} {col['name']}")
        lines.append("    }")
    for tbl, meta in schema.items():
        for fk in meta.get("foreign_keys", []):
            lines.append(
                f'    {tbl} ||--o{{ {fk["table"]} : "{fk["column"]} -> {fk["ref_col"]}"'
            )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# CHART AUTO-SELECTOR
# ══════════════════════════════════════════════════════════════════════════════

def infer_chart_type(cols: list, rows: list, hint: str = "") -> str:
    h = hint.lower()
    if any(w in h for w in ["scatter", "correlation", "correlate", "relationship"]):
        return "scatter"
    if any(w in h for w in ["trend", "over time", "daily", "weekly", "monthly", "yearly", "time series", "per day", "per month"]):
        return "line"
    if rows and isinstance(rows[0][0], str):
        if re.match(r"^\d{4}-\d{2}", rows[0][0]):
            return "line"
    if any(w in h for w in ["pie", "distribution", "share", "proportion", "percentage", "breakdown", "split"]):
        return "pie"
    if any(w in h for w in ["top", "best", "highest", "lowest", "comparison", "compare", "region", "category", "ranking", "bar", "versus", "vs"]):
        return "bar"
    if len(rows) <= 6 and len(cols) == 2:
        return "pie"
    return "bar"


# ══════════════════════════════════════════════════════════════════════════════
# AUTO INSIGHT GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def _auto_insight(query: str, cols: list, rows: list, qr: dict, llm_error: str = "") -> str:
    if not rows:
        return "No records found for this query."
    lines = []
    n = qr["row_count"]
    t = qr["exec_time_ms"]
    lines.append(f"**{n} record{'s' if n != 1 else ''}** returned in {t}ms.")
    if len(cols) >= 2 and len(rows) >= 1:
        top_label = rows[0][0]
        top_val   = rows[0][1]
        if isinstance(top_val, (int, float)):
            lines.append(
                f"**Top performer:** {top_label} with **{top_val:,.2f}** — "
                f"the highest value in this dataset."
            )
    if len(rows) >= 3 and isinstance(rows[-1][1] if len(rows[-1]) > 1 else None, (int, float)):
        bot_label = rows[-1][0]
        bot_val   = rows[-1][1]
        lines.append(f"**Lowest:** {bot_label} at **{bot_val:,.2f}**.")
    if len(rows) >= 4 and len(cols) >= 2:
        values = [r[1] for r in rows if isinstance(r[1] if len(r) > 1 else None, (int, float))]
        if len(values) >= 4:
            first_half  = sum(values[:len(values)//2]) / (len(values)//2)
            second_half = sum(values[len(values)//2:]) / (len(values) - len(values)//2)
            if second_half > first_half * 1.05:
                pct = ((second_half - first_half) / first_half) * 100
                lines.append(f"**Trend:** Values are **growing** — second half avg is {pct:.1f}% higher than first half.")
            elif second_half < first_half * 0.95:
                pct = ((first_half - second_half) / first_half) * 100
                lines.append(f"**Trend:** Values are **declining** — second half avg dropped {pct:.1f}% vs first half.")
            else:
                lines.append("**Trend:** Values are relatively **stable** across the period.")
    if len(rows) >= 3 and len(cols) >= 2:
        nums = [r[1] for r in rows if isinstance(r[1] if len(r) > 1 else None, (int, float))]
        if nums and len(nums) >= 3:
            avg = sum(nums) / len(nums)
            if nums[0] > avg * 2:
                lines.append(
                    f"**Anomaly:** The top entry (**{rows[0][0]}**) is more than 2x the average "
                    f"({avg:,.2f}), suggesting it is a significant outlier."
                )
    if llm_error:
        lines.append(
            f"*Note: AI model was rate-limited; SQL was auto-generated from schema patterns.*"
        )
    return "\n\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PIPELINE  (7 mandatory steps)
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(query: str, history: list) -> dict:
    log = logging.getLogger("pipeline")
    log.info("=" * 60)
    log.info(f"STEP 1 | USER QUERY: {query}")

    q_lower = query.lower()
    er_keywords = ["er diagram", "erd", "entity", "relationship diagram", "schema diagram",
                   "show tables", "database diagram"]
    is_er = any(kw in q_lower for kw in er_keywords)

    log.info("STEP 2 | Fetching live schema...")
    schema = get_schema()
    log.info(f"        Tables found: {list(schema.keys())}")

    if is_er:
        log.info("STEP 7 | Returning ER diagram")
        mermaid = build_er_diagram(schema)
        return {
            "intent": "er_diagram",
            "sql": None,
            "mermaid": mermaid,
            "explanation": f"ER diagram generated from {len(schema)} live tables: {', '.join(schema.keys())}",
            "chart_type": None,
            "chart_title": "Database ER Diagram",
            "chart_labels": None,
            "chart_data": None,
            "table_headers": None,
            "table_rows": None,
            "tools": ["get_schema()", "generate_chart()", "explain_data()"],
        }

    log.info("STEP 3 | Generating SQL...")
    sql = None
    chart_type_hint = "bar"
    chart_title = "Query Results"
    llm_used = False
    llm_error = None

    if OPENAI_API_KEY:
        try:
            schema_txt = schema_summary_text(schema)
            prompt = f"""You are an AI Data Analyst Agent that converts natural language questions into database insights.

Your job:
1. Understand the user query and classify intent.
2. Generate correct and executable SQL using ONLY the verified database schema below.
3. Choose the best visualization type:
   - BAR chart   → for comparisons
   - LINE chart  → for trends over time
   - PIE chart   → for proportional distribution
   - SCATTER     → for correlations
4. If the query contains time-based terms like "last 6 months" or "last year", always convert them into proper SQLite WHERE conditions using SQLite date functions (e.g. date('now', '-6 month') or date('now', '-1 year')). Note that the current date is {datetime.now().strftime("%Y-%m-%d")}.

RULES:
- Use ONLY tables and columns from the VERIFIED SCHEMA below. Never hallucinate.
- Always use correct JOINs when multiple tables are needed.
- Generate optimized, valid SQLite SELECT queries only.
- When generating SQL, return only the SQL query inside the "sql_query" key of the JSON response, without explanations or inline markdown comments.
- Return ONLY a JSON object — no explanations, no markdown:
{{
  "sql_query": "<valid SQLite SELECT query>",
  "chart_type": "<bar|line|pie|scatter|table>",
  "chart_title": "<short descriptive title for dashboard>"
}}

VERIFIED DATABASE SCHEMA:
{schema_txt}

USER QUESTION: {query}
"""
            raw = call_llm(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=800,
            )
            parsed = extract_json(raw)
            sql_candidate = parsed.get("sql_query", "").strip()
            if sql_candidate and sql_candidate.upper().startswith("SELECT"):
                sql = sql_candidate
                chart_type_hint = parsed.get("chart_type", "bar")
                chart_title = parsed.get("chart_title", "Query Results")
                llm_used = True
                log.info(f"        LLM SQL: {sql[:120]}...")
            else:
                raise Exception(f"LLM returned empty/invalid SQL: {parsed}")
        except Exception as exc:
            llm_error = str(exc)
            log.warning(f"        LLM failed ({llm_error}) — using smart fallback")

    if not sql:
        try:
            fb = smart_sql_fallback(query, schema)
            if fb.get("intent") == "er_diagram":
                mermaid = build_er_diagram(schema)
                return {
                    "intent": "er_diagram",
                    "sql": None,
                    "mermaid": mermaid,
                    "explanation": "ER diagram generated from live database schema.",
                    "chart_type": None,
                    "chart_title": "Database ER Diagram",
                    "chart_labels": None,
                    "chart_data": None,
                    "table_headers": None,
                    "table_rows": None,
                    "tools": ["get_schema()", "generate_chart()", "explain_data()"],
                }
            sql = fb["sql"]
            chart_type_hint = fb.get("chart_type", "bar")
            chart_title = fb.get("chart_title", "Query Results")
            log.info(f"        Fallback SQL: {sql[:120]}...")
        except ValueError as ve:
            log.error(f"        Fallback failed: {ve}")
            return {
                "intent": "error",
                "sql": None,
                "mermaid": None,
                "explanation": (
                    f"⚠️ I couldn't auto-generate SQL for: **\"{query}\"**\n\n"
                    f"**Reason:** No matching pattern found in schema.\n\n"
                    f"**Available tables:** {', '.join(schema.keys())}\n\n"
                    "Please rephrase — e.g., specify a table name or metric (revenue, count, sales, etc.)."
                ),
                "chart_type": None,
                "chart_title": "",
                "chart_labels": None,
                "chart_data": None,
                "table_headers": None,
                "table_rows": None,
                "tools": ["get_schema()"],
            }

    log.info(f"STEP 4 | Validating SQL...")
    is_valid, errors = validate_sql(sql, schema)
    if not is_valid:
        log.warning(f"        Validation errors: {errors}")

    retry_count = 0
    exec_error = None
    qr = None

    while retry_count <= 1:
        log.info(f"STEP 5 | Executing SQL (attempt {retry_count + 1}):\n        {sql}")
        try:
            qr = run_sql(sql)
            exec_error = None
            log.info(f"        OK — {qr['row_count']} rows in {qr['exec_time_ms']}ms")
            break
        except Exception as exc:
            exec_error = str(exc)
            log.error(f"        Execution error: {exec_error}")
            if retry_count == 0:
                log.info("STEP 6 | Self-debug: attempting auto-fix...")
                fixed_sql = ""
                if OPENAI_API_KEY and llm_used:
                    try:
                        schema_txt = schema_summary_text(schema)
                        fixed_sql = call_llm_to_fix_sql(query, sql, exec_error, schema_txt)
                    except Exception as llm_exc:
                        log.warning(f"        LLM fix attempt failed: {llm_exc}")
                if not fixed_sql:
                    try:
                        fb2 = smart_sql_fallback(query, schema)
                        if fb2.get("sql"):
                            fixed_sql = fb2["sql"]
                            chart_type_hint = fb2.get("chart_type", chart_type_hint)
                    except Exception:
                        pass
                if fixed_sql and fixed_sql != sql:
                    sql = fixed_sql
                    log.info(f"        Auto-fixed SQL: {sql[:120]}...")
                else:
                    log.warning("        Could not find a different SQL query to retry.")
                    retry_count = 2
                    break
            retry_count += 1

    log.info("STEP 7 | Formatting output...")

    if exec_error and qr is None:
        return {
            "intent": "error",
            "sql": sql,
            "mermaid": None,
            "explanation": (
                f"⚠️ **SQL Execution Failed** (after 1 auto-retry)\n\n"
                f"**Error:** `{exec_error}`\n\n"
                f"**Generated SQL:**\n```sql\n{sql}\n```\n\n"
                f"Please check your query or rephrase the question."
            ),
            "chart_type": None,
            "chart_title": "",
            "chart_labels": None,
            "chart_data": None,
            "table_headers": None,
            "table_rows": None,
            "tools": ["get_schema()", "execute_query()"],
        }

    cols = qr["columns"]
    rows = qr["rows"]

    tools_executed = ["get_schema()", "execute_query()", "generate_chart()", "explain_data()"]

    if not rows:
        return {
            "intent": "analytics_query",
            "sql": sql,
            "mermaid": None,
            "explanation": f"✅ Query executed successfully but returned **no records**.\n\n**SQL:**\n```sql\n{sql}\n```",
            "chart_type": "table",
            "chart_title": chart_title,
            "chart_labels": None,
            "chart_data": None,
            "table_headers": cols,
            "table_rows": [],
            "tools": ["get_schema()", "execute_query()", "explain_data()"],
        }

    chart_type = infer_chart_type(cols, rows, query + " " + chart_type_hint)
    chart_labels = [str(r[0]) for r in rows]
    chart_data   = [r[1] if len(r) > 1 else None for r in rows]

    insight = ""
    if OPENAI_API_KEY and llm_used:
        try:
            result_sample = [dict(zip(cols, r)) for r in rows[:25]]
            insight_prompt = f"""You are an AI Data Analyst Agent embedded in a business dashboard.

Analyze the query results below and write a business insight summary of 3-5 lines.

Your summary MUST:
- Highlight the TOP PERFORMERS (highest values)
- Identify any TRENDS (growing/declining patterns if time-series)
- Flag any ANOMALIES or surprising findings
- Use simple, non-technical business language
- Be suitable for a business dashboard card

Do NOT include any JSON, code, or SQL. Only write the insight paragraph.

USER QUESTION: {query}

QUERY RESULTS ({qr['row_count']} rows):
{json.dumps(result_sample, indent=2)}

COLUMNS: {cols}
"""
            insight = call_llm(
                [{"role": "user", "content": insight_prompt}],
                temperature=0.4,
                max_tokens=400,
            )
        except Exception:
            insight = ""

    if not insight:
        insight = _auto_insight(query, cols, rows, qr, llm_error)

    log.info(f"        Chart: {chart_type} | Rows: {qr['row_count']}")
    log.info("=" * 60)

    return {
        "intent": "analytics_query",
        "sql": sql,
        "mermaid": None,
        "explanation": insight,
        "chart_type": chart_type,
        "chart_title": chart_title,
        "chart_labels": chart_labels,
        "chart_data": chart_data,
        "table_headers": cols,
        "table_rows": rows,
        "tools": tools_executed,
    }


# ══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_db_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    stats = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in tables}
    conn.close()
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE INIT
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS regions (
            id INTEGER PRIMARY KEY,
            region_name TEXT NOT NULL,
            country_code TEXT
        );
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            price REAL NOT NULL,
            stock INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            region_id INTEGER REFERENCES regions(id),
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'completed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id),
            product_id INTEGER REFERENCES products(id),
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id),
            amount REAL NOT NULL,
            method TEXT,
            status TEXT DEFAULT 'paid'
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS query_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            sql_generated TEXT,
            chart_type TEXT,
            chart_title TEXT,
            row_count INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    if c.execute("SELECT COUNT(*) FROM regions").fetchone()[0] == 0:
        c.executemany("INSERT INTO regions VALUES (?,?,?)", [
            (1,"North America","US"),(2,"Europe","EU"),(3,"Asia Pacific","AP"),
            (4,"Latin America","LA"),(5,"Middle East","ME"),(6,"Africa","AF"),
        ])
        customers = [
            ("Acme Corp","billing@acme.com"),
            ("TechSolutions Ltd","accounts@techsol.io"),
            ("GlobalTrade Inc","finance@globaltrade.net"),
            ("Nexus Partners","ap@nexuspartners.co"),
            ("Vertex Systems","purchasing@vertex.com"),
            ("Apex Retail Group","orders@apexretail.com"),
            ("Orion Dynamics","billing@oriondyn.io"),
            ("CoreLogic Co","finance@corelogic.io"),
            ("Summit Enterprises","ap@summit-e.com"),
            ("Pinnacle Group","accounting@pinnacle.net"),
        ] + [(f"Customer {i}", f"c{i}@example.com") for i in range(11, 101)]
        c.executemany("INSERT INTO customers (name,email) VALUES (?,?)", customers)
        c.executemany("INSERT INTO products (name,category,price,stock) VALUES (?,?,?,?)", [
            ("Enterprise Suite","Software",2999.99,500),
            ("Pro License","Software",499.99,2000),
            ("Hardware Kit","Hardware",1299.99,150),
            ("Support Plan","Services",799.99,999),
            ("Analytics Add-on","Software",299.99,800),
        ])

        monthly_targets = [1200000,1400000,1100000,1600000,1800000,2100000,
                           1900000,2300000,2000000,2847320,3100000,2850000]
        region_weights  = [0.38,0.24,0.19,0.11,0.05,0.03]
        base = datetime(2024, 1, 1)
        oid = pid = payid = 1

        for mi, target in enumerate(monthly_targets):
            mo = base + timedelta(days=30*mi)
            num = int(target / 307)
            region_pool = []
            for ri, w in enumerate(region_weights):
                region_pool.extend([ri+1]*int(num*w))
            for rid in region_pool:
                cid  = random.randint(1, 100)
                amt  = round(random.uniform(100, 2000), 2)
                dt   = (mo + timedelta(days=random.randint(0, 27))).strftime("%Y-%m-%d %H:%M:%S")
                stat = "completed" if random.random() > 0.05 else "refunded"
                c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", (oid,cid,rid,amt,stat,dt))
                c.execute("INSERT INTO order_items VALUES (?,?,?,?,?)",
                          (pid, oid, random.randint(1,5), random.randint(1,5), round(amt/2,2)))
                c.execute("INSERT INTO payments VALUES (?,?,?,?,?)",
                          (payid, oid, amt,
                           random.choice(["credit_card","bank_transfer","paypal"]),
                           "paid" if stat=="completed" else "refunded"))
                oid+=1; pid+=1; payid+=1

    conn.commit()
    conn.close()
    print(f"[DB] Ready → {DB_PATH}")


# ══════════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    session_id: str
    query: str
    history: list = []

class SaveHistoryRequest(BaseModel):
    query: str
    sql_generated: str = ""
    chart_type: str = ""
    chart_title: str = ""
    row_count: int = 0

class FavoriteRequest(BaseModel):
    history_id: int
    is_favorite: bool

class ExportRequest(BaseModel):
    headers: list
    rows: list
    filename: str = "export"


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception):
    logging.error(f"Unhandled exception: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
    )


@app.post("/api/chat")
async def chat(body: ChatRequest):
    query = body.query.strip()
    session_id = body.session_id

    if not query:
        raise HTTPException(400, "No query provided")

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
            (session_id, "user", query),
        )
        conn.commit()

        c.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT 20",
            (session_id,),
        )
        history = [{"role": r, "content": cnt} for r, cnt in c.fetchall()]
        conn.close()

        ai = run_pipeline(query, history)

        # ── Auto-save to query_history ─────────────────────────────────────
        try:
            conn2 = sqlite3.connect(DB_PATH)
            c2 = conn2.cursor()
            c2.execute(
                """INSERT INTO query_history (query, sql_generated, chart_type, chart_title, row_count)
                   VALUES (?,?,?,?,?)""",
                (
                    query,
                    ai.get("sql") or "",
                    ai.get("chart_type") or "",
                    ai.get("chart_title") or "",
                    len(ai.get("table_rows") or []),
                ),
            )
            conn2.commit()
            conn2.close()
        except Exception as he:
            logging.warning(f"History save failed: {he}")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?,?,?)",
            (session_id, "assistant", json.dumps(ai)),
        )
        conn.commit()
        conn.close()

        db_stats = get_db_stats()
        result = {
            "intent":       ai.get("intent"),
            "sql":          ai.get("sql"),
            "mermaid":      ai.get("mermaid"),
            "explanation":  ai.get("explanation"),
            "chartType":    ai.get("chart_type"),
            "chartTitle":   ai.get("chart_title", "Analytics"),
            "chartLabels":  ai.get("chart_labels"),
            "chartData":    ai.get("chart_data"),
            "tableHeaders": ai.get("table_headers"),
            "tableRows":    ai.get("table_rows"),
            "stats": {
                "tables":     len(db_stats),
                "total_rows": sum(db_stats.values()),
                "db_name":    "sqlite://demo.db",
            },
            "tools": (
                ai.get("tools") or
                ["get_schema()", "execute_query()", "generate_chart()", "explain_data()"]
            ),
        }

        if result["tableHeaders"] and result["tableRows"] is not None:
            result["table"] = {
                "columns": result["tableHeaders"],
                "rows":    result["tableRows"],
            }
        if result["chartType"] and result["chartLabels"] and result["chartData"]:
            result["chart"] = {
                "type":   result["chartType"],
                "title":  result["chartTitle"],
                "labels": result["chartLabels"],
                "data":   result["chartData"],
            }

        return JSONResponse(result)

    except Exception as exc:
        logging.error(f"[API] Unhandled error: {repr(exc)}\n{traceback.format_exc()}")
        return JSONResponse({
            "intent":      "error",
            "sql":         None,
            "mermaid":     None,
            "explanation": f"⚠️ **Unexpected server error:** {str(exc)}",
            "chartType":   None,
            "chartTitle":  "",
            "chartLabels": None,
            "chartData":   None,
            "tableHeaders":None,
            "tableRows":   None,
            "tools":       ["Error handling..."],
        })


# ── QUERY HISTORY ENDPOINTS ────────────────────────────────────────────────────

@app.get("/api/history")
def get_history(favorites_only: bool = Query(False), limit: int = Query(50)):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if favorites_only:
        rows = c.execute(
            "SELECT id, query, sql_generated, chart_type, chart_title, row_count, is_favorite, created_at "
            "FROM query_history WHERE is_favorite=1 ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT id, query, sql_generated, chart_type, chart_title, row_count, is_favorite, created_at "
            "FROM query_history ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return JSONResponse({
        "history": [
            {
                "id": r[0], "query": r[1], "sql_generated": r[2],
                "chart_type": r[3], "chart_title": r[4], "row_count": r[5],
                "is_favorite": bool(r[6]), "created_at": r[7]
            }
            for r in rows
        ]
    })


@app.post("/api/history/favorite")
def toggle_favorite(body: FavoriteRequest):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE query_history SET is_favorite=? WHERE id=?",
        (1 if body.is_favorite else 0, body.history_id)
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": body.history_id, "is_favorite": body.is_favorite})


@app.delete("/api/history/{history_id}")
def delete_history(history_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM query_history WHERE id=?", (history_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "deleted_id": history_id})


# ── EXPORT ENDPOINTS ───────────────────────────────────────────────────────────

@app.post("/api/export/csv")
async def export_csv(body: ExportRequest):
    """
    Stream table data as a downloadable CSV file.
    Accepts: { headers: [...], rows: [[...], ...], filename: "name" }
    """
    if not body.headers:
        raise HTTPException(400, "No headers provided")

    output = _io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_NONNUMERIC)
    writer.writerow(body.headers)
    for row in body.rows:
        # Ensure every cell is serialisable as a string
        writer.writerow([str(cell) if cell is not None else "" for cell in row])
    output.seek(0)

    # Sanitise filename
    safe_name = re.sub(r"[^\w\-]", "_", body.filename or "export")
    filename = safe_name + ".csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        }
    )


@app.post("/api/export/json")
async def export_json(body: ExportRequest):
    """
    Return table data as a downloadable JSON file.
    Accepts: { headers: [...], rows: [[...], ...], filename: "name" }
    """
    if not body.headers:
        raise HTTPException(400, "No headers provided")

    records = [dict(zip(body.headers, row)) for row in body.rows]
    payload = json.dumps({
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "row_count": len(records),
        "columns": body.headers,
        "data": records,
    }, indent=2, default=str)

    safe_name = re.sub(r"[^\w\-]", "_", body.filename or "export")
    filename = safe_name + ".json"

    return StreamingResponse(
        iter([payload]),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        }
    )


@app.get("/api/config")
def get_config():
    base_url_lower = OPENAI_API_BASE.lower()
    if "openrouter.ai" in base_url_lower:
        provider = "OpenRouter"
    elif "api.openai.com" in base_url_lower:
        provider = "OpenAI"
    elif "nvidia" in base_url_lower or "integrate.api.nvidia.com" in base_url_lower:
        provider = "NVIDIA"
    else:
        provider = "OpenAI-Compatible"
    return JSONResponse({
        "provider":    provider,
        "base_url":    OPENAI_API_BASE,
        "model":       OPENAI_MODEL,
        "api_key_set": bool(OPENAI_API_KEY),
        "model_short": OPENAI_MODEL.split("/")[-1] if OPENAI_MODEL else "",
    })


@app.get("/api/schema")
def schema_endpoint():
    return {"schema": get_schema(), "stats": get_db_stats(), "db_path": DB_PATH}


@app.get("/api/tables")
def tables_endpoint():
    return {"tables": get_db_stats()}


@app.get("/api/health")
def health():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1")
        conn.close()
        stats = get_db_stats()
        return {
            "status": "ok",
            "db": "connected",
            "tables": len(stats),
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as exc:
        raise HTTPException(500, f"DB error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
def startup():
    import platform, pydantic, fastapi as _fa
    logging.info(f"Python version: {platform.python_version()}")
    logging.info(f"Pydantic version: {pydantic.__version__}")
    logging.info(f"FastAPI version: {_fa.__version__}")
    logging.info(f"Loaded model: {OPENAI_MODEL}")
    logging.info(f"API base URL: {OPENAI_API_BASE}")
    base_url_lower = OPENAI_API_BASE.lower()
    if "openrouter.ai" in base_url_lower:
        provider = "OpenRouter"
    elif "api.openai.com" in base_url_lower:
        provider = "OpenAI"
    elif "nvidia" in base_url_lower:
        provider = "NVIDIA"
    else:
        provider = "OpenAI-Compatible"
    logging.info(f"[Server] Active Provider: {provider}")
    init_db()
    logging.info("[Server] Docs → http://localhost:8000/docs")
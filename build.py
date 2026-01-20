import os, json, math, random, pymysql
from datetime import datetime, date, timedelta
from collections import defaultdict


DB_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("MYSQL_PORT", "3306"))
DB_NAME = os.getenv("MYSQL_DB", "MultilifeQA")
DB_USER = os.getenv("MYSQL_USER", "username")
DB_PASS = os.getenv("MYSQL_PASSWORD", "password")

OUT_ROOT = "./gen_data_simplified"

GENERATE = {
    "FQ": True,
    "AS": True,
    "NC": True,
    "CQ": True,
    "TA": True,
}

TABLE_NAME = "pa_active_minutes"
ID_FIELD, TS_FIELD = "id", "ts"

METRIC_WHITELIST = {
    "fat_burn_minutes",
    "cardio_minutes",
    "peak_minutes",
    "sedentary_minutes",
    "lightly_active_minutes",
    "moderately_active_minutes",
    "very_active_minutes",
}

ACTIVE3 = ["lightly_active_minutes", "moderately_active_minutes", "very_active_minutes"]
TOTAL_METRIC = "total_active_minutes"

# Fixed RNG for reproducible thresholds in CQ
_RNG = random.Random(20250827)

# DDL map (keyed by table name) -- add entries here to extend to new tables
DDL_MAP = {
    "pa_active_minutes": """CREATE TABLE `pa_active_minutes` (
  `id`                         VARCHAR(20) NOT NULL,
  `ts`                         DATETIME NOT NULL,
  `fat_burn_minutes`           SMALLINT UNSIGNED,
  `cardio_minutes`             SMALLINT UNSIGNED,
  `peak_minutes`               SMALLINT UNSIGNED,
  `sedentary_minutes`          SMALLINT UNSIGNED,
  `lightly_active_minutes`     SMALLINT UNSIGNED,
  `moderately_active_minutes`  SMALLINT UNSIGNED,
  `very_active_minutes`        SMALLINT UNSIGNED,
  PRIMARY KEY (`id`,`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- Note: use DATE(ts)=YYYY-MM-DD for day filtering.
"""
}

PROMPTS = {
    "sql_query_base":  "Given the following MySQL table schema, write ONE SELECT statement to compute the data you need. Use DATE(ts) for date filtering if needed. Output SQL only.",
    "sql_base": "You will be given the SQL result for the question. Answer the question based on the SQL result, do not include explanations. ", 
    "pa_active_minutes": {
        # Numeric (single number; round to 2 decimals)
        "type2_intro": (
            "You are given a compact TSV view derived from a single relational table. "
            "Header is the first row. The view is restricted to a single entity (id) and includes all columns. "
            "Treat the 'date' column as DATE(ts). Answer strictly with a single number (Round to two decimal places); do not include explanations."
        ),
        "type3_sql_intro_generic": (
            "Given the following MySQL table schema, write ONE SELECT statement to compute the requested numeric value. "
            "Use DATE(ts) for date filtering if needed. Return exactly one row and one column. Output SQL only."
        ),

        # Label answers: which column among ACTIVE3 is longer (single word from the set)
        "type2_intro_label_cols": (
            "You are given a compact TSV view derived from a single relational table. "
            "Header is the first row. The view is restricted to a single entity (id) and includes all columns. "
            "Treat the 'date' column as DATE(ts). Answer strictly with a single word chosen from "
            "{lightly_active_minutes, moderately_active_minutes, very_active_minutes}; do not include explanations."
        ),

        # Label answers: increase/decrease/same
        "type2_intro_label_trend": (
            "You are given a compact TSV view derived from a single relational table. "
            "Header is the first row. The view is restricted to a single entity (id) and includes all columns. "
            "Treat the 'date' column as DATE(ts). Answer strictly with one word: increase, decrease, or same; no explanations."
        ),

        # Value + date (e.g., '123 on 2022-06-09')
        "type2_intro_value_date": (
            "You are given a compact TSV view derived from a single relational table. "
            "Header is the first row. The view is restricted to a single entity (id) and includes all columns. "
            "Treat the 'date' column as DATE(ts). Respond with the numeric value and the date only, formatted as 'N on YYYY-MM-DD'; no explanations."
        ),
    }
}


def ensure_dirs(table_name: str):
    os.makedirs(os.path.join(OUT_ROOT, "original", table_name), exist_ok=True)
    os.makedirs(os.path.join(OUT_ROOT, "simple",   table_name), exist_ok=True)
    os.makedirs(os.path.join(OUT_ROOT, "sql",      table_name), exist_ok=True)

def connect_db():
    if not DB_NAME:
        raise RuntimeError("Please set DB_NAME.")
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
        database=DB_NAME, charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )

def fetch_table(conn, table_name):
    # full table rows + columns.
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{table_name}` ORDER BY `{ID_FIELD}`, `{TS_FIELD}`")
        rows = cur.fetchall()
    cols = list(rows[0].keys()) if rows else []
    return cols, rows

def to_date_obj(ts_val) -> date:
    if isinstance(ts_val, datetime): return ts_val.date()
    if isinstance(ts_val, date):     return ts_val
    s = str(ts_val)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try: return datetime.strptime(s[:len(fmt)], fmt).date()
        except Exception: pass
    try: return datetime.fromisoformat(s).date()
    except Exception: return datetime.strptime(s[:10], "%Y-%m-%d").date()

def to_date_str(d: date) -> str:
    return d.isoformat()

def rows_to_tsv(cols, rows):
    def esc(v):
        if v is None: return ""
        return str(v).replace("\n", " ").replace("\r", " ")
    lines = ["\t".join(cols)]
    for r in rows:
        lines.append("\t".join(esc(r.get(c)) for c in cols))
    return "\n".join(lines)

def build_compact_view_per_id_all_cols(cols_all, rows_all, uid):
    # for type 2
    rows = [r.copy() for r in rows_all if r.get(ID_FIELD) == uid]
    for r in rows:
        r["date"] = to_date_str(to_date_obj(r.get(TS_FIELD)))
        if TS_FIELD in r:
            del r[TS_FIELD]
    use_cols = [ID_FIELD, "date"] + [c for c in cols_all if c not in (ID_FIELD, TS_FIELD)]
    return rows_to_tsv(use_cols, rows)

def fmt_num(x: float) -> str:
    x = round(float(x), 2)
    if x == int(x):
        return str(int(x))
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def preaggregate(rows, cols):
    # Build id->dates, id-date-col sums, ids set.
    metric_cols = [c for c in cols if c in METRIC_WHITELIST]
    if not metric_cols:
        raise RuntimeError("No metric columns found according to METRIC_WHITELIST.")
    id_date_col_sum = defaultdict(float)   # (uid, date, col) -> float
    id_dates = defaultdict(set)            # uid -> set(date)
    ids = set()
    for r in rows:
        uid = r.get(ID_FIELD)
        d = to_date_obj(r.get(TS_FIELD))
        ids.add(uid)
        id_dates[uid].add(d)
        for c in metric_cols:
            v = r.get(c)
            if v is None: continue
            try:
                id_date_col_sum[(uid, d, c)] += float(v)
            except Exception:
                pass
    return metric_cols, id_date_col_sum, id_dates, ids


def generate_FQ(cols, rows, per_id_tsv, prompts, ddl):
    import random, os, json
    ROOT = "./gen_data_simplified"
    os.makedirs(os.path.join(ROOT, "original", TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "simple",   TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "sql",      TABLE_NAME), exist_ok=True)

    path_type1 = os.path.join(ROOT, "original", TABLE_NAME, "FQ.jsonl")
    path_type2 = os.path.join(ROOT, "simple",   TABLE_NAME, "FQ.jsonl")
    path_type3 = os.path.join(ROOT, "sql",      TABLE_NAME, "FQ.jsonl")

    MAX_PER_TEMPLATE = int(__import__("os").environ.get("MAX_PER_TEMPLATE", 10))
    MAX_TRIES = 50

    metric_cols, id_date_col_sum, id_dates, ids = preaggregate(rows, cols)
    ids = list(ids)
    rnd = random.Random(20250901)

    with open(path_type1, "w", encoding="utf-8") as f1, \
         open(path_type2, "w", encoding="utf-8") as f2, \
         open(path_type3, "w", encoding="utf-8") as f3:

        produced = 0
        seen_queries = set()
        while produced < MAX_PER_TEMPLATE and MAX_TRIES > 0:
            MAX_TRIES -= 1
            if not ids:
                break
            uid = rnd.choice(ids)
            if not id_dates[uid]:
                continue
            d = rnd.choice(list(id_dates[uid]))
            ds = to_date_str(d)
            c = rnd.choice(metric_cols)

            val = id_date_col_sum.get((uid, d, c), 0.0)
            if val is None or float(val) == 0.0:
                continue 
            ans = fmt_num(val)

            q = f"How many {{{c}}} does {uid} have on {ds}?"
            if q in seen_queries:
                continue
            seen_queries.add(q)

            # Type-1
            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")

            # Type-2
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")

            # Type-3
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")

            produced += 1


def generate_AS(cols, rows, per_id_tsv, prompts, ddl):
    import random, os, json
    ROOT = "./gen_data_simplified"
    os.makedirs(os.path.join(ROOT, "original", TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "simple",   TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "sql",      TABLE_NAME), exist_ok=True)

    path_type1 = os.path.join(ROOT, "original", TABLE_NAME, "AS.jsonl")
    path_type2 = os.path.join(ROOT, "simple",   TABLE_NAME, "AS.jsonl")
    path_type3 = os.path.join(ROOT, "sql",      TABLE_NAME, "AS.jsonl")

    MAX_PER_TEMPLATE = int(__import__("os").environ.get("MAX_PER_TEMPLATE", 10))
    MAX_TRIES = 200  
    rnd = random.Random(20250901)

    metric_cols, id_date_col_sum, id_dates, ids = preaggregate(rows, cols)
    ids = list(ids)

    def random_week_start(uid):
        if not id_dates[uid]:
            return None
        dates_sorted = sorted(id_dates[uid])
        min_d, max_d = dates_sorted[0], dates_sorted[-1]
        candidates = [d for d in dates_sorted if d <= max_d - timedelta(days=6)]
        if not candidates:
            candidates = dates_sorted
        return rnd.choice(candidates) if candidates else None

    with open(path_type1, "w", encoding="utf-8") as f1, \
         open(path_type2, "w", encoding="utf-8") as f2, \
         open(path_type3, "w", encoding="utf-8") as f3:

        # template A: sum/avg within a week
        produced_total = 0; tries = MAX_TRIES
        seen = set()
        while produced_total < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            start = random_week_start(uid)
            if start is None: continue
            c = rnd.choice(metric_cols)
            sums = 0.0
            for i in range(7):
                sums += id_date_col_sum.get((uid, start + timedelta(days=i), c), 0.0)
            if sums is None or sums == 0.0:
                continue
            ans = fmt_num(sums)
            start_str = to_date_str(start)
            q = f"What is the total {{{c}}} of {uid} within one week, starting from {start_str}?"
            if q in seen: continue
            seen.add(q)
            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_total += 1

        produced_avg = 0; tries = MAX_TRIES; seen.clear()
        while produced_avg < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            start = random_week_start(uid)
            if start is None: continue
            c = rnd.choice(metric_cols)
            sums = 0.0
            for i in range(7):
                sums += id_date_col_sum.get((uid, start + timedelta(days=i), c), 0.0)
            avg = sums / 7.0
            if avg is None or avg == 0.0:
                continue
            ans = fmt_num(avg)
            start_str = to_date_str(start)
            q = f"What is the average {{{c}}} of {uid} within one week, starting from {start_str}?"
            if q in seen: continue
            seen.add(q)
            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_avg += 1

        # template B: percentage within a week
        produced_pct = 0; tries = MAX_TRIES; seen.clear()
        while produced_pct < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            start = random_week_start(uid)
            if start is None: continue
            start_str = to_date_str(start)

            sums = {c: 0.0 for c in metric_cols}
            for i in range(7):
                di = start + timedelta(days=i)
                for c in metric_cols:
                    sums[c] += id_date_col_sum.get((uid, di, c), 0.0)

            total_active = sums.get("lightly_active_minutes", 0.0) + \
                           sums.get("moderately_active_minutes", 0.0) + \
                           sums.get("very_active_minutes", 0.0)
            if total_active <= 0:
                continue

            c_pick = rnd.choice(ACTIVE3)
            pct = (sums[c_pick] / total_active) * 100.0 if total_active > 0 else 0.0
            if pct == 0.0:
                continue
            ans = fmt_num(pct)
            q = f"What percentage of total active minutes did {uid} spend in {{{c_pick}}} within a week, starting from {start_str}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_pct += 1


def generate_NC(cols, rows, per_id_tsv, prompts, ddl):
    import random, os, json
    ROOT = "./gen_data_simplified"
    os.makedirs(os.path.join(ROOT, "original", TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "simple",   TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "sql",      TABLE_NAME), exist_ok=True)

    path_type1 = os.path.join(ROOT, "original", TABLE_NAME, "NC.jsonl")
    path_type2 = os.path.join(ROOT, "simple",   TABLE_NAME, "NC.jsonl")
    path_type3 = os.path.join(ROOT, "sql",      TABLE_NAME, "NC.jsonl")

    MAX_PER_TEMPLATE = int(__import__("os").environ.get("MAX_PER_TEMPLATE", 10))
    MAX_TRIES = 200
    rnd = random.Random(20250901)

    metric_cols, id_date_col_sum, id_dates, ids = preaggregate(rows, cols)
    ids = list(ids)

    def argmax_label(d: dict) -> str:
        if not d: return ""
        maxv = max(d.values())
        cands = sorted([k for k, v in d.items() if v == maxv])
        return cands[0] if cands else ""

    def random_week_start(uid):
        if not id_dates[uid]:
            return None
        dates_sorted = sorted(id_dates[uid])
        min_d, max_d = dates_sorted[0], dates_sorted[-1]
        candidates = [d for d in dates_sorted if d <= max_d - timedelta(days=6)]
        if not candidates:
            candidates = dates_sorted
        return rnd.choice(candidates) if candidates else None

    with open(path_type1, "w", encoding="utf-8") as f1, \
         open(path_type2, "w", encoding="utf-8") as f2, \
         open(path_type3, "w", encoding="utf-8") as f3:

        # template A (day): which is longer (label)
        produced_day_label = 0; tries = MAX_TRIES; seen = set()
        while produced_day_label < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            if not id_dates[uid]: continue
            d = rnd.choice(list(id_dates[uid]))
            ds = to_date_str(d)

            vals = {c: id_date_col_sum.get((uid, d, c), 0.0) for c in ACTIVE3}
            label_day = argmax_label(vals)
            if not label_day:
                continue

            q = (
                f"Which duration is longer for {uid} on {ds}: "
                "{lightly_active_minutes}, {moderately_active_minutes} or {very_active_minutes}?"
            )
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": label_day}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro_label_cols']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": label_day}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": label_day}, ensure_ascii=False) + "\n")
            produced_day_label += 1

        # template B (day): sedentary - total_active (numeric, no 0 allowed)
        produced_day_diff = 0; tries = MAX_TRIES; seen.clear()
        while produced_day_diff < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            if not id_dates[uid]: continue
            d = rnd.choice(list(id_dates[uid]))
            ds = to_date_str(d)

            sed = id_date_col_sum.get((uid, d, "sedentary_minutes"), 0.0)
            act = sum(id_date_col_sum.get((uid, d, c), 0.0) for c in ACTIVE3)
            diff = sed - act
            if diff is None or diff == 0.0:
                continue
            ans = fmt_num(diff)
            q = f"How many minutes longer was {uid}'s sedentary time than his/her total activity time on {ds}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_day_diff += 1

        # template C (week): which is longer (label)
        produced_week_label = 0; tries = MAX_TRIES; seen.clear()
        while produced_week_label < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            start = random_week_start(uid)
            if start is None: continue
            sums = {c: 0.0 for c in ACTIVE3}
            for i in range(7):
                di = start + timedelta(days=i)
                for c in ACTIVE3:
                    sums[c] += id_date_col_sum.get((uid, di, c), 0.0)
            label_week = argmax_label(sums)
            if not label_week:
                continue
            start_str = to_date_str(start)
            q = (
                f"Which duration of activity is longer for {uid} within a week: "
                "{lightly_active_minutes}, {moderately_active_minutes} or {very_active_minutes}, starting from {start_str}?"
            )
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": label_week}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro_label_cols']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": label_week}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": label_week}, ensure_ascii=False) + "\n")
            produced_week_label += 1

        # template D (week): sedentary - total_active (numeric, no 0 allowed)
        produced_week_diff = 0; tries = MAX_TRIES; seen.clear()
        while produced_week_diff < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            start = random_week_start(uid)
            if start is None: continue
            sed_w = 0.0
            act_w = 0.0
            for i in range(7):
                di = start + timedelta(days=i)
                sed_w += id_date_col_sum.get((uid, di, "sedentary_minutes"), 0.0)
                act_w += sum(id_date_col_sum.get((uid, di, c), 0.0) for c in ACTIVE3)
            diff = sed_w - act_w
            if diff is None or diff == 0.0:
                continue
            ans = fmt_num(diff)
            start_str = to_date_str(start)
            q = f"How many minutes longer was {uid}'s sedentary time than his/her total activity time in a certain week, starting from {start_str}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_week_diff += 1

        # template E (week): highest value of a column + date (no 0 allowed)
        produced_week_max = 0; tries = MAX_TRIES; seen.clear()
        while produced_week_max < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            start = random_week_start(uid)
            if start is None: continue
            c = rnd.choice(metric_cols)
            max_val = -1e18
            max_date = None
            for i in range(7):
                di = start + timedelta(days=i)
                v = id_date_col_sum.get((uid, di, c), 0.0)
                if (v > max_val) or (v == max_val and (max_date is None or di < max_date)):
                    max_val = v; max_date = di
            if max_val is None or max_val == 0.0:
                continue
            ans_value_date = f"{fmt_num(max_val)} on {to_date_str(max_date)}"
            start_str = to_date_str(start)
            q = f"What was the highest record of {{{c}}} for {uid} within a week and on which day did it occur, starting from {start_str}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans_value_date}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro_value_date']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans_value_date}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans_value_date}, ensure_ascii=False) + "\n")
            produced_week_max += 1


def generate_CQ(cols, rows, per_id_tsv, prompts, ddl):
    import random, os, json, math
    ROOT = "./gen_data_simplified"
    os.makedirs(os.path.join(ROOT, "original", TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "simple",   TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "sql",      TABLE_NAME), exist_ok=True)

    path_type1 = os.path.join(ROOT, "original", TABLE_NAME, "CQ.jsonl")
    path_type2 = os.path.join(ROOT, "simple",   TABLE_NAME, "CQ.jsonl")
    path_type3 = os.path.join(ROOT, "sql",      TABLE_NAME, "CQ.jsonl")

    MAX_PER_TEMPLATE = int(__import__("os").environ.get("MAX_PER_TEMPLATE", 10))
    MAX_TRIES = 200
    rnd = random.Random(20250901)

    metric_cols, id_date_col_sum, id_dates, ids = preaggregate(rows, cols)
    ids = list(ids)

    def random_week_start(uid):
        if not id_dates[uid]:
            return None
        dates_sorted = sorted(id_dates[uid])
        min_d, max_d = dates_sorted[0], dates_sorted[-1]
        candidates = [d for d in dates_sorted if d <= max_d - timedelta(days=6)]
        if not candidates:
            candidates = dates_sorted
        return rnd.choice(candidates) if candidates else None

    def make_thresholds(vals7):
        mean = sum(vals7) / 7.0
        var = sum((v - mean) ** 2 for v in vals7) / 7.0
        sigma = math.sqrt(var)
        if sigma == 0.0:
            base = max(1.0, 0.1 * max(vals7 + [0.0]))
            sigma = base
        delta1 = _RNG.uniform(0.25, 0.75) * sigma
        delta2 = _RNG.uniform(0.25, 0.75) * sigma
        return [mean, mean + delta1, max(0.0, mean - delta2)]

    with open(path_type1, "w", encoding="utf-8") as f1, \
         open(path_type2, "w", encoding="utf-8") as f2, \
         open(path_type3, "w", encoding="utf-8") as f3:

        # template A: count of days within a week where > threshold (0 count is invalid)
        produced_gt = 0; tries = MAX_TRIES; seen = set()
        while produced_gt < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            start = random_week_start(uid)
            if start is None: continue
            c = rnd.choice(metric_cols)
            vals7 = [id_date_col_sum.get((uid, start + timedelta(days=i), c), 0.0) for i in range(7)]
            t = rnd.choice(make_thresholds(vals7))
            t_str = fmt_num(t)
            cnt = sum(1 for v in vals7 if v > t)
            if cnt == 0:
                continue
            ans = str(int(cnt))
            start_str = to_date_str(start)
            q = f"How many days within a week does {uid} have {{{c}}} greater than {t_str}, starting from {start_str}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_gt += 1

        # template B: count of days within a week where < threshold (0 count is invalid)
        produced_lt = 0; tries = MAX_TRIES; seen.clear()
        while produced_lt < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            start = random_week_start(uid)
            if start is None: continue
            c = rnd.choice(metric_cols)
            vals7 = [id_date_col_sum.get((uid, start + timedelta(days=i), c), 0.0) for i in range(7)]
            t = rnd.choice(make_thresholds(vals7))
            t_str = fmt_num(t)
            cnt = sum(1 for v in vals7 if v < t)
            if cnt == 0:
                continue
            ans = str(int(cnt))
            start_str = to_date_str(start)
            q = f"How many days within a week does {uid} have {{{c}}} less than {t_str}, starting from {start_str}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_lt += 1


def generate_TA(cols, rows, per_id_tsv, prompts, ddl):
    import random, os, json
    ROOT = "./gen_data_simplified"
    os.makedirs(os.path.join(ROOT, "original", TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "simple",   TABLE_NAME), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "sql",      TABLE_NAME), exist_ok=True)

    path_type1 = os.path.join(ROOT, "original", TABLE_NAME, "TA.jsonl")
    path_type2 = os.path.join(ROOT, "simple",   TABLE_NAME, "TA.jsonl")
    path_type3 = os.path.join(ROOT, "sql",      TABLE_NAME, "TA.jsonl")

    MAX_PER_TEMPLATE = int(__import__("os").environ.get("MAX_PER_TEMPLATE", 10))
    MAX_TRIES = 300
    rnd = random.Random(20250901)

    real_metrics, id_date_col_sum, id_dates, ids = preaggregate(rows, cols)
    metrics_for_trend = real_metrics + [TOTAL_METRIC]
    ids = list(ids)

    def make_series(uid, metric, min_date, max_date):
        series = {}
        d = min_date
        while d <= max_date:
            if metric == TOTAL_METRIC:
                series[d] = (
                    id_date_col_sum.get((uid, d, "lightly_active_minutes"), 0.0)
                    + id_date_col_sum.get((uid, d, "moderately_active_minutes"), 0.0)
                    + id_date_col_sum.get((uid, d, "very_active_minutes"), 0.0)
                )
            else:
                series[d] = id_date_col_sum.get((uid, d, metric), 0.0)
            d += timedelta(days=1)
        return series

    def consecutive_days_inclusive(values_by_day, start_date, max_date, direction: str):
        count = 1
        cur = start_date
        while cur < max_date:
            v0 = values_by_day.get(cur, 0.0)
            v1 = values_by_day.get(cur + timedelta(days=1), 0.0)
            if direction == "up" and v1 > v0:
                count += 1; cur += timedelta(days=1); continue
            if direction == "down" and v1 < v0:
                count += 1; cur += timedelta(days=1); continue
            break
        return count

    def week_sum(values_by_day, start):
        return sum(values_by_day.get(start + timedelta(days=i), 0.0) for i in range(7))

    def consecutive_weeks_inclusive(values_by_day, start_date, max_date, direction: str):
        if start_date + timedelta(days=6) > max_date:
            return 0
        count = 1
        w0 = start_date
        while True:
            w1 = w0 + timedelta(days=7)
            if w1 + timedelta(days=6) > max_date:
                break
            s0 = week_sum(values_by_day, w0)
            s1 = week_sum(values_by_day, w1)
            if direction == "up" and s1 > s0:
                count += 1; w0 = w1; continue
            if direction == "down" and s1 < s0:
                count += 1; w0 = w1; continue
            break
        return count

    with open(path_type1, "w", encoding="utf-8") as f1, \
         open(path_type2, "w", encoding="utf-8") as f2, \
         open(path_type3, "w", encoding="utf-8") as f3:

        uid_ranges = {}
        for uid in ids:
            ds = sorted(id_dates[uid])
            if not ds: continue
            uid_ranges[uid] = (ds[0], ds[-1])

        # template A1: daily comparison with previous day (label)
        produced_day_label = 0; tries = MAX_TRIES; seen = set()
        while produced_day_label < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            if uid not in uid_ranges: continue
            min_d, max_d = uid_ranges[uid]
            d = rnd.choice(sorted(id_dates[uid]))
            prev = d - timedelta(days=1)
            if prev not in id_dates[uid]:
                continue
            c = rnd.choice(metrics_for_trend)
            series = make_series(uid, c, min_d, max_d)
            v_today = series.get(d, 0.0)
            v_prev = series.get(prev, 0.0)
            if v_today > v_prev: label = "increase"
            elif v_today < v_prev: label = "decrease"
            else: label = "same"

            q = f"Did {uid}'s {{{c}}} on {to_date_str(d)} increase or decrease or remain the same, compared to the previous day?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": label}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro_label_trend']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": label}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": label}, ensure_ascii=False) + "\n")
            produced_day_label += 1

        # template A2: daily count of consecutive increasing days (1 is invalid)
        consec_count = defaultdict(int)
        for i in range(1, 8):
            consec_count[i] = 0
        produced_day_up = 0; tries = MAX_TRIES; seen.clear()
        while produced_day_up < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            if uid not in uid_ranges: continue
            min_d, max_d = uid_ranges[uid]
            d = rnd.choice(sorted(id_dates[uid]))
            c = rnd.choice(metrics_for_trend)
            series = make_series(uid, c, min_d, max_d)
            inc_days = consecutive_days_inclusive(series, d, max_d, direction="up")
            consec_count[inc_days]+=1
            if consec_count[inc_days] > 10 and not all(count > 10 for count in consec_count.values()) and not tries < 100:
                continue
            if inc_days == 1:
                continue
            ans = fmt_num(inc_days)
            q = f"How many consecutive days did {uid}'s {{{c}}} increase, starting from {to_date_str(d)}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_day_up += 1

        # template A3: daily count of consecutive decreasing days (1 is invalid)
        produced_day_down = 0; tries = MAX_TRIES; seen.clear()
        consec_count = defaultdict(int)
        for i in range(1, 8):
            consec_count[i] = 0
        while produced_day_down < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            if uid not in uid_ranges: continue
            min_d, max_d = uid_ranges[uid]
            d = rnd.choice(sorted(id_dates[uid]))
            c = rnd.choice(metrics_for_trend)
            series = make_series(uid, c, min_d, max_d)
            dec_days = consecutive_days_inclusive(series, d, max_d, direction="down")
            consec_count[dec_days]+=1
            if consec_count[dec_days] > 10 and not all(count > 10 for count in consec_count.values()) and not tries < 100:
                continue
            if dec_days == 1:
                continue
            ans = fmt_num(dec_days)
            q = f"How many consecutive days did {uid}'s {{{c}}} decrease, starting from {to_date_str(d)}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_day_down += 1

        # template B1: weekly comparison with previous week (label)
        produced_week_label = 0; tries = MAX_TRIES; seen.clear()
        while produced_week_label < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            if uid not in uid_ranges: continue
            min_d, max_d = uid_ranges[uid]
            c = rnd.choice(metrics_for_trend)
            series = make_series(uid, c, min_d, max_d)

            dates_sorted = sorted(id_dates[uid])
            candidates = [d for d in dates_sorted if d + timedelta(days=6) <= max_d]
            if not candidates:
                continue
            start = rnd.choice(candidates)
            prev_week_start = start - timedelta(days=7)
            cur_sum = sum(series.get(start + timedelta(days=i), 0.0) for i in range(7))
            if prev_week_start < min_d:
                continue
            prev_sum = sum(series.get(prev_week_start + timedelta(days=i), 0.0) for i in range(7))
            if cur_sum > prev_sum: wlabel = "increase"
            elif cur_sum < prev_sum: wlabel = "decrease"
            else: wlabel = "same"

            q = (
                f"Did {uid}'s {{{c}}} within a week, starting from {to_date_str(start)}, "
                f"increase or decrease or remain the same, compared to the previous week?"
            )
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": wlabel}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro_label_trend']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": wlabel}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": wlabel}, ensure_ascii=False) + "\n")
            produced_week_label += 1

        # template B2: weekly count of consecutive increasing weeks (1 is invalid)
        produced_week_up = 0; tries = MAX_TRIES; seen.clear()
        while produced_week_up < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            if uid not in uid_ranges: continue
            min_d, max_d = uid_ranges[uid]
            c = rnd.choice(metrics_for_trend)
            series = make_series(uid, c, min_d, max_d)
            dates_sorted = sorted(id_dates[uid])
            candidates = [d for d in dates_sorted if d + timedelta(days=6) <= max_d]
            if not candidates: continue
            start = rnd.choice(candidates)
            inc_weeks = consecutive_weeks_inclusive(series, start, max_d, direction="up")
            if inc_weeks == 1:
                continue
            ans = fmt_num(inc_weeks)
            q = f"How many consecutive weeks did {uid}'s {{{c}}} increase, starting from {to_date_str(start)}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_week_up += 1

        # template B3: weekly count of consecutive decreasing weeks (1 is invalid)
        produced_week_down = 0; tries = MAX_TRIES; seen.clear()
        while produced_week_down < MAX_PER_TEMPLATE and tries > 0:
            tries -= 1
            if not ids: break
            uid = rnd.choice(ids)
            if uid not in uid_ranges: continue
            min_d, max_d = uid_ranges[uid]
            c = rnd.choice(metrics_for_trend)
            series = make_series(uid, c, min_d, max_d)
            dates_sorted = sorted(id_dates[uid])
            candidates = [d for d in dates_sorted if d + timedelta(days=6) <= max_d]
            if not candidates: continue
            start = rnd.choice(candidates)
            dec_weeks = consecutive_weeks_inclusive(series, start, max_d, direction="down")
            if dec_weeks == 1:
                continue
            ans = fmt_num(dec_weeks)
            q = f"How many consecutive weeks did {uid}'s {{{c}}} decrease, starting from {to_date_str(start)}?"
            if q in seen: continue
            seen.add(q)

            f1.write(json.dumps({"Query": q, "Answer": ans}, ensure_ascii=False) + "\n")
            q2 = (
                f"{prompts['type2_intro']}\n\n"
                f"Question: {q}\n\n"
                f"=== BEGIN TABLE `{TABLE_NAME}` (compact TSV, id={uid}) ===\n"
                f"{per_id_tsv[uid]}\n"
                f"=== END TABLE ==="
            )
            f2.write(json.dumps({"Query": q2, "Answer": ans}, ensure_ascii=False) + "\n")
            qsql  = f"{PROMPTS['sql_query_base']}\n\nQuestion:\n{q}\n\nSchema (DDL):\n```sql\n{ddl}\n```"
            qbase = f"{PROMPTS['sql_base']}\nQuestion: {q}"
            f3.write(json.dumps({"Query_sql": qsql, "Query_base": qbase, "Answer": ans}, ensure_ascii=False) + "\n")
            produced_week_down += 1

def main():
    ensure_dirs(TABLE_NAME)
    conn = connect_db()
    try:
        cols, rows = fetch_table(conn, TABLE_NAME)
        if not rows:
            print(f"[WARN] Table `{TABLE_NAME}` is empty.")
            return

        # Per-id compact TSV
        per_id_tsv = {}
        ids_seen = set()
        for r in rows:
            uid = r.get(ID_FIELD)
            if uid not in ids_seen:
                per_id_tsv[uid] = build_compact_view_per_id_all_cols(cols, rows, uid)
                ids_seen.add(uid)

        ddl  = DDL_MAP[TABLE_NAME]
        prom = PROMPTS[TABLE_NAME]

        if GENERATE.get("FQ"): generate_FQ(cols, rows, per_id_tsv, prom, ddl)
        if GENERATE.get("AS"): generate_AS(cols, rows, per_id_tsv, prom, ddl)
        if GENERATE.get("NC"): generate_NC(cols, rows, per_id_tsv, prom, ddl)
        if GENERATE.get("CQ"): generate_CQ(cols, rows, per_id_tsv, prom, ddl)
        if GENERATE.get("TA"): generate_TA(cols, rows, per_id_tsv, prom, ddl)

        print("All requested categories generated.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()

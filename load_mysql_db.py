from __future__ import annotations
import os, re, glob, pathlib, logging
from typing import Dict, Callable
from dotenv import load_dotenv
import pandas as pd
import numpy as np
import pymysql
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
load_dotenv()

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
SCHEMA_SQL = ROOT / "schema.sql"

DB_CFG = dict(
    host=os.getenv("MYSQL_HOST", "localhost"),
    port=int(os.getenv("MYSQL_PORT", 3306)),
    user=os.getenv("MYSQL_USER", "username"),
    password=os.getenv("MYSQL_PWD", "password!"),
    database="MultilifeQA",
    charset="utf8mb4",
    autocommit=True,
)

CHUNKSIZE = 2000

def run_schema(conn):
    logging.info("Executing schema.sql …")
    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0;")
        try:
            with open(SCHEMA_SQL, encoding="utf-8") as f:
                buff = ""
                for line in f:
                    buff += line
                    if line.strip().endswith(";"):
                        try:
                            cur.execute(buff)
                        except pymysql.err.InternalError as e:
                            if e.args[0] not in (1050, 1091):
                                raise
                        buff = ""
        finally:
            cur.execute("SET FOREIGN_KEY_CHECKS=1;")


def df_to_sql(df: pd.DataFrame, table: str, conn):
    if df.empty:
        return
    df = df.replace([np.inf, -np.inf], np.nan)

    cols = ", ".join(f"`{c}`" for c in df.columns)
    ph = ", ".join(["%s"] * len(df.columns))
    sql = f"INSERT IGNORE INTO `{table}` ({cols}) VALUES ({ph})"

    def _clean(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except Exception:
            pass

        if isinstance(v, pd.Timestamp):
            if v.tzinfo is not None:
                v = v.tz_localize(None)
            return v.to_pydatetime()

        if isinstance(v, str):
            vv = _strip_tz_tokens(v)
            try:
                parsed = pd.to_datetime(vv, errors="raise")
                return parsed.to_pydatetime() if isinstance(parsed, pd.Timestamp) else parsed
            except Exception:
                return vv

        return v

    with conn.cursor() as cur:
        for i in range(0, len(df), CHUNKSIZE):
            chunk = df.iloc[i : i + CHUNKSIZE]
            values = [tuple(_clean(x) for x in row) for row in chunk.itertuples(index=False, name=None)]
            cur.executemany(sql, values)


def get_table_columns(conn, table: str) -> list[str]:
    sql = """SELECT COLUMN_NAME
             FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
             ORDER BY ORDINAL_POSITION"""
    with conn.cursor() as cur:
        cur.execute(sql, (DB_CFG["database"], table))
        return [r[0] for r in cur.fetchall()]

_TZ_OFFSET_RE = re.compile(r'([+-]\d{2}:?\d{2}|Z)$', re.IGNORECASE)
_TZ_TRAILING_TOKEN_RE = re.compile(r'\s*(?:[A-Z]{2,5}|\([^)]+\)|\[[^\]]+\])$')

def _strip_tz_tokens(val):
    if pd.isna(val):
        return val
    s = str(val).strip().replace("T", " ")
    s = _TZ_TRAILING_TOKEN_RE.sub("", s)
    s = _TZ_OFFSET_RE.sub("", s)
    return s.strip()

_NUM_RE = re.compile(r"^\s*[<>]\s*")

def clean_numeric(df: pd.DataFrame, numeric_cols: list[str] | None = None):
    if numeric_cols is None:
        numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
        for col in df.select_dtypes(include=["object"]).columns:
            sample = df[col].dropna().astype(str).head(20)
            if (sample.str.match(r"^\s*[<>]?\s*\d")).mean() > 0.8:
                numeric_cols.append(col)
    for col in set(numeric_cols):
        ser = df[col].astype(str)
        if ser.str.contains(_NUM_RE).any():
            ser = ser.str.replace(_NUM_RE, "", regex=True)
        df[col] = pd.to_numeric(ser, errors="coerce")
    return df

def to_datetime_naive(s: pd.Series) -> pd.Series:
    s_clean = s.astype(str).map(_strip_tz_tokens)
    dt = pd.to_datetime(s_clean, errors="coerce")
    try:
        return dt.apply(lambda x: x.tz_localize(None) if isinstance(x, pd.Timestamp) and x.tzinfo else x)
    except Exception:
        return dt


def id_from_filename(name: str) -> str | None:
    m = re.match(r"(A\d+[A-Z]?_\d+)", pathlib.Path(name).stem)
    return m.group(1) if m else None

ALIASES = {
    "ts": ["timestamp"],
    "record_ts": ["timestamp"],
    "session_ts": ["timestamp"],

    "start_time": ["start", "start_local", "start_time_local"],
    "end_time": ["end", "end_local", "end_time_local"],
    "start_sleep": ["start_sleep_time"],
    "end_sleep": ["end_sleep_time"],

    "day": ["date", "timestamp"],
    "date": ["day", "timestamp"],

    "bpm": ["value", "heart_rate", "hr"],
    "glucose_mg_dl": ["glucose_value_in_mg_dl"],

    "resting_hr_bpm": ["resting_heart_rate", "resting_hr"],
    "systolic_bp_mmhg": ["systolic", "systolic_bp"],
    "diastolic_bp_mmhg": ["diastolic", "diastolic_bp"],

    "calories_kcal": ["calories"],
    "distance_m": ["distance"],
    "altitude_m": ["altitude"],

    "rmssd": ["rmssd_ms"],
    "nrem_hr": ["nrem_heart_rate"],
    "minutes_in_rem": ["minutes_in_rem_sleep"],

    "night_end": ["timestamp"],
}

TIME_LIKE = {
    "ts","record_ts","session_ts",
    "start_time","end_time","start_sleep","end_sleep",
    "night_end"
}

def build_insert_frame(df: pd.DataFrame, table: str, conn, per_id=False, pid=None) -> pd.DataFrame:
    wanted = get_table_columns(conn, table)
    src_cols = {c.lower(): c for c in df.columns}
    out = pd.DataFrame(index=df.index)

    for tgt in wanted:
        if tgt.lower() in src_cols:
            out[tgt] = df[src_cols[tgt.lower()]]
            continue
        for alias in ALIASES.get(tgt, []):
            if alias.lower() in src_cols:
                out[tgt] = df[src_cols[alias.lower()]]
                break
        if tgt == "id" and per_id and "id" not in out.columns:
            out["id"] = pid
        if tgt not in out.columns:
            out[tgt] = pd.Series([None]*len(df), index=df.index)

    if "day" in wanted and out["day"].isna().all():
        cand = None
        for c in ["timestamp","ts"]:
            if c.lower() in src_cols:
                cand = df[src_cols[c.lower()]]
                break
        if cand is not None:
            out["day"] = pd.to_datetime(to_datetime_naive(cand).dt.date)

    if "date" in wanted and out["date"].isna().all():
        cand = None
        for c in ["date","timestamp","ts","day"]:
            if c.lower() in src_cols:
                cand = df[src_cols[c.lower()]]
                break
            if c in out.columns and out[c].notna().any():
                cand = out[c]; break
        if cand is not None:
            out["date"] = pd.to_datetime(to_datetime_naive(cand).dt.date)

    for c in set(TIME_LIKE).intersection(wanted):
        if c in out.columns and out[c].notna().any():
            out[c] = to_datetime_naive(out[c])

    clean_numeric(out)

    return out[wanted]

def load_generic_single(fp: pathlib.Path, table: str, conn):
    df = pd.read_csv(fp)
    insert_df = build_insert_frame(df, table, conn, per_id=False)
    df_to_sql(insert_df, table, conn)

def load_generic_per_id(fp: pathlib.Path, table: str, conn):
    pid = id_from_filename(fp.name)
    df = pd.read_csv(fp)
    insert_df = build_insert_frame(df, table, conn, per_id=True, pid=pid)
    df_to_sql(insert_df, table, conn)

def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    low = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in low:
            return low[cand.lower()]
    return None

def parse_numeric_array(cell) -> list[int]:
    if pd.isna(cell): return []
    text = str(cell).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    parts = re.split(r"[,\s]+", text.strip())
    out = []
    for p in parts:
        if not p: continue
        try: out.append(int(float(p)))
        except: pass
    return out

def load_ecg_file(fp: pathlib.Path, conn):
    pid = id_from_filename(fp.name)
    df = pd.read_csv(fp)

    rec_df = build_insert_frame(df, "ecg_recordings", conn, per_id=True, pid=pid)

    wave_col = find_col(df, ["waveform_samples", "waveform", "samples"])

    for i, row in rec_df.iterrows():
        arr = parse_numeric_array(df.iloc[i][wave_col]) if wave_col else []
        rec_df.at[i, "sample_count"] = int(len(arr))

        df_to_sql(pd.DataFrame([rec_df.loc[i]]), "ecg_recordings", conn)

        if arr:
            wf = pd.DataFrame({
                "id": row["id"],
                "record_ts": row["record_ts"],
                "sample_idx": np.arange(len(arr), dtype=int),
                "voltage": arr,
            })
            df_to_sql(wf, "ecg_waveforms", conn)


def load_eda_sessions_file(fp: pathlib.Path, conn):
    pid = id_from_filename(fp.name)
    df = pd.read_csv(fp)

    meta_df = build_insert_frame(df, "eda_sessions", conn, per_id=True, pid=pid)

    wave_col = find_col(df, ["skin_conductance_levels", "levels", "eda_levels", "eda_series"])

    with conn.cursor() as cur:
        for i, meta in meta_df.iterrows():
            arr = parse_numeric_array(df.iloc[i][wave_col]) if wave_col else []
            meta_df.at[i, "sample_count"] = int(len(arr))

            cols = [c for c in meta.index if pd.notna(meta_df.at[i, c]) or c in ["id", "session_ts"]]
            sql = f"INSERT INTO `eda_sessions` ({', '.join('`'+c+'`' for c in cols)}) VALUES ({', '.join(['%s']*len(cols))})"
            cur.execute(sql, [meta_df.at[i, c] for c in cols])
            session_id = cur.lastrowid

            if arr:
                batch = [(int(session_id), int(j), float(v)) for j, v in enumerate(arr)]
                cur.executemany(
                    "INSERT IGNORE INTO `eda_levels` (`session_id`,`sample_idx`,`level_microsiemens`) VALUES (%s,%s,%s)",
                    batch
                )

LOAD_MAP: Dict[str, Dict[str, Callable]] = {
    "participant_information.csv": {"table": "participants", "loader": load_generic_single},

    "DS7_PhysicalActivity/IPAQ.csv": {"table": "physical_activity_ipaq", "loader": load_generic_single},
    "DS7_PhysicalActivity/active_minutes/*.csv": {"table": "pa_active_minutes", "loader": load_generic_per_id},
    "DS7_PhysicalActivity/estimated_VO2/*.csv": {"table": "pa_estimated_VO2", "loader": load_generic_per_id},
    "DS7_PhysicalActivity/physical_activity_reports/*.csv": {"table": "pa_reports", "loader": load_generic_per_id},
    "DS7_PhysicalActivity/additional_physical_activity_data/*.csv": {"table": "pa_daily_summary", "loader": load_generic_per_id},

    "DS8_SleepActivity/OviedoSleepQuestionnaire.csv": {"table": "OviedoSleepQuestionnaire", "loader": load_generic_single},
    "DS8_SleepActivity/additional_sleep_data/*.csv": {"table": "additional_sleep", "loader": load_generic_per_id},
    "DS8_SleepActivity/skin_temperature/*computed_temperature.csv": {"table": "skin_temp_sleep_nightly", "loader": load_generic_per_id},
    "DS8_SleepActivity/skin_temperature/*wrist_temperature.csv": {"table": "skin_temp_wrist_minute", "loader": load_generic_per_id},
    "DS8_SleepActivity/sleep_quality/*.csv": {"table": "sleep_quality", "loader": load_generic_per_id},
    "DS8_SleepActivity/oxygen_saturation/*daily_oxygen_saturation.csv": {"table": "oxygen_sat_daily", "loader": load_generic_per_id},
    "DS8_SleepActivity/oxygen_saturation/*oxygen_saturation_by_minute.csv": {"table": "oxygen_sat_minute", "loader": load_generic_per_id},
    "DS8_SleepActivity/respiratory_rate/*.csv": {"table": "respiratory_rate", "loader": load_generic_per_id},
    "DS8_SleepActivity/heart_rate_variability/*.csv": {"table": "heart_rate_variability", "loader": load_generic_per_id},

    "DS9_EmotionalState/DASS-21.csv": {"table": "emotional_dass21", "loader": load_generic_single},
    "DS9_EmotionalState/stress_score/*.csv": {"table": "stress_daily_scores", "loader": load_generic_per_id},
    "DS9_EmotionalState/eda_sessions/*.csv": {"table": "eda_sessions", "loader": load_eda_sessions_file},

    "DS10_AdditionalInformation/SUS.csv": {"table": "sus_scores", "loader": load_generic_single},
}

def main():
    if not DATA.exists():
        logging.error("data/ not found at %s", DATA)
        return
    conn = pymysql.connect(**DB_CFG)
    with conn:
        run_schema(conn)
        for pattern, cfg in LOAD_MAP.items():
            files = glob.glob(str(DATA / pattern))
            if not files:
                logging.warning("No match: %s", pattern)
                continue
            logging.info("Loading %s (%d files)…", pattern, len(files))
            for fp in tqdm(files, unit="file", ncols=80, leave=False):
                fp = pathlib.Path(fp)
                loader: Callable = cfg["loader"]
                table: str = cfg["table"]
                try:
                    if loader in (load_ecg_file, load_eda_sessions_file):
                        loader(fp, conn)
                    else:
                        loader(fp, table, conn)
                except Exception as e:
                    logging.exception("Failed to load %s into %s: %s", fp.name, table, e)

    logging.info("All done.")

if __name__ == "__main__":
    main()

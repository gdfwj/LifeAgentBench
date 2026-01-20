import os
import sys
import re
import math
import pandas as pd
import pymysql
from datetime import datetime
from typing import List, Tuple

MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "username")
MYSQL_PASS = os.getenv("MYSQL_PASSWORD", "password")
DB_NAME    = "MultilifeQA"

DATA_ROOT = "./data/FoodNExtDB"
BATCH_SIZE = 2000


def get_conn(db: str | None = None):
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=db,
        charset="utf8mb4",
        autocommit=False,
    )


def create_db_and_table():
    ddl_table = f"""
    CREATE TABLE IF NOT EXISTS {DB_NAME}.food_meal_labels (
      id            VARCHAR(20)   NOT NULL,
      ts            DATETIME      NOT NULL,
      image_id      VARCHAR(100)  NOT NULL,
      category      VARCHAR(128)  NOT NULL,
      subcategory   VARCHAR(128)  NOT NULL,
      cooking_style VARCHAR(128)  NOT NULL,
      PRIMARY KEY (id, ts, image_id, category, subcategory, cooking_style),
      KEY idx_id_ts (id, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl_table)
        conn.commit()
        print("[OK] connected")
    finally:
        conn.close()


def parse_user_id_from_image(image_id: str) -> str:
    base = image_id.replace(".jpg", "")
    parts = base.split("_")
    if len(parts) >= 2:
        return f"{parts[0]}_{parts[1]}"
    return parts[0]


def strip_tz_to_naive(ts_str: str) -> datetime:
    if not isinstance(ts_str, str):
        raise ValueError(f"timestamp is not string: {ts_str}")

    try:
        dt = datetime.fromisoformat(ts_str)
        return dt.replace(tzinfo=None)
    except Exception:
        core = ts_str[:19]
        return datetime.strptime(core, "%Y-%m-%d %H:%M:%S")


def read_csv_auto(path: str) -> pd.DataFrame:
    return pd.read_csv(path, sep=None, engine="python", dtype=str, keep_default_na=False)


def load_folder(user_dir: str) -> List[Tuple[str, datetime, str, str, str, str]]:
    user_id = os.path.basename(user_dir.rstrip("/\\"))
    labeled_csv = os.path.join(user_dir, f"{user_id}_labeled_data.csv")
    time_csv    = os.path.join(user_dir, f"{user_id}_timestamps.csv")

    if not (os.path.exists(labeled_csv) and os.path.exists(time_csv)):
        print(f"[WARN] no csv, skip: {user_dir}")
        return []

    df_lab = read_csv_auto(labeled_csv)
    df_ts  = read_csv_auto(time_csv)

    df_lab = df_lab.rename(columns={
        "id": "image_id",
        "category": "category",
        "subcategory": "subcategory",
        "cooking_style": "cooking_style",
    })[["image_id", "category", "subcategory", "cooking_style"]]

    df_lab = df_lab.drop_duplicates(subset=["image_id", "category", "subcategory", "cooking_style"])

    df_ts = df_ts.rename(columns={
        "id": "image_id",
        "timestamp": "timestamp_raw",
    })[["image_id", "timestamp_raw"]]

    df = pd.merge(df_lab, df_ts, on="image_id", how="inner")

    df["ts"] = df["timestamp_raw"].apply(strip_tz_to_naive)
    df["id"] = user_id

    rows = list(
        df[["id", "ts", "image_id", "category", "subcategory", "cooking_style"]]
        .itertuples(index=False, name=None)
    )
    return rows


def load_all_rows(root: str) -> List[Tuple[str, datetime, str, str, str, str]]:
    all_rows: List[Tuple[str, datetime, str, str, str, str]] = []
    if not os.path.isdir(root):
        raise FileNotFoundError(f"not found: {root}")

    for entry in os.scandir(root):
        if entry.is_dir():
            rows = load_folder(entry.path)
            if rows:
                all_rows.extend(rows)
                print(f"[OK] {entry.name}: {len(rows)}")
    print(f"[INFO] writing {len(all_rows)}")
    return all_rows


def insert_rows(rows: List[Tuple[str, datetime, str, str, str, str]]):
    if not rows:
        print("[INFO] no data")
        return

    sql = f"""
    INSERT IGNORE INTO {DB_NAME}.food_meal_labels
    (id, ts, image_id, category, subcategory, cooking_style)
    VALUES (%s, %s, %s, %s, %s, %s)
    """
    conn = get_conn(DB_NAME)
    try:
        with conn.cursor() as cur:
            total = len(rows)
            for i in range(0, total, BATCH_SIZE):
                batch = rows[i:i+BATCH_SIZE]
                cur.executemany(sql, batch)
                conn.commit()
                print(f"[BATCH] {min(i+BATCH_SIZE, total)}/{total}")
        print("[OK]")
    finally:
        conn.close()


def main():
    create_db_and_table()
    rows = load_all_rows(DATA_ROOT)
    insert_rows(rows)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

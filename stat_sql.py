import math
import json, re, sys
from collections import defaultdict

def normalize_text(s: str) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"^(final\s+answer|answer|a)\s*[:\-]\s*", "", s)
    s = s.replace("\u200b", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\"'`]", "", s)
    return s.strip()

def first_word_yesno(s: str):
    s = normalize_text(s)
    m = re.match(r"\b(yes|no|true|false)\b", s)
    if not m:
        return None
    return "yes" if m.group(1) in {"yes","true"} else "no"

def safe_float(s: str):
    try:
        x = float(s)
        if not math.isfinite(x):
            return None
        return x
    except Exception:
        return None

def is_int_like(x: float) -> bool:
    try:
        if x is None or not math.isfinite(x):
            return False
        return abs(x - round(x)) < 1e-9
    except Exception:
        return False

_SEMI_SPLIT = re.compile(r"\s*[;；]\s*")

def split_answer_items_gt(ans: str):
    s = str(ans).strip()
    if ";" not in s and "；" not in s:
        return [s]
    return [a for a in _SEMI_SPLIT.split(s) if a != ""]

def split_items_pred(ans: str, allow_space_split=True):
    s = ans.strip()
    if ";" in s or "；" in s:
        return [a for a in _SEMI_SPLIT.split(s) if a != ""]

    if not allow_space_split:
        return [s]

    tokens = s.split()
    if not tokens:
        return [""]

    items, buf = [], []
    def _safe_float(t):
        try:
            return float(t)
        except Exception:
            return None

    for i, tok in enumerate(tokens):
        buf.append(tok)
        if i < len(tokens) - 1:
            left, right = tokens[i], tokens[i + 1]
            if _safe_float(left) is not None or _safe_float(right) is not None:
                items.append(" ".join(buf))
                buf = []
    if buf:
        items.append(" ".join(buf))
    return [a.strip() for a in items]

def compare_item_relaxed(pred_raw: str, gt_raw: str):
    gt_norm = normalize_text(gt_raw)
    pred_norm = normalize_text(pred_raw)

    # Yes/No
    if gt_norm in {"yes", "no"}:
        p = first_word_yesno(pred_raw)
        return (p == gt_norm, None, False)

    # Numeric
    gt_num = safe_float(gt_norm)
    if gt_num is not None:
        pred_num = safe_float(pred_norm)
        if pred_num is None:
            return (False, None, False)

        if is_int_like(gt_num) and abs(gt_num) < 14:
            ok = abs(pred_num - gt_num) <= 1.0
            return (ok, None, False)

        tol = max(0.005 * abs(gt_num), 0.01)
        ok = abs(pred_num - gt_num) <= tol

        if (not is_int_like(gt_num)) and (not is_int_like(pred_num)):
            mse = (pred_num - gt_num) ** 2
            return (ok, mse, True)
        else:
            return (ok, None, False)

    # Text
    ok = (pred_norm == gt_norm)
    return (ok, None, False)


import json
import re

_num_pat = re.compile(r'[-+]?\d+(?:\.\d+)?')

def _normalize_preview_to_text_and_numbers(preview_obj):
    if preview_obj is None:
        raw = ""
    elif isinstance(preview_obj, str):
        raw = preview_obj
    else:
        try:
            raw = json.dumps(preview_obj, ensure_ascii=False)
        except Exception:
            raw = str(preview_obj)

    text = raw.lower()
    text = text.replace("\u200b", " ")
    text = re.sub(r'\s+', ' ', text)
    text = text.replace('"', '').replace("'", "").strip()

    nums = []
    for m in _num_pat.finditer(raw):
        try:
            x = float(m.group())
            if math.isfinite(x):
                nums.append(x)
        except Exception:
            pass

    return text, nums

def _number_match_in_preview(gt_num: float, preview_nums):
    if is_int_like(gt_num) and abs(gt_num) < 14:
        tol = 1.0
    else:
        tol = max(0.005 * abs(gt_num), 0.01)

    best_idx, best_diff = None, float('inf')
    for idx, x in enumerate(preview_nums):
        d = abs(x - gt_num)
        if d <= tol and d < best_diff:
            best_idx, best_diff = idx, d
    return best_idx

def sql_preview_covers_answer(gt_items, preview_obj) -> bool:
    preview_text, preview_nums_all = _normalize_preview_to_text_and_numbers(preview_obj)
    used_num_idx = set()

    for g in gt_items:
        g_norm = normalize_text(g)
        g_num = safe_float(g_norm)

        if g_num is not None:
            idx = _number_match_in_preview(g_num, preview_nums_all)
            if idx is None:
                return False
            if idx in used_num_idx:
                temp = list(preview_nums_all)
                while idx is not None and idx in used_num_idx:
                    temp[idx] = float('nan')
                    best_idx, best_diff = None, float('inf')
                    for j, x in enumerate(temp):
                        if not (isinstance(x, float) and math.isnan(x)):
                            d = abs(x - g_num)
                            if is_int_like(g_num) and abs(g_num) < 14:
                                tol = 1.0
                            else:
                                tol = max(0.005 * abs(g_num), 0.01)
                            if d <= tol and d < best_diff:
                                best_idx, best_diff = j, d
                    idx = best_idx
                if idx is None:
                    return False
            used_num_idx.add(idx)
        else:
            if g_norm not in preview_text:
                return False

    return True


def classify_answer_kind(gt_items):
    if len(gt_items) == 1:
        norm = normalize_text(gt_items[0])
        if norm in {"yes", "no"}:
            return "Yes/No"
        if safe_float(norm) is not None:
            return "1-Number"
        return "1-Text"
    elif len(gt_items) == 2:
        return "2-Items"
    else:
        return "≥3-Items"

def classify_table(folder: str):
    if folder == "sleep_joint":
        return "M-Sleep"
    if folder == "physical_activity_joint":
        return "M-Activity"
    if folder == "all_joint":
        return "M-C4"
    if "_joint" in folder:
        return "M-C2"
    return "Single"

def is_multi_folder(folder: str) -> bool:
    return folder.endswith("_multi") or "_multi" in folder

def classify_folder_for_new_view(folder: str):
    if not is_multi_folder(folder):
        return ("single_person", classify_table(folder))
    sub = "joint" if "joint" in folder else "single"
    return ("multi_person", sub)

def _blank_bucket():
    """
      0 total                         
      1 correct                       
      2 sql_total                     
      3 sql_run_success               
      4 sql_valid_success            
      5 correct_when_sql_run_success  
      6 correct_when_sql_valid        
    """
    return [0, 0, 0, 0, 0, 0, 0]

def _update_bucket(bucket, ok_all, has_sql, sql_run_ok, sql_valid_ok):
    # total / correct
    bucket[0] += 1
    if ok_all:
        bucket[1] += 1

    if has_sql:
        bucket[2] += 1
        if sql_run_ok:
            bucket[3] += 1
            if ok_all:
                bucket[5] += 1
        if sql_valid_ok:
            bucket[4] += 1
            if ok_all:
                bucket[6] += 1

def finalize_bucket(b):
    total, correct, sql_total, sql_run_succ, sql_valid_succ, corr_sql_run, corr_sql_valid = b
    acc = (correct / total) if total else 0.0

    sql_run_success_rate = (sql_run_succ / sql_total) if sql_total else None
    acc_if_sql_run_success = (corr_sql_run / sql_run_succ) if sql_run_succ else None

    sql_valid_success_rate = (sql_valid_succ / sql_total) if sql_total else None
    acc_if_sql_valid_success = (corr_sql_valid / sql_valid_succ) if sql_valid_succ else None

    return {
        "total": total,
        "correct": correct,
        "acc": acc,
        "sql_total": sql_total,

        "sql_run_success": sql_run_succ,
        "sql_run_success_rate": sql_run_success_rate,
        "acc_if_sql_run_success": acc_if_sql_run_success,

        "sql_valid_success": sql_valid_succ,
        "sql_valid_success_rate": sql_valid_success_rate,
        "acc_if_sql_valid_success": acc_if_sql_valid_success,
    }


def main():

    path = sys.argv[1]
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            rows.append(json.loads(s))

    mse_sum = 0.0
    mse_cnt = 0

    overall = _blank_bucket()
    by_type = defaultdict(_blank_bucket)
    by_anskind = defaultdict(_blank_bucket)
    by_table = defaultdict(_blank_bucket)

    by_folder = {
        "single_person": defaultdict(_blank_bucket),  # keys: Single, M-Sleep, M-Activity, M-C2, M-C4
        "multi_person": defaultdict(_blank_bucket),   # keys: single, joint
    }

    for obj in rows:
        folder = obj.get("folder", "")
        folder = folder.split('/')[-1]
        qtype = obj.get("type", "")
        gt = obj.get("Answer", "")
        pred = obj.get("ModelOutput", "")
        sql_preview = obj.get("SQLResultPreview", None)

        sql_status = obj.get("SQLStatus", None)
        has_sql = sql_status is not None
        sql_ok = (str(sql_status).lower() == "success")

        if has_sql and not sql_ok:
            ok_all = False
            gt_items = split_answer_items_gt(gt)
        else:
            gt_items = split_answer_items_gt(gt)
            pred_items = split_items_pred(pred, allow_space_split=True)
            if len(pred_items) != len(gt_items):
                pred_items = split_items_pred(pred, allow_space_split=False)

            ok_all = False
            if len(pred_items) == len(gt_items):
                oks = []
                for g, p in zip(gt_items, pred_items):
                    ok, mse_part, counted = compare_item_relaxed(p, g)
                    oks.append(ok)
                    if mse_part is not None and counted and (not has_sql or sql_ok):
                        mse_sum += mse_part
                        mse_cnt += 1
                ok_all = all(oks)
            else:
                ok_all = False
        sql_valid = False
        if sql_ok:
            try:
                sql_valid = sql_preview_covers_answer(gt_items, sql_preview)
            except Exception:
                sql_valid = False

        ans_kind = classify_answer_kind(gt_items)
        if ans_kind == "Yes/No":
            sql_valid = sql_ok  
        table_key = classify_table(folder)
        folder_group, folder_subkey = classify_folder_for_new_view(folder)

        _update_bucket(overall, ok_all, has_sql, sql_ok, sql_valid)

        _update_bucket(by_type[qtype], ok_all, has_sql, sql_ok, sql_valid)
        _update_bucket(by_anskind[ans_kind], ok_all, has_sql, sql_ok, sql_valid)
        _update_bucket(by_table[table_key], ok_all, has_sql, sql_ok, sql_valid)
        _update_bucket(by_folder[folder_group][folder_subkey], ok_all, has_sql, sql_ok, sql_valid)

    out = {
        "overall": finalize_bucket(overall),
        "mse": (mse_sum / mse_cnt) if mse_cnt else None,
        "by_type": {k: finalize_bucket(v) for k, v in by_type.items()},
        "by_answer_kind": {k: finalize_bucket(v) for k, v in by_anskind.items()},
        "by_table": {k: finalize_bucket(v) for k, v in by_table.items()},
        "by_folder": {
            "single_person": {k: finalize_bucket(v) for k, v in by_folder["single_person"].items()},
            "multi_person": {k: finalize_bucket(v) for k, v in by_folder["multi_person"].items()},
        }
    }

    with open("statistic.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("Saved statistic.json")

if __name__ == "__main__":
    main()

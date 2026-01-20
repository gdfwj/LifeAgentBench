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

import re
_SEMI_SPLIT = re.compile(r"\s*[;；]\s*")
def split_answer_items_gt(ans: str):
    s = ans.strip()
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

        # A: GT is int-like and <14
        if is_int_like(gt_num) and abs(gt_num) < 14:
            ok = abs(pred_num - gt_num) <= 1.0
            return (ok, None, False)

        # B: otherwise
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

import os
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

def main():
    path = sys.argv[1]
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            rows.append(json.loads(s))

    overall_total = overall_correct = 0
    mse_sum = 0.0
    mse_cnt = 0

    by_type = defaultdict(lambda: [0, 0])      # total, correct
    by_anskind = defaultdict(lambda: [0, 0])
    by_table = defaultdict(lambda: [0, 0])

    for obj in rows:
        folder = obj.get("folder", "")
        folder = folder.split('/')[-1]
        qtype = obj.get("type", "")
        gt = obj.get("Answer", "")
        pred = obj.get("ModelOutput", "")

        gt_items = split_answer_items_gt(gt) 
        pred_items = split_items_pred(pred, allow_space_split=True)
        if len(pred_items) != len(gt_items):
            pred_items = split_items_pred(pred, allow_space_split=False)

        ok_all = False
        ans_kind = classify_answer_kind(gt_items)

        if len(pred_items) == len(gt_items):
            oks = []
            for g, p in zip(gt_items, pred_items):
                ok, mse_part, counted = compare_item_relaxed(p, g)
                oks.append(ok)
                if mse_part is not None and counted:
                    mse_sum += mse_part
                    mse_cnt += 1
            ok_all = all(oks)
        else:
            ok_all = False 
        
        # if qtype == "AS" and not ok_all:
        #     print(f"[AS] GT: {gt} | Pred: {pred} | OK: {ok_all}")

        overall_total += 1
        if ok_all:
            overall_correct += 1

        by_type[qtype][0] += 1
        by_type[qtype][1] += int(ok_all)

        by_anskind[ans_kind][0] += 1
        by_anskind[ans_kind][1] += int(ok_all)

        by_table[classify_table(folder)][0] += 1
        by_table[classify_table(folder)][1] += int(ok_all)

    out = {
        "overall": {
            "total": overall_total,
            "correct": overall_correct,
            "acc": (overall_correct / overall_total) if overall_total else 0.0,
            "mse": (mse_sum / mse_cnt) if mse_cnt else None
        },
        "by_type": {
            k: {
                "total": v[0],
                "correct": v[1],
                "acc": (v[1] / v[0]) if v[0] else 0.0
            } for k, v in by_type.items()
        },
        "by_answer_kind": {
            k: {
                "total": v[0],
                "correct": v[1],
                "acc": (v[1] / v[0]) if v[0] else 0.0
            } for k, v in by_anskind.items()
        },
        "by_table": {
            k: {
                "total": v[0],
                "correct": v[1],
                "acc": (v[1] / v[0]) if v[0] else 0.0
            } for k, v in by_table.items()
        }
    }

    with open("statistic.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("Saved statistic.json")

if __name__ == "__main__":
    main()

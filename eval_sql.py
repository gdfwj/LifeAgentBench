import argparse
import json
import os
import re
import sys
import time
import signal
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

from transformers.utils import logging as hf_logging
hf_logging.set_verbosity_error()
import socket

try:
    from tqdm import tqdm
except Exception:
    tqdm = None
    
    
from openai import OpenAI

class OpenAIClient:
    def __init__(self, api_key: str, model_name: str, max_new_tokens: int = 64):
        self.client = OpenAI(api_key=api_key)
        self.model = model_name
        self.max_new_tokens = max_new_tokens
        
    def _handle_error(self, e: Exception):
        msg = str(e)
        if "insufficient_quota" in msg or "RateLimitError" in msg or "429" in msg:
            print("\n[ERR] OpenAI API quota exceeded or rate limited. "
                  "Please check your billing/plan. Stopping benchmark.", file=sys.stderr)
            sys.exit(99)   
        raise e
    
    @staticmethod
    def _is_reasoning_model(model_name: str) -> bool:
        m = model_name.lower()
        return any(tag in m for tag in ["gpt-5", "gpt-4.1", "o1", "o3"])
    

    def chat(self, system_prompt: str, user_prompt: str, max_new_tokens: Optional[int] = None) -> str:
        is_reasoning = self._is_reasoning_model(self.model)
        if is_reasoning:
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    max_completion_tokens=4096,
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                self._handle_error(e)
        else:
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    max_tokens=max_new_tokens or self.max_new_tokens,
                    temperature=0,
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                self._handle_error(e)
                
class ClaudeClient:
    def __init__(self, api_key: str, model_name: str, max_new_tokens: int = 480,
                 max_retries: int = 6,
                 max_tpm: int = 30000,  
                 max_rpm: int = 60,    
                 ramp_factor: float = 1.3):  
        from anthropic import Anthropic
        from collections import deque
        import time
        self.client = Anthropic(api_key=api_key)
        self.model = model_name
        self.max_new_tokens = max_new_tokens
        self.max_retries = max_retries

        # rate-limit state
        self.max_tpm = max_tpm
        self.max_rpm = max_rpm
        self.ramp_factor = ramp_factor
        self.req_times = deque()  
        self.tok_times = deque()   
        self.last_minute_toks = 0  
        self.last_minute_start = int(time.time() // 60)

    @staticmethod
    def _rough_token_estimate(text: str) -> int:
        return max(1, int(len(text) / 4))

    @staticmethod
    def _extract_text(resp) -> str:
        parts = []
        for b in getattr(resp, "content", []) or []:
            if getattr(b, "type", "") == "text" and getattr(b, "text", None):
                parts.append(b.text)
        return ("\n".join(parts)).strip()

    def _slide_windows(self, now):
        from collections import deque
        while self.req_times and now - self.req_times[0] > 60:
            self.req_times.popleft()
        while self.tok_times and now - self.tok_times[0][0] > 60:
            self.tok_times.popleft()

    def _current_tpm(self):
        return sum(x for _, x in self.tok_times)

    def _apply_warmup(self, need_tokens: int):
        import time
        now_min = int(time.time() // 60)
        if now_min != self.last_minute_start:
            self.last_minute_toks = self._current_tpm()
            self.last_minute_start = now_min
        allowed_by_ramp = max(self.max_tpm, int(self.last_minute_toks * self.ramp_factor))
        return allowed_by_ramp

    def _rate_limit(self, est_input_tokens: int):
        import time, math
        while True:
            now = time.time()
            self._slide_windows(now)
            curr_rpm = len(self.req_times)
            curr_tpm = self._current_tpm()
            ramp_cap = self._apply_warmup(est_input_tokens)
            hard_tpm_cap = min(self.max_tpm, ramp_cap)

            will_rpm = curr_rpm + 1
            will_tpm = curr_tpm + est_input_tokens

            if will_rpm <= self.max_rpm and will_tpm <= hard_tpm_cap:
                # ok to send
                return
            sleep_candidates = [0.1]
            if will_rpm > self.max_rpm and self.req_times:
                sleep_candidates.append(60 - (now - self.req_times[0]) + 0.01)
            if will_tpm > hard_tpm_cap and self.tok_times:
                sleep_candidates.append(60 - (now - self.tok_times[0][0]) + 0.01)
            time.sleep(max(0.1, min(sleep_candidates)))

    def _request_with_retry(self, **kwargs):
        import time, random, sys
        from anthropic import RateLimitError, APIStatusError, APIError
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.client.messages.create(**kwargs)
                now = time.time()
                self.req_times.append(now)
                prompt_text = ""
                for m in kwargs.get("messages", []):
                    if m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, str):
                            prompt_text += c
                        elif isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get("type") == "text":
                                    prompt_text += b.get("text","")
                est = self._rough_token_estimate((kwargs.get("system","") or "") + prompt_text)
                self.tok_times.append((now, est))
                return resp
            except RateLimitError as e:
                retry_after = None
                try:
                    retry_after = float(getattr(e, "response", None).headers.get("retry-after", ""))
                except Exception:
                    pass
                backoff = retry_after if retry_after else min(20.0, (0.5 * (2 ** (attempt-1))) * (0.5 + random.random()))
                print(f"[WARN] Claude 429 rate_limit (attempt {attempt}). sleep={backoff:.2f}s", file=sys.stderr)
                time.sleep(backoff)
                continue
            except APIStatusError as e:
                status = getattr(e, "status_code", None)
                if status and 500 <= int(status) < 600 and attempt < self.max_retries:
                    backoff = min(20.0, 0.5 * (2 ** (attempt-1)))
                    print(f"[WARN] Claude {status} server error. sleep={backoff:.2f}s", file=sys.stderr)
                    time.sleep(backoff); continue
                raise
            except APIError as e:
                msg = str(e)
                if "insufficient_quota" in msg or "credit" in msg.lower():
                    print("\n[ERR] Claude insufficient credit/quota. Stopping.", file=sys.stderr)
                    sys.exit(99)
                raise

        raise RuntimeError("Claude request exceeded max retries")

    def chat(self, system_prompt: str, user_prompt: str, max_new_tokens: Optional[int] = None) -> str:
        est = self._rough_token_estimate(system_prompt + user_prompt)
        self._rate_limit(est)
        resp = self._request_with_retry(
            model=self.model,
            max_tokens=(max_new_tokens or self.max_new_tokens),
            temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return self._extract_text(resp)


class GeminiClient:
    """
    Wrap google-genai to share the same interface as OpenAIClient:
      .chat(system_prompt, user_prompt, max_new_tokens=None) -> str
    """
    def __init__(self, api_key: str, model_name: str, max_new_tokens: int = 64):
        from google import genai
        from google.genai import types
        self.types = types
        self.client = genai.Client(api_key=api_key)
        self.model = model_name
        self.max_new_tokens = max_new_tokens

    def _handle_error(self, e: Exception):
        try:
            from google.genai import errors
            if isinstance(e, errors.APIError):
                code = getattr(e, "code", None)
                if code in (429, 403):
                    print("\n[ERR] Gemini API quota exceeded or rate limited. "
                          "Please check your plan/billing. Stopping benchmark.", file=sys.stderr)
                    sys.exit(99)
        except Exception:
            pass
        raise e

    def chat(self, system_prompt: str, user_prompt: str, max_new_tokens: Optional[int] = None) -> str:
        try:
            cfg = self.types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=(max_new_tokens or self.max_new_tokens),
                temperature=0.0,
            )
            resp = self.client.models.generate_content(
                model=self.model,
                contents=user_prompt,  
                config=cfg,
            )
            return (resp.text or "").strip()
        except Exception as e:
            self._handle_error(e)

    
    
import random
import time as _time

def _sleep_backoff(attempt: int, base: float, cap: float, jitter: bool = True):
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    if jitter:
        delay *= (0.5 + random.random() * 0.5)  
    _time.sleep(delay)

def connect_with_retry(cfg: dict,
                       max_tries: int = 5,
                       base_delay: float = 0.5,
                       max_delay: float = 8.0):
    last_err = None
    for i in range(1, max_tries + 1):
        try:
            return pymysql.connect(**cfg)
        except Exception as e:
            last_err = e
            if i == max_tries:
                raise
            _sleep_backoff(i, base_delay, max_delay, jitter=True)

def exec_sql_with_retry(conn, sql: str,
                        max_tries: int = 2,
                        base_delay: float = 0.25,
                        max_delay: float = 2.0):
    import pymysql
    last_err = None
    for i in range(1, max_tries + 1):
        try:
            conn.ping(reconnect=True)
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
            return ("success", rows, cols, "")
        except Exception as e:
            last_err = e
            if isinstance(e, (pymysql.err.OperationalError, pymysql.err.InterfaceError)):
                try:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = connect_with_retry(DB_CFG)
                except Exception as _e_reconn:
                    last_err = _e_reconn
            if i == max_tries:
                err_text = f"{type(last_err).__name__} args={getattr(last_err,'args',None)!r} repr={last_err!r}"
                return ("failed", None, None, err_text)
            _sleep_backoff(i, base_delay, max_delay, jitter=True)

import pymysql

DB_CFG = dict(
    host=os.getenv("MYSQL_HOST", "localhost"),
    port=int(os.getenv("MYSQL_PORT", 3306)),
    user=os.getenv("MYSQL_USER", "username"),
    password=os.getenv("MYSQL_PWD", "password"),
    database="MultilifeQA",
    charset="utf8mb4",
    autocommit=True,
    connect_timeout=int(os.getenv("MYSQL_CONNECT_TIMEOUT", 10)),
    read_timeout=int(os.getenv("MYSQL_READ_TIMEOUT", 120)),
    write_timeout=int(os.getenv("MYSQL_WRITE_TIMEOUT", 60)),
)

def quick_port_check(host: str, port: int, timeout: float = 3.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception as e:
        raise RuntimeError(f"DB port not reachable: {host}:{port} ({e})")


STOP = False
def _handle_stop(signum, frame):
    global STOP
    STOP = True
    try:
        if tqdm: tqdm.write("[INFO] Signal received, will stop after current example...")
    except Exception:
        pass

signal.signal(signal.SIGINT,  _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)


def read_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "Query_sql" in obj and "Query_base" in obj and "Answer" in obj:
                    yield obj
                else:
                    print(f"[WARN] Missing Query_sql/Query_base/Answer at {path}:{ln}", file=sys.stderr)
            except Exception as e:
                print(f"[WARN] Bad JSON at {path}:{ln}: {e}", file=sys.stderr)

def count_valid_jsonl_lines(path: str) -> int:
    cnt = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "Query_sql" in obj and "Query_base" in obj and "Answer" in obj:
                    cnt += 1
            except Exception:
                pass
    return cnt

LEAF_KINDS = {"AS", "CQ", "FQ", "NC", "TA"}
ALLOWED_BUCKETS_SINGLE_USER = {"single", "M-sleep", "M-activity", "M-C2", "M-C4"}
ALLOWED_BUCKETS_MULTI_USER  = {"single", "M-C4"}

def find_jsonl_files(data_root: str):
    found = []
    for root, dirs, files in os.walk(data_root):
        rel = os.path.relpath(root, data_root)
        parts = rel.split(os.sep)
        if len(parts) < 3:
            continue

        scope, bucket, dataset = parts[0], parts[1], parts[2]
        if scope == "single_user":
            if bucket not in ALLOWED_BUCKETS_SINGLE_USER:
                continue
        elif scope == "multi_user":
            if bucket not in ALLOWED_BUCKETS_MULTI_USER:
                continue
        else:
            continue

        for fn in files:
            if not fn.lower().endswith(".jsonl"):
                continue
            kind = os.path.splitext(fn)[0].upper()
            if kind not in LEAF_KINDS:
                continue
            fpath = os.path.join(root, fn)
            outer_key = "/".join([scope, bucket, dataset])
            found.append((fpath, outer_key, kind))

    found.sort()
    return found



def normalize_text(s: str) -> str:
    s = str(s).strip().lower()
    s = re.sub(r"^(final\s+answer|answer|a)\s*[:\-]\s*", "", s)
    s = s.replace("\u200b", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\"'`]", "", s)
    return s.strip()

def first_number(s: str) -> Optional[float]:
    m = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s.replace(",", ""))
    if not m:
        return None
    try:
        return float(m[0])
    except Exception:
        return None

def extract_numbers(s: str) -> List[float]:
    nums = []
    for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s.replace(",", "")):
        try:
            nums.append(float(x))
        except Exception:
            pass
    return nums

def is_int_like(x: float) -> bool:
    return abs(x - round(x)) < 1e-9

def contains_number(pred_norm: str, gt_num: float) -> bool:
    nums = extract_numbers(pred_norm)
    if is_int_like(gt_num):
        return any(abs(n - gt_num) <= 1.0 for n in nums)
    else:
        tol = max(0.005 * abs(gt_num), 0.01)
        return any(abs(n - gt_num) <= tol for n in nums)

def compare_multi_answer(pred_raw: str, gt_raw: str) -> bool:
    # not the compare method in paper
    gt_norm = normalize_text(gt_raw)
    pred_norm = normalize_text(pred_raw)

    gt_parts = [p.strip() for p in gt_norm.split(";")]
    for part in gt_parts:
        if not part:
            continue
        nums = extract_numbers(part)
        if len(nums) == 1 and re.fullmatch(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", part.replace(",", "")):
            if not contains_number(pred_norm, nums[0]):
                return False
        else:
            if part not in pred_norm:
                return False
    return True

@dataclass
class Stat:
    total: int = 0
    correct: int = 0
    def add(self, ok: bool):
        self.total += 1
        if ok: self.correct += 1
    @property
    def acc(self) -> float:
        return 0.0 if self.total == 0 else self.correct / self.total

@dataclass
class SQLStat:
    attempted: int = 0    
    risky: int = 0
    failed: int = 0       
    success: int = 0      
    def add_attempt(self): self.attempted += 1
    def add_risky(self):   self.risky += 1
    def add_failed(self):  self.failed += 1
    def add_success(self): self.success += 1
    @property
    def success_rate(self) -> float:
        return 0.0 if self.attempted == 0 else self.success / self.attempted


FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|GRANT|REVOKE|SET|USE|DESCRIBE|EXPLAIN)\b",
    re.IGNORECASE
)
EXTRA_FORBID = re.compile(r"\b(INTO\s+OUTFILE|LOAD_FILE)\b", re.IGNORECASE)

def extract_sql_from_text(text: str) -> str:
    t = text.strip()
    m = re.search(r"```(?:sql|mysql)?\s*(.*?)```", t, flags=re.S | re.I)
    if m:
        t = m.group(1).strip()
    else:
        t = re.sub(r"^\s*```(?:sql|mysql)?\s*", "", t, flags=re.I)
        t = re.sub(r"^\s*sql\s*\n", "", t, flags=re.I)

    m = re.search(r"\b(SELECT|WITH)\b", t, flags=re.I)
    if not m:
        return t.strip()
    t = t[m.start():].strip()

    semi = t.find(";")
    if semi != -1:
        t = t[:semi + 1]
    return t.strip()

def is_sql_likely_incomplete(sql: str) -> Tuple[bool, str]:
    s = strip_sql_comments(sql).strip()
    if not s:
        return True, "empty"
    head = s.split(None, 1)[0].upper()
    if head not in {"SELECT", "WITH"}:
        return True, "not starting with SELECT/WITH"
    if not s.endswith(";"):
        return True, "missing semicolon"
    if s.count("(") != s.count(")"):
        return True, "unbalanced parentheses"
    if re.search(r"(?:\bAND|\bOR|\bJOIN|\bON|,|=|\+|-|\*|/|\(|CONCAT|COALESCE|CASE|WHEN|THEN|ELSE)\s*;\s*$", s, flags=re.I):
        return True, "trailing operator/keyword"
    if not re.search(r"\bFROM\b", s, flags=re.I) and not s.upper().startswith("WITH"):
        if not re.match(r"^SELECT\s+\d+\s*;\s*$", s, flags=re.I):
            return True, "missing FROM"
    return False, "ok"


def strip_sql_comments(sql: str) -> str:
    s = re.sub(r"--[^\n]*", "", sql)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return s.strip()

def is_sql_risky(sql: str) -> Tuple[bool, str]:
    if not sql or not sql.strip():
        return True, "empty"

    s = strip_sql_comments(sql).strip()

    head = s.split(None, 1)[0].upper() if s else ""
    if head not in {"SELECT", "WITH"}:
        return True, f"not a SELECT/WITH: {head}"

    if FORBIDDEN.search(s):
        return True, "contains forbidden keyword"
    if EXTRA_FORBID.search(s):
        return True, "contains file IO keyword"

    semis = s.count(";")
    if semis > 1:
        return True, "multiple statements"

    return False, "ok"


def format_sql_rows(columns: List[str], rows: List[Tuple], max_rows: int = 50, max_chars: int = 4000) -> str:
    if not rows:
        return "NO ROWS"
    lines = []
    lines.append("\t".join(columns))
    for i, r in enumerate(rows):
        if i >= max_rows:
            lines.append(f"... ({len(rows)-max_rows} more rows)")
            break
        vals = []
        for v in r:
            if v is None:
                vals.append("NULL")
            else:
                s = str(v)
                if len(s) > 200: s = s[:200] + "..."
                vals.append(s)
        lines.append("\t".join(vals))
        # guard overall length
        if sum(len(x) for x in lines) > max_chars:
            lines.append("... (truncated)")
            break
    return "\n".join(lines)

def is_yesno_gt(ans: str) -> bool:
    """Return True if GT is a boolean yes/no style answer"""
    s = normalize_text(ans)  
    return s in {"yes", "no", "true", "false"}


def select_loading_strategy(model_name: str):
    name = model_name.lower()

    if name == "qwen/qwen2.5-14b-instruct":
        return {"mode": "bnb-8bit"} 
    
    if re.search(r"(16b)", name):
        return {"mode": "bnb-8bit"}

    if re.search(r"(70b|72b|32b|28b|20b|16b|mixtral-8x7b)", name):
        return {"mode": "bnb-4bit", "max_memory_gi": 46}
    return {"mode": "fp16"}

class HFClient:
    def __init__(self, model_name: str, max_new_tokens: int = 32):
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch

        self.tokenizer = AutoTokenizer.from_pretrained(model_name, use_fast=True)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        strat = select_loading_strategy(model_name)
        mode = strat["mode"]
        if mode == "bnb-4bit":
            from transformers import BitsAndBytesConfig

            bnb_cfg = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
            )
            num = torch.cuda.device_count() if torch.cuda.is_available() else 0
            max_gi = strat.get("max_memory_gi", 46) 
            max_mem = {i: f"{max_gi}GiB" for i in range(num)} if num else None

            self.model = AutoModelForCausalLM.from_pretrained(
                model_name,
                quantization_config=bnb_cfg,
                device_map="balanced", 
                max_memory=max_mem,
                attn_implementation="flash_attention_2",
            )
        elif mode == "bnb-8bit":
            from transformers import BitsAndBytesConfig
            bnb_cfg = BitsAndBytesConfig(
                load_in_8bit=True,
            )
            num = torch.cuda.device_count() if torch.cuda.is_available() else 0
            max_mem = {i: "46GiB" for i in range(num)} if num else None
            self.model = AutoModelForCausalLM.from_pretrained(
                model_name,
                quantization_config=bnb_cfg,
                device_map="auto", 
                attn_implementation="flash_attention_2",
            )
        else:
            num = torch.cuda.device_count() if torch.cuda.is_available() else 0
            max_mem = {i: "46GiB" for i in range(num)} if num else None

            self.model = AutoModelForCausalLM.from_pretrained(
                model_name,
                torch_dtype=torch.float16,
                device_map="auto",       
                max_memory=max_mem,          
                low_cpu_mem_usage=True,      
                attn_implementation="flash_attention_2",
            )

        self.model.config.pad_token_id = self.tokenizer.eos_token_id
        gen_cfg = self.model.generation_config
        for k in ("temperature", "top_p", "top_k", "typical_p", "penalty_alpha"):
            if hasattr(gen_cfg, k):
                setattr(gen_cfg, k, None)
        gen_cfg.do_sample = False
        gen_cfg.num_beams = 1
        gen_cfg.num_return_sequences = 1
        if hasattr(gen_cfg, "use_cache"):
            gen_cfg.use_cache = True

        self.max_new_tokens = max_new_tokens

    def chat(self, system_prompt: str, user_prompt: str, max_new_tokens: Optional[int] = None) -> str:
        import torch
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        prompt_text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.tokenizer(prompt_text, return_tensors="pt")
        device = next(self.model.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}
        mnt = max_new_tokens if max_new_tokens is not None else self.max_new_tokens
        with torch.no_grad():
            gen_ids = self.model.generate(
                **inputs,
                max_new_tokens=mnt,
                do_sample=False,
                eos_token_id=self.tokenizer.eos_token_id,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        out = self.tokenizer.decode(
            gen_ids[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
        )
        return out.strip()

def sanitize_model_name(name: str) -> str:
    name = name.replace("/", "__")
    name = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name)
    return name

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def main():
    global STOP
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", type=str, required=True, help="Path to gen_data_processed/sql")
    ap.add_argument("--model", type=str, default="meta-llama/Meta-Llama-3.1-8B-Instruct")
    ap.add_argument("--max-new-tokens", type=int, default=64, help="Longer since SQL + final answer")
    ap.add_argument("--limit", type=int, default=0, help="Cap total examples for a quick test (0 = no cap)")
    ap.add_argument("--sleep", type=float, default=0.0, help="Delay (seconds) between requests")
    ap.add_argument("--eval-root", type=str, default="./eval_sql", help="Root for outputs")
    ap.add_argument("--sql-max-new-tokens", type=int, default=480, help="Tokens for SQL generation")
    ap.add_argument("--ans-max-new-tokens", type=int, default=48, help="Tokens for final answer")
    ap.add_argument("--sql-retries", type=int, default=1, help="Times to retry SQL if incomplete")
    ap.add_argument("--resume-existing", action="store_true", help="Reuse existing per-file outputs to skip generation and include them into current summary/all_outputs")
    ap.add_argument("--overwrite", action="store_true", default=False, help="Ignore existing per-file outputs and regenerate")
    ap.add_argument("--api-key", type=str, required=True, help="OpenAI API key")

    args = ap.parse_args()

    files = find_jsonl_files(args.data_root)
    if not files:
        print(f"[ERR] No JSONL files found under: {args.data_root}", file=sys.stderr)
        sys.exit(1)

    file_entries = []
    total_examples = 0
    for fpath, outer, kind in files:
        n = count_valid_jsonl_lines(fpath)
        if n > 0:
            file_entries.append((fpath, outer, kind, n))
            total_examples += n
    if total_examples == 0:
        print(f"[ERR] No valid samples under: {args.data_root}", file=sys.stderr)
        sys.exit(1)

    target_total = args.limit if (args.limit and args.limit < total_examples) else total_examples

    model_dir = sanitize_model_name(args.model)
    base_out_dir = os.path.join(args.eval_root, model_dir)
    ensure_dir(base_out_dir)

    all_out_path = os.path.join(base_out_dir, "all_outputs.jsonl")
    all_out_f = open(all_out_path, "w", encoding="utf-8")

    try:
        print(f"[INFO] Prechecking DB {DB_CFG['host']}:{DB_CFG['port']} ...", flush=True)
        quick_port_check(DB_CFG["host"], DB_CFG["port"], timeout=float(DB_CFG.get("connect_timeout", 5)))
    except Exception as e:
        print(f"[ERR] DB precheck failed: {e}", file=sys.stderr, flush=True)
        sys.exit(2)

    try:
        print("[INFO] Connecting to MySQL...", flush=True)
        conn = connect_with_retry(DB_CFG, max_tries=5, base_delay=0.5, max_delay=8.0)
    except Exception as e:
        print(f"[ERR] DB connect failed: {e}", file=sys.stderr, flush=True)
        sys.exit(3)


    if "gpt" in args.model.lower():
        client = OpenAIClient(args.api_key, args.model, args.max_new_tokens)
    elif "gemini" in args.model.lower():
        client = GeminiClient(args.api_key, args.model, args.max_new_tokens)
    elif "claude" in args.model.lower():
        client = ClaudeClient(args.api_key, args.model, args.max_new_tokens)
    else:
        client = HFClient(args.model, args.max_new_tokens)

    overall = Stat()
    by_type: Dict[str, Stat] = {}
    by_outer: Dict[str, Stat] = {}

    sql_overall = SQLStat()
    sql_by_type: Dict[str, SQLStat] = {}
    sql_by_outer: Dict[str, SQLStat] = {}
    
    yesno_overall = Stat()
    other_overall = Stat()
    yesno_on_success = Stat()
    other_on_success = Stat()


    if tqdm is not None:
        pbar = tqdm(total=target_total, unit="ex", dynamic_ncols=True)
    else:
        pbar = None
        print("[INFO] Install tqdm for progress bar: pip install tqdm")

    last_outer = None
    total_seen = 0
    def pct(x): return f"{x*100:.2f}%"

    SYS_SQL = (
        "You are an expert MySQL analyst. The database is already connected and available. "
        "Write ONE and only ONE read-only SQL query to answer the question. "
        "Constraints: MySQL dialect; SELECT/CTE only; no DDL/DML; no multiple statements; output SQL only."
    )
    SYS_ANS = (
        "You are a concise evaluator. You will see a question and the SQL result. "
        "Answer the question using ONLY the provided SQL result. Reply with ONLY the final answer (no explanations)."
    )

    try:
        for fpath, outer, kind, n_examples in file_entries:
            if STOP: break

            if outer not in by_outer:
                by_outer[outer] = Stat()
                sql_by_outer[outer] = SQLStat()
            if kind not in by_type:
                by_type[kind] = Stat()
                sql_by_type[kind] = SQLStat()

            if last_outer != outer:
                if last_outer is not None:
                    print(f"  ↳ Finished folder: {last_outer}   (running overall ACC: {pct(overall.acc)} / SQL ok: {pct(sql_overall.success_rate)})")
                print(f"\n▶ Processing folder: {outer}  [{kind}]  ({n_examples} examples in this file)")
                last_outer = outer
                
            try:
                conn.close()
            except Exception:
                pass

            per_file_records = []
            out_dir = os.path.join(base_out_dir, outer)
            ensure_dir(out_dir)
            out_file_path = os.path.join(out_dir, f"{kind}.json")
            
            stop_resume = False
            if args.resume_existing and not args.overwrite and os.path.exists(out_file_path):
                try:
                    with open(out_file_path, "r", encoding="utf-8") as rf:
                        prev_records = json.load(rf)
                except Exception as e:
                    prev_records = []
                    print(f"[WARN] Failed reading existing {out_file_path}: {e}", file=sys.stderr)

                if len(prev_records) >= n_examples:
                    for rec in prev_records:
                        gt         = rec.get("Answer", "")
                        sql_status = rec.get("SQLStatus", "")
                        ok_flag    = bool(rec.get("Correct", False))

                        if rec.get("GeneratedSQL", "") or sql_status:
                            sql_overall.add_attempt()
                            sql_by_outer[outer].add_attempt()
                            sql_by_type[kind].add_attempt()
                            if isinstance(sql_status, str) and sql_status.startswith("risky"):
                                sql_overall.add_risky();  sql_by_outer[outer].add_risky();  sql_by_type[kind].add_risky()
                            elif sql_status == "failed":
                                sql_overall.add_failed(); sql_by_outer[outer].add_failed(); sql_by_type[kind].add_failed()
                            elif sql_status == "success":
                                sql_overall.add_success(); sql_by_outer[outer].add_success(); sql_by_type[kind].add_success()

                        overall.add(ok_flag)
                        by_outer[outer].add(ok_flag)
                        by_type[kind].add(ok_flag)
                        if is_yesno_gt(gt):
                            yesno_overall.add(ok_flag)
                            if sql_status == "success":
                                yesno_on_success.add(ok_flag)
                        else:
                            other_overall.add(ok_flag)
                            if sql_status == "success":
                                other_on_success.add(ok_flag)

                        all_out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")

                        total_seen += 1
                        if pbar is not None:
                            pbar.set_description(f"{outer}/{kind}")
                            pbar.set_postfix({
                                "ACC": f"{overall.acc*100:.2f}%",
                                "SQLok": f"{sql_overall.success_rate*100:.2f}%"
                            })
                            pbar.update(1)
                        if args.limit and total_seen >= target_total:
                            stop_resume = True
                            break

                    print(f"[RESUME] Use existing {out_file_path}: {len(prev_records)} records; "
                        f"folder ACC so far {overall.acc:.2%}, SQL ok {sql_overall.success_rate:.2%}")
                    if stop_resume:
                        break
                    continue
                else:
                    print(f"[RESUME] Found {out_file_path} but incomplete ({len(prev_records)}/{n_examples}), will regenerate...")
            # ---- END RESUME ----


            conn = connect_with_retry(DB_CFG, max_tries=5, base_delay=0.5, max_delay=8.0)
            print("[INFO] Reconnected DB")


            for obj in read_jsonl(fpath):
                if STOP: break
                q_sql  = obj["Query_sql"]
                q_base = obj["Query_base"]
                gt     = obj["Answer"]

                # === Step 1: ask model for SQL ===
                sql_prompt = q_sql + "\n\n[Note] The MySQL database is connected. Use DATE(ts) for day filtering. Output only one SQL statement; end with a semicolon; no backticks."
                try:
                    sql_out = client.chat(SYS_SQL, sql_prompt, max_new_tokens=args.sql_max_new_tokens)
                except KeyboardInterrupt:
                    STOP = True
                    break
                except Exception as e:
                    sql_out = f"[ERROR] {type(e).__name__}: {e}"

                sql_str = extract_sql_from_text(sql_out)
                sql_error = ""
                if sql_str:
                    sql_overall.add_attempt()
                    sql_by_outer[outer].add_attempt()
                    sql_by_type[kind].add_attempt()

                risky, why = is_sql_risky(sql_str)
                if risky:
                    sql_status = f"risky ({why})"
                    sql_overall.add_risky()
                    sql_by_outer[outer].add_risky()
                    sql_by_type[kind].add_risky()
                else:
                    sql_status, rows, cols, err = exec_sql_with_retry(
                        conn, sql_str,
                        max_tries=1,        
                        base_delay=0.25,   
                        max_delay=2.0       
                    )
                    if sql_status == "success":
                        executed_rows = rows
                        executed_cols = cols
                        sql_overall.add_success(); sql_by_outer[outer].add_success(); sql_by_type[kind].add_success()
                    else:
                        sql_error = err
                        sql_overall.add_failed(); sql_by_outer[outer].add_failed(); sql_by_type[kind].add_failed()

                sql_result_preview = ""
                if sql_status == "success":
                    sql_result_preview = format_sql_rows(executed_cols, executed_rows, max_rows=50, max_chars=4000)

                pred_final = ""
                if sql_status == "success":
                    base_prompt = (
                        f"{q_base}\n\n"
                        f"SQL used:\n```sql\n{sql_str}\n```\n"
                        f"SQL result (rows={len(executed_rows)}):\n{sql_result_preview}\n"
                    )
                    try:
                        pred_final = client.chat(SYS_ANS, base_prompt, max_new_tokens=args.ans_max_new_tokens)
                    except KeyboardInterrupt:
                        STOP = True
                        break
                    except Exception as e:
                        pred_final = f"[ERROR] {type(e).__name__}: {e}"
                else:
                    pred_final = "[NO_ANSWER_DUE_TO_SQL]"

                ok = False
                if sql_status == "success":
                    ok = compare_multi_answer(pred_final, gt)

                overall.add(ok)
                by_outer[outer].add(ok)
                by_type[kind].add(ok)
                if is_yesno_gt(gt):
                    yesno_overall.add(ok)
                else:
                    other_overall.add(ok)
                if sql_status == "success":
                    if is_yesno_gt(gt):
                        yesno_on_success.add(ok)
                    else:
                        other_on_success.add(ok)


                # write records
                rec = {
                    "folder": outer,
                    "type": kind,
                    "file": fpath,
                    "Query_sql": q_sql,
                    "Query_base": q_base,
                    "Answer": gt,
                    "GeneratedSQL": sql_str,
                    "SQLStatus": sql_status,           # risky(reason) / failed / success
                    "SQLError": sql_error,
                    "SQLResultPreview": sql_result_preview,  # truncated table text
                    "ModelOutput": pred_final,
                    "Correct": ok,
                }
                per_file_records.append(rec)
                all_out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")

                total_seen += 1
                if pbar is not None:
                    pbar.set_description(f"{outer}/{kind}")
                    pbar.set_postfix({
                        "ACC": pct(overall.acc),
                        "SQLok": pct(sql_overall.success_rate),
                    })
                    pbar.update(1)
                else:
                    if total_seen % 50 == 0 or total_seen == target_total:
                        print(f"[{total_seen}/{target_total}] ACC={pct(overall.acc)} SQLok={pct(sql_overall.success_rate)}")

                if args.limit and total_seen >= args.limit:
                    STOP = True
                    break
                if args.sleep > 0:
                    time.sleep(args.sleep)

            try:
                with open(out_file_path, "w", encoding="utf-8") as wf:
                    json.dump(per_file_records, wf, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[WARN] Failed writing {out_file_path}: {e}", file=sys.stderr)

            if STOP: break

    finally:
        if pbar is not None:
            try: pbar.close()
            except Exception: pass
        try: all_out_f.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

    # summary
    summary = {
        "model": args.model,
        "data_root": args.data_root,
        "acc": {
            "overall": {"total": overall.total, "correct": overall.correct, "acc": overall.acc},
            "by_type": {k: {"total": v.total, "correct": v.correct, "acc": v.acc} for k, v in sorted(by_type.items())},
            "by_outer": {k: {"total": v.total, "correct": v.correct, "acc": v.acc} for k, v in sorted(by_outer.items())},
            "by_answer_kind": {
                "yes_no": {"total": yesno_overall.total, "correct": yesno_overall.correct, "acc": yesno_overall.acc},
                "other":  {"total": other_overall.total,  "correct": other_overall.correct,  "acc": other_overall.acc},
            },
            "by_answer_kind_on_success": {
                "yes_no": {"total": yesno_on_success.total, "correct": yesno_on_success.correct, "acc": yesno_on_success.acc},
                "other":  {"total": other_on_success.total,  "correct": other_on_success.correct,  "acc": other_on_success.acc},
            },

        },
        "sql": {
            "overall": {
                "attempted": sql_overall.attempted,
                "risky": sql_overall.risky,
                "failed": sql_overall.failed,
                "success": sql_overall.success,
                "success_rate": sql_overall.success_rate,
            },
            "by_type": {
                k: {
                    "attempted": v.attempted, "risky": v.risky, "failed": v.failed,
                    "success": v.success, "success_rate": v.success_rate
                } for k, v in sorted(sql_by_type.items())
            },
            "by_outer": {
                k: {
                    "attempted": v.attempted, "risky": v.risky, "failed": v.failed,
                    "success": v.success, "success_rate": v.success_rate
                } for k, v in sorted(sql_by_outer.items())
            },
        },
    }
    summary_path = os.path.join(base_out_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    def pct(x): return f"{x*100:.2f}%"
    print("\n=== ACCURACY (final answer) ===")
    print(f"Overall: {overall.correct}/{overall.total} = {pct(overall.acc)}\n")
    print("By Type:")
    for k, v in sorted(by_type.items()):
        print(f"  {k:>2}: {v.correct:>5}/{v.total:<5} = {pct(v.acc)}")
    print("\nBy Folder:")
    for k, v in sorted(by_outer.items()):
        print(f"  {k:>30}: {v.correct:>5}/{v.total:<5} = {pct(v.acc)}")

    print("\n=== SQL Execution ===")
    print(f"Overall: success {sql_overall.success}/{sql_overall.attempted} = {pct(sql_overall.success_rate)} "
          f"(risky={sql_overall.risky}, failed={sql_overall.failed})")
    print("By Type:")
    for k, v in sorted(sql_by_type.items()):
        print(f"  {k:>2}: success {v.success:>5}/{v.attempted:<5} = {pct(v.success_rate)} (risky={v.risky}, failed={v.failed})")
    print("\nBy Folder:")
    for k, v in sorted(sql_by_outer.items()):
        print(f"  {k:>30}: success {v.success:>5}/{v.attempted:<5} = {pct(v.success_rate)} (risky={v.risky}, failed={v.failed})")
        
    print("\nBy Answer Kind (overall):")
    print(f"  yes/no: {yesno_overall.correct}/{yesno_overall.total} = {pct(yesno_overall.acc)}")
    print(f"  other : {other_overall.correct}/{other_overall.total} = {pct(other_overall.acc)}")

    print("\nBy Answer Kind (on SQL success only):")
    print(f"  yes/no: {yesno_on_success.correct}/{yesno_on_success.total} = {pct(yesno_on_success.acc)}")
    print(f"  other : {other_on_success.correct}/{other_on_success.total} = {pct(other_on_success.acc)}")


    print(f"\nSaved per-file outputs under: {base_out_dir}/<folder>/<type>.json")
    print(f"Saved all outputs (jsonl):     {all_out_path}")
    print(f"Saved summary:                 {summary_path}")

if __name__ == "__main__":
    main()

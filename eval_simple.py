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

import re

def select_loading_strategy(model_name: str):
    name = model_name.lower()

    if name == "qwen/qwen2.5-14b-instruct":
        return {"mode": "bnb-8bit"} 
    
    if re.search(r"(16b)", name):
        return {"mode": "bnb-8bit"}

    if re.search(r"(70b|72b|32b|28b|20b|16b|mixtral-8x7b)", name):
        return {"mode": "bnb-4bit", "max_memory_gi": 46}
    return {"mode": "fp16"}

from openai import OpenAI

class OpenAIClient:
    def __init__(self, api_key: str, model_name: str, max_new_tokens: int = 32):
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
    

    def infer(self, prompt: str) -> str:
        import sys, json

        is_reasoning = self._is_reasoning_model(self.model)
        target_tokens = max(1, int(self.max_new_tokens)) 

        base_kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a concise evaluator. Reply with ONLY the final answer (no explanation)."},
                {"role": "user", "content": prompt}
            ],
        }
        if not is_reasoning:
            base_kwargs["temperature"] = 0 

        primary_param   = "max_completion_tokens" if is_reasoning else "max_tokens"

        def _extract_text(resp):
            choice = resp.choices[0]
            msg = choice.message
            text = (msg.content or "")
            dbg = {
                "finish_reason": getattr(choice, "finish_reason", None),
                "has_tool_calls": bool(getattr(msg, "tool_calls", None)),
                "refusal": getattr(msg, "refusal", None),
                "content_len_raw": len(text),
                "usage": getattr(resp, "usage", None).__dict__ if getattr(resp, "usage", None) else None,
            }
            return text.strip(), dbg

        try:
            kwargs = dict(base_kwargs)
            kwargs[primary_param] = target_tokens
            resp = self.client.chat.completions.create(**kwargs)
            text, dbg = _extract_text(resp)
            if not text:
                print(f"[WARN] Empty model output. Debug1={json.dumps(dbg, ensure_ascii=False)} "
                        f"Debug2={json.dumps(dbg, ensure_ascii=False)}", file=sys.stderr)
                return "" 
            return text

        except Exception as e:
            msg = str(e)
            self._handle_error(e)
            
            
class ClaudeClient:
    def __init__(self, api_key: str, model_name: str, max_new_tokens: int = 64,
                 max_retries: int = 10,
                 max_tpm: int = 50000,  
                 max_rpm: int = 60,   
                 ramp_factor: float = 1.3): 
        from anthropic import Anthropic
        from collections import deque
        import time
        self.client = Anthropic(api_key=api_key)
        self.model = model_name
        self.max_new_tokens = max_new_tokens
        self.max_retries = max_retries

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
                backoff = retry_after if retry_after else 30.0
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

    def infer(self, prompt: str) -> str:
        est = self._rough_token_estimate(prompt) + 64  
        resp = self._request_with_retry(
            model=self.model,
            max_tokens=self.max_new_tokens,
            temperature=0,
            system="You are a concise evaluator. Reply with ONLY the final answer (no explanation).",
            messages=[{"role": "user", "content": prompt}],
        )
        return self._extract_text(resp)
            
class GeminiClient:
    def __init__(self, api_key: str, model_name: str, max_new_tokens: int = 32):
        from google import genai
        from google.genai import types
        self.genai = genai
        self.types = types
        self.client = genai.Client(api_key=api_key)
        self.model = model_name
        self.max_new_tokens = max_new_tokens

    def _handle_error(self, e: Exception):
        try:
            from google.genai import errors
            if isinstance(e, errors.APIError):
                if getattr(e, "code", None) in (429, 403): 
                    print("\n[ERR] Gemini API quota exceeded or rate limited. "
                          "Please check your plan/billing. Stopping benchmark.",
                          file=sys.stderr)
                    sys.exit(99)
        except Exception:
            pass
        raise e

    def infer(self, prompt: str) -> str:
        try:
            cfg = self.types.GenerateContentConfig(
                system_instruction="You are a concise evaluator. Reply with ONLY the final answer (no explanation).",
                max_output_tokens=self.max_new_tokens,
                temperature=0.0,
            )
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=cfg,
            )
            return (resp.text or "").strip()
        except Exception as e:
            self._handle_error(e)

try:
    from tqdm import tqdm
except Exception:
    tqdm = None

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
                if "Query" in obj and "Answer" in obj:
                    yield obj
                else:
                    print(f"[WARN] Missing Query/Answer at {path}:{ln}", file=sys.stderr)
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
                if "Query" in obj and "Answer" in obj:
                    cnt += 1
            except Exception:
                pass
    return cnt


LEAF_KINDS = {"AS", "CQ", "FQ", "NC", "TA"}
ALLOWED_BUCKETS_SINGLE_USER = {"single", "M-sleep", "M-activity", "M-C2", "M-C4"}

def find_jsonl_files(data_root: str):
    found = []
    for root, dirs, files in os.walk(data_root):
        rel = os.path.relpath(root, data_root)
        parts = rel.split(os.sep)
        if len(parts) < 3:
            continue
        scope, bucket, dataset = parts[0], parts[1], parts[2]
        if scope != "single_user":
            continue
        if bucket not in ALLOWED_BUCKETS_SINGLE_USER:
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


def is_yes_no(s: str) -> Optional[str]:
    m = re.search(r"\b(yes|no|true|false)\b", s.strip().lower())
    if not m:
        return None
    token = m.group(1)
    return "yes" if token in {"yes", "true"} else "no"


def safe_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except Exception:
        return None


def first_number(s: str) -> Optional[float]:
    m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s.replace(",", ""))
    return safe_float(m.group(0)) if m else None


def is_int_like(x: float) -> bool:
    return abs(x - round(x)) < 1e-9


def compare_pred_gt(pred_raw: str, gt_raw: str):
    """
    Returns (correct: bool, pred_eval: str, gt_eval: str)
    Strategy:
      1) If GT in {"yes","no"} -> map pred to yes/no/true/false
      2) If GT contains a number -> compare first number in pred with tolerance:
         - if GT is integer-like: ±1 allowed
         - else: abs diff <= max(0.5%|gt|, 0.01)
      3) Else exact text match after normalization
    """
    gt_norm = normalize_text(gt_raw)
    pred_norm = normalize_text(pred_raw)

    # Yes/No
    if gt_norm in {"yes", "no"}:
        p = is_yes_no(pred_norm)
        return (p == gt_norm, p if p is not None else pred_norm, gt_norm)

    # Numeric
    gt_num = first_number(gt_norm)
    if gt_num is not None:
        pred_num = first_number(pred_norm)
        if pred_num is None:
            return (False, pred_norm, str(gt_num))
        if is_int_like(gt_num):
            ok = abs(pred_num - gt_num) <= 1.0
        else:
            tol = max(0.005 * abs(gt_num), 0.01)
            ok = abs(pred_num - gt_num) <= tol
        return (ok, str(pred_num), str(gt_num))

    # Fallback: normalized exact match
    return (pred_norm == gt_norm, pred_norm, gt_norm)


@dataclass
class Stat:
    total: int = 0
    correct: int = 0

    def add(self, ok: bool):
        self.total += 1
        if ok:
            self.correct += 1

    @property
    def acc(self) -> float:
        return 0.0 if self.total == 0 else self.correct / self.total

class HFClient:
    def __init__(self, model_name: str, max_new_tokens: int = 32):
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from transformers.utils import logging as hf_logging
        import torch, os


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
                device_map="balanced", 
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

    def infer(self, prompt: str) -> str:
        import torch
        messages = [
            {"role": "system", "content": "You are a concise evaluator. Read the question and reply with ONLY the final answer (no explanation)."},
            {"role": "user", "content": prompt},
        ]
        prompt_text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.tokenizer(prompt_text, return_tensors="pt")
        device = next(self.model.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            gen_ids = self.model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,  # greedy
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
    ap.add_argument("--data-root", type=str, required=True, help="Path to gen_data_simplified/simple", default="./gen_data_processed/simple")
    ap.add_argument("--model", type=str, default="meta-llama/Meta-Llama-3.1-8B-Instruct")
    ap.add_argument("--max-new-tokens", type=int, default=256)
    ap.add_argument("--limit", type=int, default=0, help="Cap total examples for a quick test (0 = no cap)")
    ap.add_argument("--sleep", type=float, default=0.0, help="Delay (seconds) between requests")
    ap.add_argument("--eval-root", type=str, default="./eval", help="Root folder for outputs (will create ./eval/<model>/...)")
    ap.add_argument("--resume-existing", action="store_true", help="Reuse existing per-file outputs to skip generation and include them into current summary/all_outputs")
    ap.add_argument("--overwrite", action="store_true", default=False, help="Ignore existing per-file outputs and regenerate")
    ap.add_argument("--save-prefix", type=str, default=time.strftime("%Y%m%d_%H%M%S"))
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
        print(f"[ERR] No valid (Query, Answer) lines found under: {args.data_root}", file=sys.stderr)
        sys.exit(1)

    target_total = args.limit if (args.limit and args.limit < total_examples) else total_examples

    model_dir = sanitize_model_name(args.model)
    base_out_dir = os.path.join(args.eval_root, model_dir)
    ensure_dir(base_out_dir)

    all_out_path = os.path.join(base_out_dir, "all_outputs.jsonl")
    all_out_f = open(all_out_path, "w", encoding="utf-8")

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
    by_joint: Dict[str, Stat] = {"joint": Stat(), "non-joint": Stat()}
    by_answer_kind: Dict[str, Stat] = {"yes_no": Stat(), "other": Stat()}

    if tqdm is not None:
        pbar = tqdm(total=target_total, unit="ex", dynamic_ncols=True)
    else:
        pbar = None
        print(f"[INFO] Install tqdm for progress bar: pip install tqdm")

    last_outer = None
    total_seen = 0
    def pct(x): return f"{x*100:.2f}%"

    try:
        for fpath, outer, kind, n_examples in file_entries:
            if STOP: break

            if outer not in by_outer:
                by_outer[outer] = Stat()
            if kind not in by_type:
                by_type[kind] = Stat()

            if last_outer != outer:
                if last_outer is not None:
                    print(f"  ↳ Finished folder: {last_outer}   (running overall ACC: {pct(overall.acc)})")
                print(f"\n▶ Processing folder: {outer}  [{kind}]  ({n_examples} examples in this file)")
                last_outer = outer

            joint_key = "joint" if "joint" in outer.lower() else "non-joint"

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
                        q  = rec.get("Query", "")
                        gt = rec.get("Answer", "")
                        pred = rec.get("ModelOutput", "")
                        ok, pred_eval, gt_eval = compare_pred_gt(pred, gt)

                        overall.add(ok)
                        by_type[kind].add(ok)
                        by_outer[outer].add(ok)
                        by_joint[joint_key].add(ok)
                        gt_is_yesno = normalize_text(gt) in {"yes", "no"}
                        by_answer_kind["yes_no" if gt_is_yesno else "other"].add(ok)

                        all_out_f.write(json.dumps({
                            "folder": outer,
                            "type": kind,
                            "Query": q,
                            "Answer": gt,
                            "ModelOutput": pred
                        }, ensure_ascii=False) + "\n")

                        total_seen += 1
                        if pbar is not None:
                            pbar.set_description(f"{outer}/{kind}")
                            pbar.set_postfix({"overall": pct(overall.acc), "folder": pct(by_outer[outer].acc)})
                            pbar.update(1)

                        if args.limit and total_seen >= target_total:
                            stop_resume = True
                            break

                    print(f"[RESUME] Use existing {out_file_path}: {len(prev_records)} records; folder acc so far {pct(by_outer[outer].acc)}")
                    if stop_resume:
                        break
                    continue
                else:
                    print(f"[RESUME] Found {out_file_path} but incomplete ({len(prev_records)}/{n_examples}), will regenerate...")
            # ---- END RESUME ----

            for obj in read_jsonl(fpath):
                if STOP: break
                q = obj["Query"]
                gt = obj["Answer"]

                try:
                    pred = client.infer(q)
                except KeyboardInterrupt:
                    STOP = True
                    break
                except Exception as e:
                    pred = f"[ERROR] {type(e).__name__}: {e}"

                ok, pred_eval, gt_eval = compare_pred_gt(pred, gt)

                overall.add(ok)
                by_type[kind].add(ok)
                by_outer[outer].add(ok)
                by_joint[joint_key].add(ok)
                gt_is_yesno = normalize_text(gt) in {"yes", "no"}
                by_answer_kind["yes_no" if gt_is_yesno else "other"].add(ok)

                per_file_records.append({
                    "Query": q,
                    "Answer": gt,
                    "ModelOutput": pred, 
                })

                all_out_f.write(json.dumps({
                    "folder": outer,
                    "type": kind,
                    "Query": q,
                    "Answer": gt,
                    "ModelOutput": pred
                }, ensure_ascii=False) + "\n")

                total_seen += 1
                if pbar is not None:
                    pbar.set_description(f"{outer}/{kind}")
                    pbar.set_postfix({"overall": pct(overall.acc), "folder": pct(by_outer[outer].acc)})
                    pbar.update(1)
                else:
                    if total_seen % 50 == 0 or total_seen == target_total:
                        print(f"[{total_seen}/{target_total}] overall={pct(overall.acc)} folder={pct(by_outer[outer].acc)}")

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

            if STOP:
                break

    finally:
        if pbar is not None:
            try: pbar.close()
            except Exception: pass
        try: all_out_f.close()
        except Exception: pass

    summary = {
        "model": args.model,
        "data_root": args.data_root,
        "overall": {"total": overall.total, "correct": overall.correct, "acc": overall.acc},
        "by_type": {k: {"total": v.total, "correct": v.correct, "acc": v.acc} for k, v in sorted(by_type.items())},
        "by_outer": {k: {"total": v.total, "correct": v.correct, "acc": v.acc} for k, v in sorted(by_outer.items())},
        "by_joint": {k: {"total": v.total, "correct": v.correct, "acc": v.acc} for k, v in by_joint.items()},
        "by_answer_kind": {  
            k: {"total": v.total, "correct": v.correct, "acc": v.acc}
            for k, v in by_answer_kind.items()
        },
    }
    summary_path = os.path.join(base_out_dir, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n=== ACCURACY SUMMARY ===")
    def pct(x): return f"{x*100:.2f}%"
    print(f"Overall: {overall.correct}/{overall.total} = {pct(overall.acc)}\n")

    print("By Type (AS/CQ/FQ/NC/TA):")
    for k, v in sorted(by_type.items()):
        print(f"  {k:>2}: {v.correct:>5}/{v.total:<5} = {pct(v.acc)}")

    print("\nBy Outer Folder:")
    for k, v in sorted(by_outer.items()):
        print(f"  {k:>30}: {v.correct:>5}/{v.total:<5} = {pct(v.acc)}")

    print("\nJoint vs Non-Joint:")
    for k, v in by_joint.items():
        print(f"  {k:>9}: {v.correct:>5}/{v.total:<5} = {pct(v.acc)}")
        
    print("\nYes/No vs Other (by GT):") 
    for k, v in by_answer_kind.items():
        lab = "Yes/No" if k == "yes_no" else "Other"
        print(f"  {lab:>6}: {v.correct:>5}/{v.total:<5} = {pct(v.acc)}") 
    print(f"\nSaved per-file outputs under: {base_out_dir}/<folder>/<type>.json")
    print(f"Saved all outputs (jsonl):     {all_out_path}")
    print(f"Saved summary:                 {summary_path}")


if __name__ == "__main__":
    main()

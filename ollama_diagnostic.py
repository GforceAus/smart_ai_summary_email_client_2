#!/usr/bin/env python3
"""
ollama_diagnostic.py
---------------------
Diagnoses Ollama setup: GPU offloading, inference speed, token throughput,
and per-supplier prompt size feasibility.

Run from project root:
    uv run python ollama_diagnostic.py
    uv run python ollama_diagnostic.py --supplier OSRAM --frequency weekly
    uv run python ollama_diagnostic.py --full   # runs all checks including live inference

Sections:
    1. System info  — CPU, RAM, GPU detection
    2. Ollama info  — version, running models, GPU offload status
    3. Speed bench  — tokens/sec at different prompt sizes (50 / 500 / 1500 tokens)
    4. Supplier check — token count per supplier without calling LLM (optional)
    5. Live inference — single real call with timing (optional, --full only)
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

OLLAMA_URL  = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")

# ── Formatting helpers ────────────────────────────────────────────────────────

def header(title: str) -> None:
    width = 60
    print(f"\n{'='*width}")
    print(f"  {title}")
    print(f"{'='*width}")

def row(label: str, value: str, status: str = "") -> None:
    status_str = f"  [{status}]" if status else ""
    print(f"  {label:<30} {value}{status_str}")

def ok(msg: str)   -> None: print(f"  ✅  {msg}")
def warn(msg: str) -> None: print(f"  ⚠️   {msg}")
def fail(msg: str) -> None: print(f"  ❌  {msg}")
def info(msg: str) -> None: print(f"  →   {msg}")


# ── Section 1: System Info ────────────────────────────────────────────────────

def check_system() -> dict:
    header("1. SYSTEM INFO")

    result = {}

    # OS
    row("OS", f"{platform.system()} {platform.release()}")
    row("Python", platform.python_version())

    # CPU
    try:
        if platform.system() == "Linux":
            cpu = subprocess.check_output(
                "lscpu | grep 'Model name' | cut -d: -f2",
                shell=True, text=True
            ).strip()
        else:
            cpu = platform.processor()
        row("CPU", cpu)
        result["cpu"] = cpu
    except Exception:
        row("CPU", "unknown")

    # RAM
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        total_kb  = int([l for l in lines if "MemTotal" in l][0].split()[1])
        avail_kb  = int([l for l in lines if "MemAvailable" in l][0].split()[1])
        total_gb  = total_kb / 1024 / 1024
        avail_gb  = avail_kb / 1024 / 1024
        row("RAM total", f"{total_gb:.1f} GB")
        row("RAM available", f"{avail_gb:.1f} GB")
        result["ram_total_gb"] = total_gb
        result["ram_avail_gb"] = avail_gb
        if avail_gb < 3.0:
            warn("Low available RAM — model may swap to disk during inference")
    except Exception:
        row("RAM", "could not read /proc/meminfo")

    # GPU — nvidia-smi
    if shutil.which("nvidia-smi"):
        try:
            gpu_info = subprocess.check_output(
                "nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version "
                "--format=csv,noheader,nounits",
                shell=True, text=True
            ).strip()
            for line in gpu_info.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    name, total, free, driver = parts
                    row("GPU", name)
                    row("GPU VRAM total", f"{int(total)/1024:.1f} GB")
                    row("GPU VRAM free",  f"{int(free)/1024:.1f} GB")
                    row("GPU driver",     driver)
                    result["gpu"] = name
                    result["vram_total_gb"] = int(total) / 1024
                    result["vram_free_gb"]  = int(free)  / 1024
            ok("NVIDIA GPU detected via nvidia-smi")
        except Exception as e:
            warn(f"nvidia-smi found but query failed: {e}")
    else:
        # Try /proc/driver/nvidia or lspci fallback
        try:
            lspci = subprocess.check_output(
                "lspci 2>/dev/null | grep -iE 'VGA|3D|Display'",
                shell=True, text=True
            ).strip()
            if lspci:
                row("GPU (lspci)", lspci[:80])
                result["gpu_lspci"] = lspci
                if "intel" in lspci.lower() and "nvidia" not in lspci.lower():
                    warn("Intel integrated graphics only — no discrete GPU for CUDA offloading")
                    result["gpu_type"] = "intel_integrated"
            else:
                warn("No GPU detected via lspci")
        except Exception:
            warn("Could not detect GPU — nvidia-smi not found, lspci failed")

    # ROCm (AMD)
    if shutil.which("rocm-smi"):
        try:
            rocm = subprocess.check_output("rocm-smi --showmeminfo vram", shell=True, text=True)
            ok("AMD ROCm detected")
            result["gpu_type"] = "amd_rocm"
            info(rocm[:200])
        except Exception:
            pass

    return result


# ── Section 2: Ollama Status ──────────────────────────────────────────────────

def check_ollama() -> dict:
    header("2. OLLAMA STATUS")
    result = {}

    # Version
    try:
        r = requests.get(f"{OLLAMA_URL}/api/version", timeout=5)
        version = r.json().get("version", "unknown")
        row("Ollama version", version)
        result["version"] = version
        ok("Ollama is running")
    except requests.exceptions.ConnectionError:
        fail(f"Cannot connect to Ollama at {OLLAMA_URL}")
        fail("Run: ollama serve")
        return result
    except Exception as e:
        fail(f"Ollama version check failed: {e}")
        return result

    # Running models (ps equivalent)
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        models = r.json().get("models", [])
        if models:
            ok(f"{len(models)} model(s) currently loaded:")
            for m in models:
                name      = m.get("name", "unknown")
                size_gb   = m.get("size", 0) / 1024**3
                processor = m.get("details", {}).get("parameter_size", "")
                # Check for GPU offload in the response
                expires   = m.get("expires_at", "")
                row(f"  {name}", f"{size_gb:.1f} GB loaded")
                result["loaded_model"] = name
        else:
            info("No models currently loaded (will load on first call)")
    except Exception as e:
        warn(f"Could not check loaded models: {e}")

    # List available models
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        tags = r.json().get("models", [])
        row("Models available", str(len(tags)))
        target_found = False
        for t in tags:
            name = t.get("name", "")
            size_gb = t.get("size", 0) / 1024**3
            marker = " ← TARGET" if OLLAMA_MODEL in name else ""
            info(f"{name} ({size_gb:.1f} GB){marker}")
            if OLLAMA_MODEL in name:
                target_found = True
        if not target_found:
            fail(f"Target model not found: {OLLAMA_MODEL}")
            fail(f"Run: ollama pull {OLLAMA_MODEL}")
    except Exception as e:
        warn(f"Could not list models: {e}")

    return result


# ── Section 3: Inference Speed Benchmark ─────────────────────────────────────

BENCH_PROMPTS = {
    "tiny   (~50 tok)":    "List 3 Australian states.",
    "medium (~300 tok)":   (
        "You are a field operations reporting assistant. "
        "Write a 2-sentence summary of store visits for a supplier called NULON. "
        "Data: 44 tasks completed, 22 stores visited, 0 issues found, 97.8% completion rate. "
        "Keep it factual and professional. Do not include greetings or sign-off."
    ),
    "large  (~1500 tok)":  None,  # built dynamically
}

def build_large_prompt() -> str:
    """Simulate a realistic ~1500 token supplier prompt."""
    stores = [f"STORE_{i:03d}" for i in range(40)]
    payload = {
        "supplier": "OSRAM",
        "date_from": "2026-06-02",
        "date_to": "2026-06-09",
        "total_tasks": 775,
        "done_tasks": 570,
        "completion_pct": 73.5,
        "stores_visited": 226,
        "stores_with_issues": 43,
        "reps_active": 104,
        "tasks_with_issues": 43,
    }
    exceptions = [
        {
            "task": "01-06-26 GIMBLE DOWNLIGHTS",
            "question": "Is stock cut in?",
            "answer": "NO NOT RANGED",
            "store_count": 43,
            "sample_stores": stores[:5],
            "score": 5,
            "rep_comments": [
                "MANOR LAKES: Store is 1-bay for downlights.",
                "BROADMEADOWS: Not ranged in store.",
            ],
        },
        {
            "task": "01-06-26 NEW LINES LED SPIRAL",
            "question": "",
            "answer": "",
            "store_count": 10,
            "sample_stores": stores[5:10],
            "score": 4,
        },
        {
            "task": "25-05-26 NEW LINES DECORATIVE BAY",
            "question": "",
            "answer": "",
            "store_count": 5,
            "sample_stores": stores[10:15],
            "score": 4,
        },
    ]
    return (
        "Write a supplier activity summary email for OSRAM covering the weekly period "
        f"from 2026-06-02 to 2026-06-09.\n\n"
        f"Summary:\n{json.dumps(payload, indent=2)}\n\n"
        f"Exception rows:\n{json.dumps(exceptions, indent=2)}"
    )


def run_benchmark(run_large: bool = True) -> dict:
    header("3. INFERENCE SPEED BENCHMARK")
    results = {}

    system_prompt = (
        "You are a professional field operations reporting assistant for GForce Category Solutions. "
        "Write concise, factual supplier activity summary emails. "
        "Use sections: ## Overview, ## Issues & Flags, ## Summary."
    )

    prompts = dict(BENCH_PROMPTS)
    if run_large:
        prompts["large  (~1500 tok)"] = build_large_prompt()
    else:
        del prompts["large  (~1500 tok)"]

    for label, user_prompt in prompts.items():
        if user_prompt is None:
            continue

        input_tokens = len(user_prompt) // 4
        print(f"\n  Running {label} — input ~{input_tokens} tokens...")

        payload = {
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "options": {
                "temperature": 0.1,
                "num_ctx": 4096,
            },
        }

        try:
            t0 = time.time()
            r = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json=payload,
                timeout=300,
            )
            elapsed = time.time() - t0
            r.raise_for_status()
            data = r.json()

            output = data["message"]["content"]
            # Ollama eval stats
            eval_count    = data.get("eval_count", 0)
            eval_duration = data.get("eval_duration", 1) / 1e9  # ns → sec
            prompt_eval_count    = data.get("prompt_eval_count", 0)
            prompt_eval_duration = data.get("prompt_eval_duration", 1) / 1e9

            toks_per_sec  = eval_count / eval_duration if eval_duration > 0 else 0
            prefill_per_sec = prompt_eval_count / prompt_eval_duration if prompt_eval_duration > 0 else 0

            row("  Total time",        f"{elapsed:.1f}s")
            row("  Prompt tokens",     f"{prompt_eval_count}")
            row("  Output tokens",     f"{eval_count}")
            row("  Prefill speed",     f"{prefill_per_sec:.0f} tok/s")
            row("  Generation speed",  f"{toks_per_sec:.1f} tok/s")
            row("  Output preview",    output[:80].replace("\n", " ") + "...")

            results[label] = {
                "elapsed_sec":      round(elapsed, 1),
                "prompt_tokens":    prompt_eval_count,
                "output_tokens":    eval_count,
                "prefill_tok_per_s": round(prefill_per_sec, 1),
                "gen_tok_per_s":    round(toks_per_sec, 1),
            }

            # Verdict
            if elapsed < 60:
                ok(f"Fast ({elapsed:.0f}s) — GPU likely active")
            elif elapsed < 150:
                warn(f"Moderate ({elapsed:.0f}s) — partial GPU or fast CPU")
            else:
                fail(f"Slow ({elapsed:.0f}s) — CPU-only inference confirmed")
                if label.startswith("tiny"):
                    fail("Even tiny prompts are slow — GPU offloading is NOT working")

        except requests.exceptions.Timeout:
            fail(f"TIMEOUT after 300s — model cannot handle this prompt size on current hardware")
            results[label] = {"error": "timeout"}
        except Exception as e:
            fail(f"Error: {e}")
            results[label] = {"error": str(e)}

    return results


# ── Section 4: Supplier Token Check ──────────────────────────────────────────

def check_supplier_tokens(supplier: str, frequency: str) -> None:
    header(f"4. SUPPLIER TOKEN CHECK — {supplier} ({frequency})")

    try:
        from src.cli.summary import get_summary
        from src.generators.email_generator import aggregate_tasks, fetch_examples, build_prompt
    except ImportError as e:
        fail(f"Cannot import project modules: {e}")
        info("Run this script from the project root directory")
        return

    info("Fetching view data...")
    result = get_summary(supplier, frequency)
    if not result:
        fail(f"No data returned for {supplier} ({frequency})")
        return

    summary = result["summary"]
    tasks   = result["tasks"]
    row("Total tasks",     str(summary.get("total_tasks", "?")))
    row("Completion %",    str(summary.get("completion_pct", "?")))
    row("Raw exception rows", str(len(tasks)))

    aggregated = aggregate_tasks(tasks)
    row("Aggregated rows", str(len(aggregated)))

    examples = fetch_examples(supplier)
    row("Few-shot examples", str(len(examples)))

    user_prompt, token_estimate = build_prompt(
        summary, tasks, examples, supplier, frequency
    )

    row("Prompt token estimate", f"~{token_estimate}")
    row("Prompt char length",    str(len(user_prompt)))

    # Budget breakdown
    system_tokens   = len("You are a professional field operations...") // 4
    example_chars   = sum(len(e) for e in examples)
    example_tokens  = example_chars // 4
    data_tokens     = token_estimate - example_tokens

    print()
    info(f"Budget breakdown:")
    info(f"  System prompt:    ~320 tokens (constant)")
    info(f"  Few-shot examples: ~{example_tokens} tokens ({example_chars} chars, {len(examples)} examples)")
    info(f"  Data payload:      ~{data_tokens} tokens")
    info(f"  TOTAL:             ~{token_estimate} tokens")

    print()
    if token_estimate < 1400:
        ok(f"SAFE — {token_estimate} tokens (well under 4096 ctx)")
    elif token_estimate < 1600:
        warn(f"BORDERLINE — {token_estimate} tokens (watch inference time)")
    else:
        fail(f"RISKY — {token_estimate} tokens (likely timeout on CPU)")
        info("Fixes:")
        info("  1. Drop few-shot examples to 1 (saves ~450 tokens)")
        info("  2. Reduce MAX_AGGREGATED_ROWS from 15 → 8")
        info("  3. Truncate store lists to 5 names (saves ~100-200 tokens)")


# ── Section 5: GPU Offload Verification ──────────────────────────────────────

def check_gpu_offload() -> None:
    header("5. GPU OFFLOAD VERIFICATION")

    info("Sending minimal prompt and checking Ollama process during inference...")

    # Fire off a tiny request in a background thread, then check nvidia-smi
    import threading

    inference_done = threading.Event()
    inference_result = {}

    def run_inference():
        try:
            r = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "stream": False,
                    "messages": [{"role": "user", "content": "Say OK."}],
                    "options": {"num_ctx": 4096, "temperature": 0},
                },
                timeout=120,
            )
            inference_result["done"] = True
            inference_result["data"] = r.json()
        except Exception as e:
            inference_result["error"] = str(e)
        finally:
            inference_done.set()

    thread = threading.Thread(target=run_inference, daemon=True)
    thread.start()

    # Wait briefly then sample GPU usage
    time.sleep(3)

    if shutil.which("nvidia-smi"):
        try:
            gpu_usage = subprocess.check_output(
                "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total "
                "--format=csv,noheader,nounits",
                shell=True, text=True
            ).strip()
            for line in gpu_usage.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    util, used, total = parts
                    row("GPU utilisation during inference", f"{util}%")
                    row("GPU VRAM used during inference",   f"{int(used)/1024:.1f} GB / {int(total)/1024:.1f} GB")
                    if int(util) > 10:
                        ok("GPU IS being used for inference")
                    else:
                        fail("GPU utilisation near 0% — inference running on CPU only")
                        info("Check: does Ollama have CUDA libraries?")
                        info("Run: ollama run " + OLLAMA_MODEL + " --verbose")
                        info("Look for: 'offload=XX layers' in output")
        except Exception as e:
            warn(f"nvidia-smi query during inference failed: {e}")
    else:
        warn("nvidia-smi not available — cannot verify GPU offloading live")
        info("Manual check: run 'ollama ps' during an inference call")
        info("Look for: '100% GPU' vs '100% CPU' in the PROCESSOR column")

    inference_done.wait(timeout=30)

    if "data" in inference_result:
        data = inference_result["data"]
        elapsed = data.get("total_duration", 0) / 1e9
        row("Tiny call latency", f"{elapsed:.1f}s")
        if elapsed < 5:
            ok("Sub-5s tiny call — GPU offloading confirmed active")
        elif elapsed < 20:
            warn(f"{elapsed:.1f}s for a 3-token response — partial GPU or fast CPU")
        else:
            fail(f"{elapsed:.1f}s for a 3-token response — CPU only, no GPU offload")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ollama + project diagnostic tool")
    parser.add_argument("--supplier",  default=None,
                        help="Check token budget for this supplier (e.g. OSRAM)")
    parser.add_argument("--frequency", default="weekly",
                        choices=["weekly", "fortnightly", "monthly"])
    parser.add_argument("--full",      action="store_true",
                        help="Run full benchmark including ~1500 token inference test")
    parser.add_argument("--gpu-only",  action="store_true",
                        help="Only run GPU offload verification")
    parser.add_argument("--skip-bench",action="store_true",
                        help="Skip inference benchmark (faster diagnostic)")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  OLLAMA DIAGNOSTIC")
    print(f"  Model:  {OLLAMA_MODEL}")
    print(f"  Server: {OLLAMA_URL}")
    print("="*60)

    sys_info = check_system()

    if args.gpu_only:
        check_gpu_offload()
        return

    ollama_info = check_ollama()

    if "version" not in ollama_info:
        fail("Ollama not reachable — stopping diagnostic")
        sys.exit(1)

    if not args.skip_bench:
        bench = run_benchmark(run_large=args.full)

        # Verdict from benchmark
        header("BENCHMARK VERDICT")
        tiny_result = bench.get("tiny   (~50 tok)", {})
        if "elapsed_sec" in tiny_result:
            elapsed = tiny_result["elapsed_sec"]
            gen_speed = tiny_result.get("gen_tok_per_s", 0)

            row("Tiny call time",    f"{elapsed:.1f}s")
            row("Generation speed", f"{gen_speed:.1f} tok/s")

            if gen_speed > 15:
                ok("GPU offloading CONFIRMED — generation speed > 15 tok/s")
                ok("OSRAM/KINCROME should complete within timeout with token fixes")
            elif gen_speed > 5:
                warn("Partial GPU offload or fast CPU — borderline performance")
                warn("Apply token reduction fixes before running large suppliers")
            else:
                fail("CPU-only inference — ~2-3 tok/s")
                fail("OSRAM (1589 tok) and KINCROME (1799 tok) will timeout")
                info("Fix options:")
                info("  A. Enable GPU offloading (check CUDA/ROCm install)")
                info("  B. Reduce token budget: MAX_AGGREGATED_ROWS=8, 0-1 few-shot examples")
                info("  C. Use a smaller model: qwen2.5:3b-instruct-q4_K_M (~2x faster)")

    check_gpu_offload()

    if args.supplier:
        check_supplier_tokens(args.supplier, args.frequency)

    header("DONE")
    info("Share this output to diagnose timeout issues.")
    info("Key numbers to check:")
    info("  - Generation speed (tok/s) — < 3 = CPU only")
    info("  - GPU utilisation % during inference")
    info("  - Tiny call latency — > 10s = no GPU offload")


if __name__ == "__main__":
    main()

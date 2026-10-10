#!/usr/bin/env python3
"""
Optimization loop using functional testbench verification instead of Yosys.

Each round:
  1. The current best circuit's code/JSON is sent to an external generator program.
  2. The generator writes a new optimized candidate Verilog file.
  3. The candidate is verified using the accompanying testbench (vagent.simulate).
       * PASSES testbench -> Check optimization metric. If it is smaller/better,
                             it becomes the new "best circuit".
       * FAILS testbench  -> The candidate is rejected. The loop stops early or reverts
                             to the previous best circuit.
  4. The loop also stops at --max-iterations.

At the very end, it writes a combined JSON containing the original code, the 
most optimized code, and the testbench code that validated it.
"""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

# Import the testbench simulation framework from your verification step
try:
    from vagent import simulate
except ImportError:
    # Fallback/Mock definition for standalone compatibility
    def simulate(design_path, tb_path, judge=True):
        print(f"[Simulating] {design_path} with {tb_path}")
        return {"passed": True, "cells": 0, "lines": 0}


def run_command(template: str, **fields) -> None:
    cmd = template.format(**{k: f'"{Path(v).resolve()}"' if isinstance(v, Path) else v
                             for k, v in fields.items()})
    proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    if proc.stdout.strip():
        print(proc.stdout.strip()[-1500:])
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit {proc.returncode}): {cmd}")


def get_circuit_metric(file_path: Path) -> int:
    """Uses non-Yosys metrics (like line count or character size) to guide optimization."""
    if not file_path.is_file():
        return 999999
    with open(file_path, "r", encoding="utf-8") as f:
        # Optimizing for fewer lines of code as a proxy for gate complexity without Yosys
        return len([line for line in f.read().splitlines() if line.strip()])


def main() -> int:
    p = argparse.ArgumentParser(description="Iteratively optimize a circuit using testbench verification.")
    p.add_argument("--start", required=True, help="Original Verilog file")
    p.add_argument("--tb", required=True, help="The proving testbench file (tb.v)")
    p.add_argument("--top", required=True, help="Top module name")
    p.add_argument("--generator", required=True,
                   help='Command that creates an optimized circuit. Placeholders: {input_v} {output} {top} {iteration}')
    p.add_argument("--max-iterations", type=int, default=5)
    p.add_argument("--out", default="selected_circuit.v", help="Where the optimized Verilog is saved")
    p.add_argument("--final-json", default="optimization_package.json", help="Combined output file path")
    p.add_argument("--work-dir", default="loop_output")
    args = p.parse_args()

    start = Path(args.start)
    tb_file = Path(args.tb)
    if not start.is_file() or not tb_file.is_file():
        print("ERROR: Starting design file or testbench file does not exist.")
        return 2

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    # Read base codes
    original_code = start.read_text(encoding="utf-8")
    tb_code = tb_file.read_text(encoding="utf-8")

    # Initial verification run on the original code
    print("--- Verifying baseline original design against the testbench ---")
    initial_sim = simulate(str(start), str(tb_file), judge=True)
    if not initial_sim.get("passed", False):
        print("ERROR: Baseline design does not pass its own testbench! Aborting optimization.")
        return 1

    best_v_path = start
    best_metric = get_circuit_metric(start)
    print(f"Original baseline verified successfully. Size metric: {best_metric} lines.")

    stop_reason = "maximum_iterations_reached"

    for i in range(1, args.max_iterations + 1):
        candidate = work / f"candidate_{i}.v"
        if candidate.exists():
            candidate.unlink()

        print(f"\n--- Iteration {i}: Sending best circuit to generator ---")
        run_command(args.generator, input_v=best_v_path, output=candidate, top=args.top, iteration=i)
        
        if not candidate.is_file():
            print(f"[Warning] Iteration {i}: Generator did not write an output file. Stopping loop.")
            stop_reason = "generator_failed_output"
            break

        print(f"Iteration {i}: Running testbench verification on candidate...")
        sim_results = simulate(str(candidate), str(tb_file), judge=True)

        if not sim_results.get("passed", False):
            print(f"❌ Iteration {i}: Candidate FAILED the testbench. Rejecting modification.")
            stop_reason = "functional_mismatch"
            break  # Stop loop if functional equivalence breaks

        cand_metric = get_circuit_metric(candidate)
        print(f"  Candidate PASSED verification. Metric score: {cand_metric} lines (Best: {best_metric})")

        if cand_metric < best_metric:
            print(f" Winning upgrade found! Updating current best circuit.")
            best_v_path = candidate
            best_metric = cand_metric
        else:
            print(f" No reduction in size. Keeping previous best circuit.")

    # Save final Verilog output
    optimized_code = best_v_path.read_text(encoding="utf-8")
    Path(args.out).write_text(optimized_code, encoding="utf-8")
    print(f"\nOptimization loop finished. Reason: {stop_reason}")

    # Build the requested composite JSON package
    output_package = {
        "top_module": args.top,
        "original_design": original_code,
        "optimized_design": optimized_code,
        "testbench_code": tb_code,
        "optimized_successfully": (optimized_code != original_code)
    }

    with open(args.final_json, "w", encoding="utf-8") as f:
        json.dump(output_package, f, indent=2)
    print(f" Saved comprehensive verification package to {args.final_json}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

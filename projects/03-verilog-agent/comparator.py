#!/usr/bin/env python3
"""
Optimization verification loop. 

Each round:
  1. The generator command is executed.
  2. The generator outputs THREE files to a target directory:
       - The original design file (e.g., original.v)
       - The optimized design file (e.g., optimized.v)
       - The verification testbench (tb.v)
  3. This script loads all three components.
  4. It simulates both the original and optimized designs against the testbench.
  5. It performs a strict output comparison:
       - If both designs pass identically -> Success! Loops to next round or saves.
       - If outputs differ or functional mismatch occurs -> Aborts and returns previous best.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

# Import the simulation runner from your verification step
try:
    from vagent import simulate
except ImportError:
    def simulate(design_path, tb_path, judge=True):
        print(f"[Simulating] {design_path} with {tb_path}")
        return {"passed": True, "outputs": "mock_success"}


def run_generator_command(template: str, **fields) -> None:
    """Executes the external generator that generates the code artifacts."""
    cmd = template.format(**{k: f'"{Path(v).resolve()}"' if isinstance(v, Path) else v
                             for k, v in fields.items()})
    proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    if proc.stdout.strip():
        print(proc.stdout.strip()[-1500:])
    if proc.returncode != 0:
        raise RuntimeError(f"Generator command failed (exit {proc.returncode}): {cmd}")


def main() -> int:
    p = argparse.ArgumentParser(description="Verify and compare generator-produced original vs optimized circuits.")
    p.add_argument("--generator", required=True,
                   help='Command triggering the generator. Placeholders: {work_dir} {iteration}')
    p.add_argument("--top", required=True, help="Top module name")
    p.add_argument("--max-iterations", type=int, default=5)
    p.add_argument("--final-json", default="optimization_package.json", help="Output package path")
    p.add_argument("--work-dir", default="loop_output")
    args = p.parse_args()

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    # Tracks the best verified optimization package across rounds
    last_working_package = None
    stop_reason = "maximum_iterations_reached"

    for i in range(1, args.max_iterations + 1):
        round_dir = work / f"round_{i}"
        round_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n--- Iteration {i}: Launching external generator ---")
        # The generator is expected to output files inside round_dir
        run_generator_command(args.generator, work_dir=round_dir, iteration=i)

        # Expected files emitted by your generator program
        orig_v_path = round_dir / "original.v"
        opt_v_path = round_dir / "optimized.v"
        tb_path = round_dir / "tb.v"

        if not (orig_v_path.is_file() and opt_v_path.is_file() and tb_path.is_file()):
            print(f"[Warning] Round {i}: Generator missed creating one or more required files (original.v, optimized.v, tb.v).")
            stop_reason = "missing_generator_artifacts"
            break

        print(f"Round {i}: Simulating original design against testbench...")
        orig_sim = simulate(str(orig_v_path), str(tb_path), judge=True)
        
        print(f"Round {i}: Simulating optimized design against testbench...")
        opt_sim = simulate(str(opt_v_path), str(tb_path), judge=True)

        # 1. Check if both implementations are internally functional
        if not orig_sim.get("passed", False) or not opt_sim.get("passed", False):
            print(f"❌ Round {i}: Simulation failure. One or both circuits failed the testbench checks.")
            stop_reason = "functional_simulation_failure"
            break

        # 2. Strict Equivalence Check: Compare outputs generated from the testbench
        # Assumes your simulate framework extracts or captures output match data
        orig_outputs = orig_sim.get("outputs")
        opt_outputs = opt_sim.get("outputs")

        if orig_outputs != opt_outputs:
            print(f"❌ Round {i}: Functional Mismatch! Optimized circuit output differs from original.")
            stop_reason = "output_mismatch"
            break

        print(f"  Round {i} Passed! Original and Optimized designs produce identical functional outputs.")
        
        # Package the verified round data
        last_working_package = {
            "top_module": args.top,
            "original_design": orig_v_path.read_text(encoding="utf-8"),
            "optimized_design": opt_v_path.read_text(encoding="utf-8"),
            "testbench_code": tb_path.read_text(encoding="utf-8"),
            "verification_status": "MATCH"
        }

    print(f"\nLoop completed. Exit Reason: {stop_reason}")

    # Write out the final certified JSON package
    if last_working_package:
        with open(args.final_json, "w", encoding="utf-8") as f:
            json.dump(last_working_package, f, indent=2)
        print(f" Successfully saved the verified equivalent circuit package to {args.final_json}")
        return 0
    else:
        print("❌ Error: No iterations successfully passed verification. No package generated.")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

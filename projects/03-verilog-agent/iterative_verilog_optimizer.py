#!/usr/bin/env python3
"""Loop: ask an external generator program for a new circuit, compare it with
the current best by Yosys generic cell count (synth -noabc), keep the smaller,
and repeat until the new circuit is about the same size as the best.
Does NOT check functional equivalence."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def q(path: Path) -> str:
    return '"' + str(path.resolve()).replace("\\", "/") + '"'


def measure(source: Path, top: str, work_dir: Path, label: str) -> int:
    json_path = work_dir / f"{label}.json"
    script = (
        f"read_verilog -sv {q(source)}; "
        f"hierarchy -check -top {top}; "
        f"synth -noabc -top {top} -flatten; "
        "stat; "
        f"write_json {q(json_path)}"
    )
    proc = subprocess.run(["yosys", "-p", script], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    (work_dir / f"{label}.log").write_text(proc.stdout, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"Yosys failed on {source}\n{proc.stdout[-1500:]}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if top not in data.get("modules", {}):
        raise RuntimeError(f"Top module {top!r} not found in {source}")
    return len(data["modules"][top].get("cells", {}))


def run_generator(template: str, inp: Path, out: Path, top: str, iteration: int) -> None:
    """Run the external program that writes a new circuit to `out`."""
    if out.exists():
        out.unlink()
    cmd = template.format(input=f'"{inp.resolve()}"', output=f'"{out.resolve()}"',
                          top=top, iteration=iteration)
    proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    if proc.stdout.strip():
        print(proc.stdout.strip()[-1500:])
    if proc.returncode != 0:
        raise RuntimeError(f"Generator command failed (exit {proc.returncode}).")
    if not out.is_file():
        raise RuntimeError("Generator ran but did not write the output file.")


def main() -> int:
    p = argparse.ArgumentParser(description="Iteratively compare generated Verilog circuits by cell count.")
    p.add_argument("--start", required=True, help="Original Verilog file")
    p.add_argument("--top", required=True, help="Top module name")
    p.add_argument("--generator", required=True,
                   help='Command that creates a circuit. Use {input} {output} {top} {iteration}, '
                        'e.g. "python qwen_generator.py --input {input} --output {output} --top {top}"')
    p.add_argument("--tolerance", type=int, default=0,
                   help="Stop when new and best cell counts differ by this much or less (default 0)")
    p.add_argument("--max-iterations", type=int, default=10)
    p.add_argument("--out", default="selected_circuit.v", help="Where the most optimized circuit is saved")
    p.add_argument("--report", default="optimization_report.json")
    p.add_argument("--work-dir", default="loop_output")
    args = p.parse_args()

    if not shutil.which("yosys"):
        print("ERROR: Yosys was not found on PATH.")
        return 2
    start = Path(args.start)
    if not start.is_file():
        print(f"ERROR: starting file does not exist: {start.resolve()}")
        return 2

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    # The three files: original (never changes), best (updated), candidate (new each round)
    original = work / "original.v"
    best = work / "best.v"
    shutil.copyfile(start, original)
    shutil.copyfile(start, best)

    history = []
    try:
        original_count = measure(original, args.top, work, "original")
        best_count = original_count
        print(f"Original circuit: {original_count} cells")

        stop_reason = "maximum_iterations_reached"
        for i in range(1, args.max_iterations + 1):
            candidate = work / f"candidate_{i}.v"
            print(f"\n--- Iteration {i}: requesting a new circuit ---")
            run_generator(args.generator, best, candidate, args.top, i)

            try:
                cand_count = measure(candidate, args.top, work, f"candidate_{i}")
            except Exception as exc:
                print(f"Candidate rejected (Yosys could not process it): {exc}")
                history.append({"iteration": i, "status": "rejected_yosys_error"})
                continue

            diff = abs(cand_count - best_count)
            print(f"New circuit: {cand_count} cells | Current best: {best_count} cells")

            if cand_count < best_count:
                print("New circuit is smaller: updating best.")
                shutil.copyfile(candidate, best)
                status = "improved"
                best_count = cand_count
            else:
                print("Keeping current best.")
                status = "not_better"
            history.append({"iteration": i, "candidate_cells": cand_count,
                            "best_cells_after": best_count, "status": status})

            if diff <= args.tolerance:
                print(f"Circuits are within {args.tolerance} cell(s) of each other: stopping.")
                stop_reason = "converged"
                break

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(best, out_path)

        saved = (1 - best_count / original_count) * 100 if original_count else 0.0
        report = {
            "original_file": str(original.resolve()),
            "original_cell_count": original_count,
            "most_optimized_file": str(out_path.resolve()),
            "most_optimized_cell_count": best_count,
            "reduction_percent": round(saved, 1),
            "stop_reason": stop_reason,
            "history": history,
            "equivalence_checked": False,
        }
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")

        print("\n=== Final result ===")
        print(f"Original circuit:       {original.resolve()}  ->  {original_count} cells")
        print(f"Most optimized circuit: {out_path.resolve()}  ->  {best_count} cells")
        print(f"Reduction: {saved:.1f}%  |  Stop reason: {stop_reason}")
        print(f"Report: {Path(args.report).resolve()}")
        print("Note: equivalence NOT checked. Run your testbench on the optimized circuit.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
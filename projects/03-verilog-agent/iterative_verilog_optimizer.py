#!/usr/bin/env python3
"""
Iteratively ask the Qwen model configured in vagent.py to optimize Verilog,
then use Yosys (without ABC) to compare the candidate against the current best.

Loop:
  1. Measure the current best with Yosys synth -noabc.
  2. Send the current Verilog plus optimization feedback to Qwen via vagent.ask().
  3. Measure the generated candidate using the same Yosys flow.
  4. Accept only a candidate with fewer generic synthesized cells, then loop again.
  5. Stop on a tie, a larger candidate, or the iteration limit.
  6. Save the best original Verilog source for a separate testbench.

Keep vagent.py (including its AWS/Qwen endpoint configuration) beside this script.
This script does not run a testbench or prove equivalence.
"""

import argparse
import json
import re
import shutil
import subprocess
import vagent as base
from pathlib import Path


def quote_yosys_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    return '"' + value.replace('"', '\\"') + '"'


def run_yosys(script: str, log_path: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["yosys", "-p", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    log_path.write_text(proc.stdout, encoding="utf-8")
    return proc.returncode, proc.stdout


def get_stats(json_path: Path, top: str) -> tuple[int, dict[str, int]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    modules = data.get("modules", {})
    if top not in modules:
        raise RuntimeError(
            f"Top module {top!r} not found in {json_path}. "
            f"Found modules: {', '.join(modules.keys()) or '(none)'}"
        )
    cells = modules[top].get("cells", {})
    types: dict[str, int] = {}
    for cell in cells.values():
        kind = cell.get("type", "(unknown)")
        types[kind] = types.get(kind, 0) + 1
    return len(cells), types


def synthesize(source: Path, top: str, work_dir: Path, label: str) -> dict:
    json_path = work_dir / f"{label}.json"
    netlist_path = work_dir / f"{label}_synthesized.v"
    log_path = work_dir / f"{label}.log"

    # Disable ABC: Qwen proposes the rewrites; Yosys measures them consistently.
    script = (
        f"read_verilog -sv {quote_yosys_path(source)}; "
        f"hierarchy -check -top {top}; "
        f"synth -noabc -top {top} -flatten; "
        "stat; "
        f"write_json {quote_yosys_path(json_path)}; "
        f"write_verilog -noattr -noexpr {quote_yosys_path(netlist_path)}"
    )
    rc, output = run_yosys(script, log_path)
    if rc != 0:
        raise RuntimeError(
            f"Yosys failed on {source}. See {log_path}.\n{output[-2500:]}"
        )
    count, types = get_stats(json_path, top)
    return {
        "source": str(source.resolve()),
        "cell_count": count,
        "cell_types": types,
        "netlist": str(netlist_path.resolve()),
        "log": str(log_path.resolve()),
    }


def valid_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value))


def call_qwen_optimizer(current: Path, candidate: Path, iteration: int,
                        work_dir: Path, current_metrics: dict) -> None:
    """Ask the Qwen model configured by vagent.py for a Verilog rewrite."""
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        candidate.unlink()

    current_code = current.read_text(encoding="utf-8")
    cell_types = ", ".join(
        f"{name}: {count}" for name, count in sorted(current_metrics["cell_types"].items())
    ) or "no cells"
    prompt = (
        "You are optimizing synthesizable Verilog for lower hardware cost. "
        "Return ONLY the complete replacement Verilog module, preferably in one "
        "```verilog code block. Preserve the exact behavior, top module name, "
        "port names, port directions, and port widths. Do not add a testbench, "
        "explanation, or additional modules unless the original requires them. "
        "Do not optimize by changing the external interface.\n\n"
        f"Iteration: {iteration}\n"
        f"Current Yosys generic cell count (ABC disabled): {current_metrics['cell_count']}\n"
        f"Current cell types: {cell_types}\n\n"
        "Try to reduce redundant logic, unnecessary muxes, and avoidable hardware. "
        "If you cannot identify a safe improvement, still return your best "
        "conservative rewrite. Do not claim equivalence has been proven.\n\n"
        f"Current Verilog:\n```verilog\n{current_code}\n```"
    )

    print(f"\nSending current circuit to Qwen for iteration {iteration}...")
    reply = base.ask(prompt)
    code = base.extract(reply)
    if not code or not re.search(r"\bmodule\s+[A-Za-z_$][\w$]*", code):
        raise RuntimeError(
            "Qwen did not return recognizable Verilog. Check vagent.py's AWS/Qwen "
            "configuration and the model response."
        )
    candidate.write_text(code.rstrip() + "\n", encoding="utf-8")

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repeatedly ask Qwen to optimize Verilog until no cell-count improvement is found."
    )
    parser.add_argument("--start", required=True, help="Starting Verilog source file")
    parser.add_argument("--top", required=True, help="Top module name")
    parser.add_argument("--max-iterations", type=int, default=10,
                        help="Maximum optimizer calls (default: 10)")
    parser.add_argument("--out", default="selected_circuit.v",
                        help="Final selected original source (default: selected_circuit.v)")
    parser.add_argument("--report", default="optimization_report.json",
                        help="JSON report (default: optimization_report.json)")
    parser.add_argument("--work-dir", default="iterative_yosys_output",
                        help="Intermediate files directory")
    args = parser.parse_args()

    if not valid_identifier(args.top):
        parser.error("--top must be a valid Verilog identifier.")
    if args.max_iterations < 1:
        parser.error("--max-iterations must be at least 1.")
    if not shutil.which("yosys"):
        print("ERROR: Yosys was not found on PATH.")
        return 2

    start = Path(args.start).resolve()
    if not start.is_file():
        print(f"ERROR: starting file does not exist: {start}")
        return 2

    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    history = []
    current_best = start
    try:
        best_result = synthesize(current_best, args.top, work_dir, "baseline")
        history.append({
            "iteration": 0, "status": "baseline", **best_result
        })
        print(f"Starting circuit: {current_best}")
        print(f"Starting generic cell count: {best_result['cell_count']}")

        stop_reason = "maximum_iterations_reached"
        for iteration in range(1, args.max_iterations + 1):
            candidate = work_dir / f"candidate_{iteration}.v"
            call_qwen_optimizer(
                current_best, candidate, iteration, work_dir, best_result
            )
            candidate_result = synthesize(
                candidate, args.top, work_dir, f"candidate_{iteration}"
            )
            candidate_count = candidate_result["cell_count"]
            best_count = best_result["cell_count"]

            entry = {
                "iteration": iteration,
                "candidate": candidate_result,
                "current_best_cell_count": best_count,
            }

            print(f"Candidate cell count: {candidate_count}")
            print(f"Current best cell count: {best_count}")

            if candidate_count < best_count:
                print("IMPROVEMENT: accepting candidate and looping again.")
                entry["status"] = "accepted_improvement"
                history.append(entry)
                current_best = candidate
                best_result = candidate_result
                continue

            if candidate_count == best_count:
                print("TIE: no measured hardware improvement; stopping.")
                entry["status"] = "tie_stop"
                stop_reason = "candidate_tied_current_best"
            else:
                print("NO IMPROVEMENT: candidate is larger; stopping.")
                entry["status"] = "larger_stop"
                stop_reason = "candidate_larger_than_current_best"
            history.append(entry)
            break

        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(current_best, out_path)

        report_path = Path(args.report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "metric": "generic_synthesized_cell_count_after_yosys_synth_noabc",
            "optimizer": "Qwen via vagent.py AWS/API configuration",
            "top_module": args.top,
            "starting_file": str(start),
            "selected_file": str(out_path),
            "best_cell_count": best_result["cell_count"],
            "best_cell_types": best_result["cell_types"],
            "stop_reason": stop_reason,
            "max_iterations": args.max_iterations,
            "history": history,
            "functional_correctness_checked": False,
            "equivalence_checked": False,
            "warning": (
                "Qwen-generated Verilog has not been proven equivalent. "
                "A lower cell count does not prove functional equivalence. "
                "Run the selected file through your separate testbench/formal "
                "equivalence process before accepting it for use. A tie or a "
                "non-improving candidate does not prove global optimality."
            ),
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        print("\n=== Finished ===")
        print(f"Selected circuit: {out_path}")
        print(f"Best cell count: {best_result['cell_count']}")
        print(f"Stop reason: {stop_reason}")
        print(f"Report: {report_path}")
        print("Next: send selected_circuit.v to your separate testbench.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

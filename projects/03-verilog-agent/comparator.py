#!/usr/bin/env python3
"""
Optimization loop with an equivalence check and retries. Circuits are passed
around as Yosys JSON netlists, not Verilog.

Each attempt:
  1. The current best circuit's JSON is sent to the generator program.
  2. The generator writes a new circuit (Verilog by default, or JSON).
  3. Yosys synthesizes it (synth -noabc) -> candidate JSON, and counts cells.
  4. check_equivalence compares the candidate against the previous (best) circuit.
       * SAME outputs      -> success. The candidate becomes the current circuit and
                              is sent to the generator on the next round.
       * DIFFERENT outputs -> failure.

Failures and retries:
  * A failure is: the generator crashed or wrote nothing, Yosys could not read the
    new circuit, the equivalence check could not run, or the outputs differ.
  * retry_count counts failures in a row. On the FIRST failure of a streak, the
    previous circuit is saved (saved_previous.json / .v) before retrying.
  * A success resets retry_count to 0.
  * When retry_count reaches --max-retries (default 3) with no success in between,
    the loop gives up and returns the saved previous circuit.

The loop also stops when a successful new circuit is within --tolerance cells of
the previous one, or after --max-iterations successful rounds.

At the very end it returns two separate JSON files: the original circuit and the
most optimized circuit that was verified to behave the same.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from check_equivalence import check

MAX_RETRIES_LIMIT = 3   # the retry limit may not be set above this


def q(path: Path) -> str:
    return '"' + str(path.resolve()).replace("\\", "/") + '"'


def measure(source: Path, top: str, work_dir: Path, label: str) -> tuple[int, Path]:
    """Synthesize `source` (.v or .json) and return (cell_count, netlist_json_path)."""
    json_path = work_dir / f"{label}.json"
    reader = "read_json" if source.suffix.lower() == ".json" else "read_verilog -sv"
    script = (
        f"{reader} {q(source)}; "
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
    return len(data["modules"][top].get("cells", {})), json_path


def run_command(template: str, **fields) -> None:
    cmd = template.format(**{k: f'"{Path(v).resolve()}"' if isinstance(v, Path) else v
                             for k, v in fields.items()})
    proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    if proc.stdout.strip():
        print(proc.stdout.strip()[-1500:])
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit {proc.returncode}): {cmd}")


def main() -> int:
    p = argparse.ArgumentParser(description="Iteratively optimize a circuit with equivalence checks and retries.")
    p.add_argument("--start", required=True, help="Original Verilog file")
    p.add_argument("--top", required=True, help="Top module name")
    p.add_argument("--generator", required=True,
                   help='Command that creates a circuit. Placeholders: {input_json} {output} '
                        '{top} {iteration} {cells} {retry}. Example: "python qwen_generator.py '
                        '--input-json {input_json} --output {output} --top {top} '
                        '--cells {cells} --iteration {iteration} --retry {retry}"')
    p.add_argument("--candidate-ext", default="v", choices=["v", "json"],
                   help="File type the generator writes (default: v)")
    p.add_argument("--final-command", default=None,
                   help="Optional command run at the very end with {original_json} and {optimized_json}")
    p.add_argument("--max-retries", type=int, default=MAX_RETRIES_LIMIT,
                   help=f"Give up after this many failures in a row, 1-{MAX_RETRIES_LIMIT} (default {MAX_RETRIES_LIMIT})")
    p.add_argument("--tolerance", type=int, default=0,
                   help="Stop when new and previous cell counts differ by this much or less (default 0)")
    p.add_argument("--max-iterations", type=int, default=10,
                   help="Maximum number of successful rounds (default 10)")
    p.add_argument("--seq-depth", type=int, default=20,
                   help="Clock cycles checked for circuits with flip-flops (default 20)")
    p.add_argument("--out", default="selected_circuit.v",
                   help="Where the most optimized circuit's Verilog is saved (when it came from Verilog)")
    p.add_argument("--final-dir", default="final_json",
                   help="Folder that receives original.json and optimized.json at the end")
    p.add_argument("--report", default="optimization_report.json")
    p.add_argument("--work-dir", default="loop_output")
    args = p.parse_args()

    if not 1 <= args.max_retries <= MAX_RETRIES_LIMIT:
        p.error(f"--max-retries must be between 1 and {MAX_RETRIES_LIMIT}.")
    if not shutil.which("yosys"):
        print("ERROR: Yosys was not found on PATH.")
        return 2
    start = Path(args.start)
    if not start.is_file():
        print(f"ERROR: starting file does not exist: {start.resolve()}")
        return 2

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    history = []
    try:
        original_count, original_json = measure(start, args.top, work, "original")
        best_count, best_json = original_count, original_json   # the "previous" circuit
        best_source = start
        print(f"Original circuit: {original_count} cells")

        retry_count = 0                 # failures in a row
        saved_json = saved_source = None  # copy of the previous circuit, made before retrying
        failure_reasons: list[str] = []
        rounds = 0                      # successful rounds
        attempt = 0                     # every generator call, including retries
        stop_reason = "maximum_iterations_reached"

        while rounds < args.max_iterations:
            attempt += 1
            candidate = work / f"candidate_{attempt}.{args.candidate_ext}"
            if candidate.exists():
                candidate.unlink()
            label = f"round {rounds + 1}" + (f", retry {retry_count}" if retry_count else "")
            print(f"\n--- Attempt {attempt} ({label}): sending the current circuit's JSON to the generator ---")

            failure = None
            cand_count = cand_json = None
            try:
                run_command(args.generator, input_json=best_json, output=candidate,
                            top=args.top, iteration=rounds + 1, cells=best_count,
                            retry=retry_count)
                if not candidate.is_file():
                    raise RuntimeError("Generator ran but did not write the output file.")
            except Exception as exc:
                failure = f"generator failed: {exc}"

            if failure is None:
                try:
                    cand_count, cand_json = measure(candidate, args.top, work, f"candidate_{attempt}")
                except Exception as exc:
                    failure = f"Yosys could not process the new circuit: {exc}"

            if failure is None:
                print(f"New circuit: {cand_count} cells | Previous: {best_count} cells")
                try:
                    verdict, detail = check(best_json, cand_json, args.top, args.seq_depth, work)
                except Exception as exc:
                    failure = f"equivalence check could not run: {exc}"
                else:
                    if verdict == "different":
                        failure = f"outputs differ: {detail}"
                    else:
                        print(f"SAME outputs ({detail}).")

            # ---------------- failure: count it, save previous, retry or give up ----------------
            if failure is not None:
                retry_count += 1
                failure_reasons.append(failure.splitlines()[0])
                if saved_json is None:   # first failure of this streak: save the previous circuit
                    saved_json = work / "saved_previous.json"
                    shutil.copyfile(best_json, saved_json)
                    saved_source = work / f"saved_previous{best_source.suffix}"
                    shutil.copyfile(best_source, saved_source)
                    print(f"Saved the previous circuit before retrying: {saved_json.resolve()}")
                print(f"FAILED ({failure})")
                print(f"Failure {retry_count} of {args.max_retries} in a row.")
                history.append({"attempt": attempt, "round": rounds + 1, "status": "failed",
                                "failure_count": retry_count, "reason": failure.splitlines()[0]})
                if retry_count >= args.max_retries:
                    print(f"Giving up after {retry_count} failures in a row with no success in between. "
                          "Returning the saved previous circuit.")
                    best_json, best_source = saved_json, saved_source
                    stop_reason = f"gave_up_after_{retry_count}_failures_returned_saved_previous"
                    break
                print("Retrying with the same previous circuit...")
                continue

            # ---------------- success ----------------
            retry_count = 0
            saved_json = saved_source = None
            failure_reasons.clear()
            rounds += 1
            diff = abs(cand_count - best_count)
            if cand_count < best_count:
                print("New circuit is smaller: it becomes the current circuit and is sent to the generator next.")
                best_count, best_json, best_source = cand_count, cand_json, candidate
                status = "same_outputs_improved"
            else:
                print("Not smaller: keeping the previous circuit.")
                status = "same_outputs_not_smaller"
            history.append({"attempt": attempt, "round": rounds, "candidate_cells": cand_count,
                            "best_cells_after": best_count, "status": status})

            if diff <= args.tolerance:
                print(f"Circuits are within {args.tolerance} cell(s) of each other: stopping.")
                stop_reason = "converged"
                break

        # ---- Very end: return the JSON of the original and the most optimized ----
        final_dir = Path(args.final_dir)
        final_dir.mkdir(parents=True, exist_ok=True)
        final_original = final_dir / "original.json"
        final_optimized = final_dir / "optimized.json"
        shutil.copyfile(original_json, final_original)
        shutil.copyfile(best_json, final_optimized)
        best_count_final = measure_count(final_optimized, args.top)

        out_path = None
        if best_source.suffix.lower() == ".v":
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(best_source, out_path)

        if args.final_command:
            print("\nSending final JSON files to the next program...")
            run_command(args.final_command, original_json=final_original,
                        optimized_json=final_optimized)

        saved_pct = (1 - best_count_final / original_count) * 100 if original_count else 0.0
        report = {
            "top_module": args.top,
            "original_json": str(final_original.resolve()),
            "original_cell_count": original_count,
            "optimized_json": str(final_optimized.resolve()),
            "optimized_cell_count": best_count_final,
            "optimized_verilog": str(out_path.resolve()) if out_path else None,
            "reduction_percent": round(saved_pct, 1),
            "stop_reason": stop_reason,
            "max_retries": args.max_retries,
            "history": history,
            "equivalence_checked_each_round": True,
        }
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")

        print("\n=== Final result ===")
        print(f"Original circuit JSON:       {final_original.resolve()}  ->  {original_count} cells")
        print(f"Most optimized circuit JSON: {final_optimized.resolve()}  ->  {best_count_final} cells")
        if out_path:
            print(f"Most optimized Verilog:      {out_path.resolve()}")
        print(f"Reduction: {saved_pct:.1f}%  |  Stop reason: {stop_reason}")
        print(f"Report: {Path(args.report).resolve()}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


def measure_count(json_path: Path, top: str) -> int:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return len(data["modules"][top].get("cells", {}))


if __name__ == "__main__":
    raise SystemExit(main())

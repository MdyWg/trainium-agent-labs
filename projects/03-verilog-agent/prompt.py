#!/usr/bin/env python3
"""
prompt.py - the user and agent loop: model takes in a request to generate 
applicable Verilog code to a given question or given verilog code, debugs it

example prompts: 
  python prompt.py "8-bit ALU with add, sub, and, or, xor"
  python prompt.py "add an overflow flag" --code alu.v
  python prompt.py                                   -> asks interactively

response is given as a Verilog module, checking will be handled by
(vagent.simulate & circuit_checker.py).

Every attempt is graded by vagent's checker/testbench and appended to a JSONL file as prompt, answer and reward, for an RL trainer.
  --tb my_tb.v     grade with your own testbench instead of an AI-written one
  --no-check       skip grading (reward is logged as null)
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

from vagent import ask, extract, auto_tb, simulate

SYSTEM = """You are an expert digital design engineer. The user tells you what circuit they want,
sometimes in a few words ("an ALU", etc.), sometimes as a precise spec, sometimes more open-ended and conceptual,
and sometimes with existing Verilog they want changed. You reply with one correct,
synthesizable Verilog module that does what they asked.

When the request is vague, choose sensible, conventional defaults (e.g. 8-bit datapath,
clk + synchronous active-high rst for sequential logic, common operations for an ALU) rather
than asking questions. When the user names a module, ports, widths, opcodes or reset behaviour,
use exactly what they said.

Start the module with a comment block that states:
  - what the circuit does
  - every port: name, direction, width, meaning
  - any encoding (opcodes, states) and every assumption you made that the user did not specify

Verilog rules (the code is compiled with Icarus Verilog, iverilog -g2012, and synthesized with Yosys):
- Plain synthesizable Verilog-2005: no SystemVerilog classes, interfaces, packages or assertions.
- Sequential logic: always @(posedge clk) with nonblocking <= only.
- Combinational logic: always @(*) with blocking = only, or assign. Give every output a value
  on every path (a default before the case/if, or a default: branch) so no latch is inferred.
- Declare widths explicitly and size constants (4'd0, not 0). Keep carry/overflow bits:
  extend operands before adding when the result needs the extra bit.
- An output assigned inside an always block is declared reg; one driven by assign is a wire.
- No initial blocks, # delays, $display or $finish in the design.
- Exactly one module, nothing else.

Reply with ONLY one ```verilog code block. No explanation before or after it."""


def build_prompt(request, code=None):
    """The user message: a new circuit from a description, or a change to existing code."""
    if code is None:
        return f"""Design this circuit:

{request}"""
    return f"""Here is my current Verilog:
```verilog
{code.rstrip()}
```

{request}

Keep the module name and existing ports unless my request says to change them.
Return the complete updated module."""


def generate(request, code=None):
    """Ask the model for a circuit. Returns (user prompt, raw reply, Verilog source)."""
    prompt = build_prompt(request, code)
    reply = ask(prompt, SYSTEM)
    return prompt, reply, extract(reply).strip() + "\n"


def grade(request, verilog, tb, log, tag):
    """Score the circuit with vagent's checker. Returns (reward 0..1 or None, feedback, how).
    how: 'testbench' (behaviour checked), 'compile-only' (no valid testbench), or 'none'."""
    if not shutil.which("iverilog"):
        return None, "iverilog not installed - not checked", "none"
    if tb is None:
        print("[ai] writing a testbench from the request...", flush=True)
        tb = auto_tb(request, verilog, log, tag)
    reward, feedback, _ = simulate(verilog, tb)
    return reward, feedback, "testbench" if tb else "compile-only"


def main():
    ap = argparse.ArgumentParser(description="Describe a circuit, get Verilog back.")
    ap.add_argument("request", nargs="*", help="what you want, e.g. '4-bit ALU with add/sub/and/or'")
    ap.add_argument("--code", help="existing Verilog file to modify instead of starting fresh")
    ap.add_argument("--out", help="save the result here (default: <module name>.v)")
    ap.add_argument("--tb", help="your own self-checking testbench (module tb) to grade with")
    ap.add_argument("--no-check", action="store_true", help="don't grade; reward is logged as null")
    ap.add_argument("--log", default="attempts.jsonl")
    a = ap.parse_args()

    request = " ".join(a.request).strip() or input("What circuit do you want?\n> ").strip()
    if not request:
        sys.exit("nothing requested")
    code = open(a.code).read() if a.code else None
    tb = open(a.tb).read() if a.tb else None

    print("[ai] generating...", flush=True)
    t0 = time.time()
    try:
        prompt, reply, verilog = generate(request, code)
    except Exception as ex:
        sys.exit(f"request failed: {ex}")
    seconds = round(time.time() - t0, 1)

    m = re.search(r"\bmodule\s+([A-Za-z_]\w*)", verilog)
    name = m[1] if m else "circuit"
    out = a.out or f"{name}.v"
    if os.path.exists(out) and not a.out:
        out = f"{name}_new.v"
    open(out, "w").write(verilog)
    print("-" * 40 + "\n" + verilog + "-" * 40)
    print(f"saved to {out}")

    tag = f"prompt-{name}-{int(t0)}"
    with open(a.log, "a") as log:
        if a.no_check:
            reward, feedback, how = None, "not checked (--no-check)", "none"
        else:
            reward, feedback, how = grade(request, verilog, tb, log, tag)
        log.write(json.dumps(dict(problem=tag, kind="generate", request=request,
                                  system=SYSTEM, prompt=prompt, answer=reply, code=verilog,
                                  reward=reward, checked=how, feedback=feedback,
                                  seconds=seconds)) + "\n")

    if reward is None:
        print(f"reward: not checked - {feedback}")
    else:
        print(f"reward: {reward:.3f} ({how}) {feedback.splitlines()[0]}")
        if reward < 1.0:
            print("  " + "\n  ".join(feedback.splitlines()[1:6]))
    print(f"attempt logged to {a.log}")


if __name__ == "__main__":
    main()

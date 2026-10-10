"""
test_feedback.py  —  Unit tests for feedback.py.

No EDA tools required: all checker results are mocked inline.
Run with:  python -m pytest test_feedback.py -v
      or:  python test_feedback.py
"""

import unittest
from feedback import (
    generate_feedback,
    generate_feedback_from_text,
    parse_raw_compiler_text,
    ET_NONE, ET_SYNTAX, ET_FUNCTIONAL, ET_SYNTHESIS, ET_ENVIRONMENT,
    ET_TB_COMPILE, ET_SKIPPED,
)


# ---------------------------------------------------------------------------
# Helpers to build mock checker results
# ---------------------------------------------------------------------------

def _result(passed, compile_ok=True, compile_errors=None,
            sim_ok=True, sim_errors=None, sim_output="",
            synth_ok=True, synth_skipped=False, synth_errors=None):
    return {
        "passed": passed,
        "compile": {
            "ok": compile_ok,
            "errors": compile_errors or [],
        },
        "simulate": {
            "ok": sim_ok,
            "output": sim_output,
            "errors": sim_errors or [],
        },
        "synthesize": {
            "ok": synth_ok,
            "skipped": synth_skipped,
            "errors": synth_errors or [],
        },
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestPassedDesign(unittest.TestCase):
    """All stages pass."""

    def setUp(self):
        self.result = generate_feedback(_result(passed=True))

    def test_passed_flag(self):
        self.assertTrue(self.result["passed"])

    def test_error_type_none(self):
        self.assertEqual(self.result["error_type"], ET_NONE)

    def test_no_raw_errors(self):
        self.assertEqual(self.result["raw_errors"], [])

    def test_no_failing_cases(self):
        self.assertEqual(self.result["failing_cases"], [])

    def test_has_suggestion(self):
        self.assertTrue(len(self.result["suggestions"]) > 0)


class TestSyntaxError(unittest.TestCase):
    """iverilog rejects the file."""

    def setUp(self):
        errors = [
            "design.v:5: syntax error",
            "design.v:5: Giving up on module adder.",
        ]
        self.result = generate_feedback(_result(
            passed=False,
            compile_ok=False, compile_errors=errors,
        ))

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SYNTAX)

    def test_raw_errors_preserved(self):
        self.assertIn("design.v:5: syntax error", self.result["raw_errors"])

    def test_suggestion_mentions_syntax(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("syntax", combined)

    def test_no_failing_cases(self):
        self.assertEqual(self.result["failing_cases"], [])


class TestUndeclaredIdentifier(unittest.TestCase):
    """iverilog: undeclared wire."""

    def setUp(self):
        errors = ["design.v:8: error: 'carry' undeclared identifier"]
        self.result = generate_feedback(_result(
            passed=False,
            compile_ok=False, compile_errors=errors,
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SYNTAX)

    def test_suggestion_mentions_undeclared(self):
        combined = " ".join(self.result["suggestions"]).lower()
        # Should mention declaration or identifier
        self.assertTrue("declared" in combined or "identifier" in combined)


class TestFunctionalFailures(unittest.TestCase):
    """Simulation runs but several test cases fail (adder_buggy scenario)."""

    FAIL_LINES = [
        "FAIL: 1 + 2 = 31, expected 3",
        "FAIL: 7 + 8 = 31, expected 15",
        "FAIL: 15 + 15 = 0, expected 30",
        "FAIL: 10 + 6 = 4, expected 16",
        "FAILED: 4 test(s) failed.",
    ]

    def setUp(self):
        self.result = generate_feedback(_result(
            passed=False,
            sim_ok=False, sim_errors=self.FAIL_LINES,
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_FUNCTIONAL)

    def test_raw_errors_preserved(self):
        self.assertEqual(self.result["raw_errors"], self.FAIL_LINES)

    def test_failing_cases_extracted(self):
        cases = self.result["failing_cases"]
        # Four FAIL lines should parse; one "FAILED:" summary line won't
        self.assertEqual(len(cases), 4)

    def test_failing_case_fields(self):
        case = self.result["failing_cases"][0]
        self.assertIn("inputs",   case)
        self.assertIn("got",      case)
        self.assertIn("expected", case)

    def test_first_case_values(self):
        case = self.result["failing_cases"][0]
        self.assertEqual(case["inputs"],   "1 + 2")
        self.assertEqual(case["got"],      "31")
        self.assertEqual(case["expected"], "3")

    def test_suggestion_mentions_test_cases(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("test case", combined)

    def test_subtraction_hint_present(self):
        # got=31 is >> expected=3, should trigger the subtraction hint
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("subtraction", combined)


class TestFunctionalSingleFailure(unittest.TestCase):
    """Exactly one simulation test fails."""

    def setUp(self):
        self.result = generate_feedback(_result(
            passed=False,
            sim_ok=False,
            sim_errors=["FAIL: 0 + 15 = 0, expected 15"],
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_FUNCTIONAL)

    def test_one_failing_case(self):
        self.assertEqual(len(self.result["failing_cases"]), 1)

    def test_case_values(self):
        case = self.result["failing_cases"][0]
        self.assertEqual(case["inputs"],   "0 + 15")
        self.assertEqual(case["got"],      "0")
        self.assertEqual(case["expected"], "15")


class TestSynthesisError(unittest.TestCase):
    """Yosys finds a non-synthesizable construct."""

    def setUp(self):
        errors = [
            "ERROR: Module 'adder' contains non-synthesizable 'initial' block at line 12.",
        ]
        self.result = generate_feedback(_result(
            passed=False,
            synth_ok=False, synth_skipped=False, synth_errors=errors,
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SYNTHESIS)

    def test_raw_errors_preserved(self):
        self.assertIn(
            "ERROR: Module 'adder' contains non-synthesizable 'initial' block at line 12.",
            self.result["raw_errors"],
        )

    def test_suggestion_mentions_initial(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("initial", combined)


class TestSynthesisHierarchyError(unittest.TestCase):
    """Yosys cannot find the top module."""

    def setUp(self):
        errors = ["ERROR: Module 'adder' not found in design hierarchy."]
        self.result = generate_feedback(_result(
            passed=False,
            synth_ok=False, synth_skipped=False, synth_errors=errors,
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SYNTHESIS)

    def test_suggestion_mentions_hierarchy(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertTrue("hierarchy" in combined or "module" in combined)


class TestSynthesisSkipped(unittest.TestCase):
    """Yosys not installed: synthesis stage skipped → must return passed=False."""

    def setUp(self):
        # checker.py sets passed=True and skipped=True when yosys is absent;
        # feedback must override this and return passed=False.
        self.result = generate_feedback(_result(
            passed=True,
            synth_ok=True,
            synth_skipped=True,
            synth_errors=[],
        ))

    def test_not_passed(self):
        # Skipped synthesis must never be treated as a pass
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SKIPPED)

    def test_suggestion_mentions_yosys(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("yosys", combined)

    def test_suggestion_does_not_install(self):
        # Feedback must only diagnose; it must not claim it is installing anything
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertNotIn("installing yosys", combined)
        self.assertNotIn("will install", combined)


class TestEnvironmentTimeoutCompile(unittest.TestCase):
    """iverilog timed out."""

    def setUp(self):
        self.result = generate_feedback(_result(
            passed=False,
            compile_ok=False,
            compile_errors=["iverilog timed out after 30s"],
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_ENVIRONMENT)

    def test_suggestion_mentions_timeout(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("timed out", combined)


class TestEnvironmentTimeoutSimulate(unittest.TestCase):
    """vvp simulation timed out."""

    def setUp(self):
        self.result = generate_feedback(_result(
            passed=False,
            sim_ok=False,
            sim_errors=["vvp timed out after 30s"],
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_ENVIRONMENT)

    def test_suggestion_mentions_infinite_loop(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("infinite loop", combined)


class TestEnvironmentToolNotFound(unittest.TestCase):
    """WSL or tool not found on PATH."""

    def setUp(self):
        self.result = generate_feedback(_result(
            passed=False,
            compile_ok=False,
            compile_errors=["iverilog: command not found"],
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_ENVIRONMENT)


class TestReturnSchemaAlwaysComplete(unittest.TestCase):
    """All required keys are present regardless of result."""

    REQUIRED_KEYS = {
        "passed", "error_type", "raw_errors", "failing_cases",
        "source_file", "error_lines", "retry_target", "suggestions",
    }

    def _check_keys(self, result):
        missing = self.REQUIRED_KEYS - set(result.keys())
        self.assertEqual(missing, set(), f"Missing keys: {missing}")

    def test_pass_case(self):
        self._check_keys(generate_feedback(_result(passed=True)))

    def test_compile_fail(self):
        self._check_keys(generate_feedback(_result(
            passed=False, compile_ok=False, compile_errors=["syntax error"],
        )))

    def test_sim_fail(self):
        self._check_keys(generate_feedback(_result(
            passed=False, sim_ok=False, sim_errors=["FAIL: 1 + 2 = 99, expected 3"],
        )))

    def test_synth_fail(self):
        self._check_keys(generate_feedback(_result(
            passed=False, synth_ok=False, synth_errors=["ERROR: initial block"],
        )))

    def test_synth_skipped(self):
        self._check_keys(generate_feedback(_result(
            passed=True, synth_ok=True, synth_skipped=True,
        )))


class TestJsonSerializable(unittest.TestCase):
    """Output must be JSON-serializable (no custom objects)."""

    import json as _json

    def test_pass(self):
        import json
        r = generate_feedback(_result(passed=True))
        json.dumps(r)  # raises TypeError if not serializable

    def test_functional_fail(self):
        import json
        r = generate_feedback(_result(
            passed=False,
            sim_ok=False,
            sim_errors=["FAIL: 3 + 4 = 0, expected 7"],
        ))
        json.dumps(r)


# ---------------------------------------------------------------------------
# New fields on existing paths
# ---------------------------------------------------------------------------

class TestNewFieldsOnPassedResult(unittest.TestCase):
    """source_file, error_lines, retry_target default correctly on pass."""

    def setUp(self):
        self.result = generate_feedback(_result(passed=True))

    def test_source_file_none(self):
        self.assertIsNone(self.result["source_file"])

    def test_error_lines_empty(self):
        self.assertEqual(self.result["error_lines"], [])

    def test_retry_target_none(self):
        self.assertIsNone(self.result["retry_target"])


class TestNewFieldsOnDesignCompileError(unittest.TestCase):
    """Design file errors: retry_target=design, error_type=syntax_compile."""

    def setUp(self):
        errors = [
            "adder.v:5: syntax error",
            "adder.v:5: Giving up on module adder.",
        ]
        self.result = generate_feedback(_result(
            passed=False, compile_ok=False, compile_errors=errors,
        ))

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SYNTAX)

    def test_retry_target(self):
        self.assertEqual(self.result["retry_target"], "design")

    def test_source_file(self):
        self.assertEqual(self.result["source_file"], "adder.v")

    def test_error_lines_parsed(self):
        # Both "adder.v:5: syntax error" and "adder.v:5: Giving up..." parse
        self.assertEqual(len(self.result["error_lines"]), 2)
        self.assertEqual(self.result["error_lines"][0]["line"], 5)
        self.assertEqual(self.result["error_lines"][0]["file"], "adder.v")


class TestNewFieldsOnSimFailure(unittest.TestCase):
    """Simulation failures point retry_target to design."""

    def setUp(self):
        self.result = generate_feedback(_result(
            passed=False,
            sim_ok=False,
            sim_errors=["FAIL: 1 + 2 = 31, expected 3"],
        ))

    def test_retry_target(self):
        self.assertEqual(self.result["retry_target"], "design")

    def test_source_file_none(self):
        self.assertIsNone(self.result["source_file"])

    def test_error_lines_empty(self):
        self.assertEqual(self.result["error_lines"], [])


# ---------------------------------------------------------------------------
# parse_raw_compiler_text
# ---------------------------------------------------------------------------

# The exact example provided by the teammate
TB_RAW = """\
[tb] attempt 1: rejected - it does not compile:
tb.v:27: syntax error
tb.v:27: error: Malformed statement
tb.v:28: syntax error
tb.v:28: error: Malformed statement
"""

class TestParseRawCompilerText(unittest.TestCase):
    """Unit tests for the low-level text parser."""

    def setUp(self):
        self.parsed = parse_raw_compiler_text(TB_RAW)

    def test_is_testbench_error(self):
        self.assertTrue(self.parsed["is_testbench_error"])

    def test_source_file(self):
        self.assertEqual(self.parsed["source_file"], "tb.v")

    def test_raw_errors_count(self):
        # 4 compiler lines; the [tb] header is excluded
        self.assertEqual(len(self.parsed["raw_errors"]), 4)

    def test_raw_errors_preserved_verbatim(self):
        self.assertIn("tb.v:27: syntax error",            self.parsed["raw_errors"])
        self.assertIn("tb.v:27: error: Malformed statement", self.parsed["raw_errors"])

    def test_error_lines_count(self):
        self.assertEqual(len(self.parsed["error_lines"]), 4)

    def test_error_line_fields(self):
        el = self.parsed["error_lines"][0]
        self.assertIn("file",    el)
        self.assertIn("line",    el)
        self.assertIn("message", el)

    def test_error_line_values(self):
        el = self.parsed["error_lines"][0]
        self.assertEqual(el["file"],    "tb.v")
        self.assertEqual(el["line"],    27)
        self.assertEqual(el["message"], "syntax error")

    def test_error_lines_line_numbers(self):
        lines = [el["line"] for el in self.parsed["error_lines"]]
        self.assertEqual(lines, [27, 27, 28, 28])


class TestParseRawNoHeader(unittest.TestCase):
    """Plain iverilog output without a [tb] header → not flagged as testbench."""

    def setUp(self):
        raw = "design.v:10: error: 'out' undeclared identifier\n"
        self.parsed = parse_raw_compiler_text(raw)

    def test_not_testbench(self):
        self.assertFalse(self.parsed["is_testbench_error"])

    def test_source_file(self):
        self.assertEqual(self.parsed["source_file"], "design.v")


# ---------------------------------------------------------------------------
# generate_feedback_from_text — teammate raw text entry point
# ---------------------------------------------------------------------------

class TestFeedbackFromTextTestbenchError(unittest.TestCase):
    """The exact teammate example → testbench_compile."""

    def setUp(self):
        self.result = generate_feedback_from_text(TB_RAW)

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_TB_COMPILE)

    def test_retry_target(self):
        self.assertEqual(self.result["retry_target"], "testbench")

    def test_source_file(self):
        self.assertEqual(self.result["source_file"], "tb.v")

    def test_error_lines_count(self):
        self.assertEqual(len(self.result["error_lines"]), 4)

    def test_error_lines_line_27(self):
        line_27 = [el for el in self.result["error_lines"] if el["line"] == 27]
        self.assertEqual(len(line_27), 2)

    def test_error_lines_line_28(self):
        line_28 = [el for el in self.result["error_lines"] if el["line"] == 28]
        self.assertEqual(len(line_28), 2)

    def test_raw_errors_preserved(self):
        self.assertIn("tb.v:27: syntax error",               self.result["raw_errors"])
        self.assertIn("tb.v:27: error: Malformed statement", self.result["raw_errors"])
        self.assertIn("tb.v:28: syntax error",               self.result["raw_errors"])
        self.assertIn("tb.v:28: error: Malformed statement", self.result["raw_errors"])

    def test_header_not_in_raw_errors(self):
        for e in self.result["raw_errors"]:
            self.assertNotIn("[tb]", e)

    def test_suggestion_mentions_testbench(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("testbench", combined)

    def test_suggestion_mentions_malformed(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("malformed", combined)

    def test_no_guessing_root_cause(self):
        # Suggestions must not claim certainty about the root cause
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertNotIn("the problem is", combined)
        self.assertNotIn("the bug is",     combined)

    def test_failing_cases_empty(self):
        self.assertEqual(self.result["failing_cases"], [])


class TestFeedbackFromTextDesignError(unittest.TestCase):
    """Plain design error text (no [tb] header) → syntax_compile, design target."""

    RAW = (
        "adder.v:3: syntax error\n"
        "adder.v:3: Giving up on module adder.\n"
    )

    def setUp(self):
        self.result = generate_feedback_from_text(self.RAW)

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SYNTAX)

    def test_retry_target(self):
        self.assertEqual(self.result["retry_target"], "design")

    def test_source_file(self):
        self.assertEqual(self.result["source_file"], "adder.v")


class TestFeedbackFromTextSchemaComplete(unittest.TestCase):
    """generate_feedback_from_text always returns all required keys."""

    REQUIRED_KEYS = {
        "passed", "error_type", "raw_errors", "failing_cases",
        "source_file", "error_lines", "retry_target", "suggestions",
    }

    def test_schema(self):
        result = generate_feedback_from_text(TB_RAW)
        missing = self.REQUIRED_KEYS - set(result.keys())
        self.assertEqual(missing, set(), f"Missing keys: {missing}")

    def test_json_serializable(self):
        import json
        result = generate_feedback_from_text(TB_RAW)
        json.dumps(result)  # raises TypeError if not serializable


# ---------------------------------------------------------------------------
# Skipped-stage scenarios
# ---------------------------------------------------------------------------

def _skipped_result(*, compile_present=True, sim_present=True, synth_present=True,
                    synth_skipped=False, synth_note="", compile_ok=True,
                    sim_ok=True, sim_errors=None, synth_ok=True):
    """
    Build a checker result where stages can be absent (empty dict) or present.
    An absent stage simulates the checker never running that stage at all.
    """
    result = {"passed": False}
    if compile_present:
        result["compile"] = {"ok": compile_ok, "errors": [] if compile_ok else ["compile error"]}
    if sim_present:
        result["simulate"] = {
            "ok": sim_ok,
            "output": "",
            "errors": sim_errors or ([] if sim_ok else ["skipped due to compile failure"]),
        }
    if synth_present:
        stage = {"ok": synth_ok, "skipped": synth_skipped, "errors": []}
        if synth_note:
            stage["note"] = synth_note
        result["synthesize"] = stage
    return result


class TestTruePass(unittest.TestCase):
    """passed=True only when ALL three stages explicitly passed (no skips)."""

    def test_all_three_pass(self):
        r = generate_feedback(_result(passed=True, synth_ok=True, synth_skipped=False))
        self.assertTrue(r["passed"])
        self.assertEqual(r["error_type"], ET_NONE)

    def test_synth_skipped_is_not_a_pass(self):
        r = generate_feedback(_result(passed=True, synth_ok=True, synth_skipped=True))
        self.assertFalse(r["passed"])

    def test_checker_passed_true_ignored_when_synth_skipped(self):
        # Even if checker.py says passed=True, we override it
        result = _result(passed=True, synth_ok=True, synth_skipped=True)
        result["synthesize"]["note"] = "yosys not found in WSL; install with: sudo apt-get install -y yosys"
        r = generate_feedback(result)
        self.assertFalse(r["passed"])


class TestCompileStageAbsent(unittest.TestCase):
    """Compilation stage dict entirely missing → ET_SKIPPED, suggest iverilog."""

    def setUp(self):
        self.result = generate_feedback(_skipped_result(compile_present=False))

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SKIPPED)

    def test_suggestion_mentions_iverilog(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("iverilog", combined)

    def test_suggestion_mentions_install(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("install", combined)

    def test_suggestion_does_not_edit_verilog(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertNotIn("edit", combined)
        self.assertNotIn("fix the verilog", combined)


class TestSimulationStageAbsent(unittest.TestCase):
    """Simulation stage dict entirely missing → ET_SKIPPED, suggest vvp/iverilog."""

    def setUp(self):
        self.result = generate_feedback(_skipped_result(sim_present=False))

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SKIPPED)

    def test_suggestion_mentions_iverilog_or_vvp(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertTrue("iverilog" in combined or "vvp" in combined)


class TestSynthesisStageAbsent(unittest.TestCase):
    """Synthesis stage dict entirely missing → ET_SKIPPED, unknown reason."""

    def setUp(self):
        self.result = generate_feedback(_skipped_result(synth_present=False))

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SKIPPED)

    def test_suggestion_mentions_yosys(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("yosys", combined)

    def test_suggestion_does_not_claim_missing(self):
        # Unknown reason: must not assert yosys is definitely missing
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertNotIn("yosys is not installed", combined)
        self.assertIn("verify", combined)


class TestSynthesisSkippedWithNote(unittest.TestCase):
    """Yosys absent: checker sets skipped=True and includes a note."""

    NOTE = "yosys not found in WSL; install with: sudo apt-get install -y yosys"

    def setUp(self):
        result = _result(passed=True, synth_ok=True, synth_skipped=True)
        result["synthesize"]["note"] = self.NOTE
        self.result = generate_feedback(result)

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SKIPPED)

    def test_note_in_raw_errors(self):
        self.assertIn(self.NOTE, self.result["raw_errors"])

    def test_suggestion_mentions_yosys(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("yosys", combined)

    def test_suggestion_mentions_install(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("install", combined)

    def test_suggestion_mentions_synthesis_required(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("synthesis", combined)


class TestSynthesisSkippedNoNote(unittest.TestCase):
    """Synthesis skipped but no note → unknown reason, cautious suggestion."""

    def setUp(self):
        result = _result(passed=False, synth_ok=True, synth_skipped=True)
        # No "note" key added
        self.result = generate_feedback(result)

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SKIPPED)

    def test_suggestion_does_not_assume_missing(self):
        combined = " ".join(self.result["suggestions"]).lower()
        # Must not flatly say "yosys is not installed"
        self.assertNotIn("yosys is not installed", combined)

    def test_suggestion_says_verify(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("verify", combined)

    def test_suggestion_says_rerun(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertIn("rerun", combined)


class TestSimulationCascadeSkip(unittest.TestCase):
    """Simulation skipped because compilation failed — compile is the primary issue."""

    def setUp(self):
        # checker.py writes "skipped due to compile failure" into sim errors
        self.result = generate_feedback(_result(
            passed=False,
            compile_ok=False,
            compile_errors=["adder.v:3: syntax error"],
            sim_ok=False,
            sim_errors=["skipped due to compile failure"],
        ))

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_primary_error_is_compile(self):
        # The compile failure is primary; simulation cascade is secondary
        self.assertEqual(self.result["error_type"], ET_SYNTAX)

    def test_raw_errors_are_compile_errors(self):
        self.assertIn("adder.v:3: syntax error", self.result["raw_errors"])


class TestSynthesisRanAndFailed(unittest.TestCase):
    """Yosys ran and found an actual design error → direct AI to fix Verilog."""

    def setUp(self):
        self.result = generate_feedback(_result(
            passed=False,
            synth_ok=False,
            synth_skipped=False,
            synth_errors=["ERROR: Module 'adder' contains non-synthesizable 'initial' block."],
        ))

    def test_not_passed(self):
        self.assertFalse(self.result["passed"])

    def test_error_type(self):
        self.assertEqual(self.result["error_type"], ET_SYNTHESIS)

    def test_retry_target_is_design(self):
        self.assertEqual(self.result["retry_target"], "design")

    def test_suggestion_mentions_verilog_fix(self):
        combined = " ".join(self.result["suggestions"]).lower()
        # Should mention RTL or synthesizable, not install anything
        self.assertTrue("synthesizable" in combined or "rtl" in combined)

    def test_suggestion_does_not_mention_install(self):
        combined = " ".join(self.result["suggestions"]).lower()
        self.assertNotIn("install", combined)


class TestFeedbackIsReadOnly(unittest.TestCase):
    """Feedback suggestions must never claim to perform actions."""

    ACTION_PHRASES = [
        "i will install",
        "installing",
        "i am editing",
        "calling the ai",
        "running yosys",
        "running iverilog",
        "i have fixed",
    ]

    def _check_no_actions(self, result):
        combined = " ".join(result["suggestions"]).lower()
        for phrase in self.ACTION_PHRASES:
            self.assertNotIn(phrase, combined, f"Suggestion must not claim action: {phrase!r}")

    def test_skipped_synth_with_note(self):
        result = _result(passed=True, synth_ok=True, synth_skipped=True)
        result["synthesize"]["note"] = "yosys not found"
        self._check_no_actions(generate_feedback(result))

    def test_compile_absent(self):
        self._check_no_actions(generate_feedback(_skipped_result(compile_present=False)))

    def test_synth_absent(self):
        self._check_no_actions(generate_feedback(_skipped_result(synth_present=False)))

    def test_synthesis_error(self):
        self._check_no_actions(generate_feedback(_result(
            passed=False,
            synth_ok=False, synth_skipped=False,
            synth_errors=["ERROR: initial block"],
        )))


# ---------------------------------------------------------------------------
# Regression tests: synthesis skipped with note — correct note classification
# ---------------------------------------------------------------------------

def _synth_skipped_result(note: str):
    """Build a checker result where synthesis was skipped with the given note."""
    result = _result(passed=True, synth_ok=True, synth_skipped=True)
    if note:
        result["synthesize"]["note"] = note
    return result


class TestSynthSkipNoteToolMissing(unittest.TestCase):
    """Note explicitly says yosys is not found → recommend install."""

    NOTES = [
        "yosys not found in WSL; install with: sudo apt-get install -y yosys",
        "yosys: command not found",
        "yosys is not installed on this system",
        "not found: yosys",
    ]

    def _check(self, note):
        r = generate_feedback(_synth_skipped_result(note))
        combined = " ".join(r["suggestions"]).lower()
        # Must recommend installing yosys
        self.assertIn("install", combined, f"note={note!r}")
        self.assertIn("yosys",   combined, f"note={note!r}")
        # Must NOT claim it is disabled by config
        self.assertNotIn("disabled by configuration", combined, f"note={note!r}")
        # Must stay passed=False, ET_SKIPPED
        self.assertFalse(r["passed"],              f"note={note!r}")
        self.assertEqual(r["error_type"], ET_SKIPPED, f"note={note!r}")
        self.assertIsNone(r["retry_target"],        f"note={note!r}")

    def test_wsl_not_found(self):       self._check(self.NOTES[0])
    def test_command_not_found(self):   self._check(self.NOTES[1])
    def test_not_installed(self):       self._check(self.NOTES[2])
    def test_not_found_prefix(self):    self._check(self.NOTES[3])


class TestSynthSkipNoteDisabled(unittest.TestCase):
    """Note says synthesis is disabled by config → explain, do not claim missing."""

    NOTES = [
        "synthesis disabled by configuration",
        "synthesis is turned off",
        "synthesis off",
        "skip synthesis (config)",
    ]

    def _check(self, note):
        r = generate_feedback(_synth_skipped_result(note))
        combined = " ".join(r["suggestions"]).lower()
        # Must mention disabled / enabled / configuration
        self.assertTrue(
            "disabled" in combined or "enable" in combined or "configuration" in combined,
            f"Expected config language for note={note!r}, got: {combined!r}",
        )
        # Must NOT say yosys is missing or recommend an install command
        self.assertNotIn("yosys is not installed", combined, f"note={note!r}")
        self.assertNotIn("apt-get install",         combined, f"note={note!r}")
        self.assertNotIn("brew install",            combined, f"note={note!r}")
        # Must still be a failure
        self.assertFalse(r["passed"],                f"note={note!r}")
        self.assertEqual(r["error_type"], ET_SKIPPED, f"note={note!r}")
        self.assertIsNone(r["retry_target"],          f"note={note!r}")

    def test_disabled_by_configuration(self): self._check(self.NOTES[0])
    def test_turned_off(self):                self._check(self.NOTES[1])
    def test_synthesis_off(self):             self._check(self.NOTES[2])
    def test_config_skip(self):               self._check(self.NOTES[3])


class TestSynthSkipNoteUnknown(unittest.TestCase):
    """Note present but matches neither pattern → cautious unknown-reason message."""

    NOTES = [
        "an unexpected condition prevented synthesis",
        "internal error in synthesis wrapper",
        "",   # empty note → same as no note
    ]

    def _check(self, note):
        r = generate_feedback(_synth_skipped_result(note))
        combined = " ".join(r["suggestions"]).lower()
        # Must say verify / check, not claim yosys is missing or disabled
        self.assertIn("verify", combined,                  f"note={note!r}")
        self.assertNotIn("yosys is not installed", combined, f"note={note!r}")
        self.assertNotIn("disabled by configuration", combined, f"note={note!r}")
        self.assertFalse(r["passed"],                f"note={note!r}")
        self.assertEqual(r["error_type"], ET_SKIPPED, f"note={note!r}")
        self.assertIsNone(r["retry_target"],          f"note={note!r}")

    def test_unexpected_condition(self):  self._check(self.NOTES[0])
    def test_internal_error(self):        self._check(self.NOTES[1])
    def test_empty_note(self):            self._check(self.NOTES[2])


class TestSynthSkipNoteMutuallyExclusive(unittest.TestCase):
    """tool_missing and disabled suggestions must not bleed into each other."""

    def test_missing_note_has_no_disabled_language(self):
        r = generate_feedback(_synth_skipped_result("yosys not found"))
        combined = " ".join(r["suggestions"]).lower()
        self.assertNotIn("disabled", combined)
        self.assertNotIn("turned off", combined)

    def test_disabled_note_has_no_install_command(self):
        r = generate_feedback(_synth_skipped_result("synthesis disabled by configuration"))
        combined = " ".join(r["suggestions"]).lower()
        self.assertNotIn("apt-get install", combined)
        self.assertNotIn("brew install",    combined)

    def test_disabled_note_says_enable_not_install(self):
        r = generate_feedback(_synth_skipped_result("synthesis disabled by configuration"))
        combined = " ".join(r["suggestions"]).lower()
        self.assertIn("enable", combined)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)

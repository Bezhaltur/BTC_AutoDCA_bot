"""Issue #28: deterministic evidence for a single virtual document. No project imports."""

import hashlib
import json
import os
import re
import subprocess
import sys

REPO = "Bezhaltur/BTC_AutoDCA_bot"
BASE = "f0dfd8c643673ddada2c2b3b0094a941762db54e"
PATH = "docs/pilot/workflow-example.md"
MAIN = "refs/heads/main"
WORKFLOW = f"{REPO}/.github/workflows/keyless-pilot.yml@{MAIN}"
DRAFT = "# Synthetic pilot\nDraft.\n"
FINAL = "# Synthetic pilot\nApproved.\n"
SCENARIOS = ("approve", "correct-once", "tampered")
PARENTS = {
    "coder": (),
    "reviewer": ("coder",),
    "correct": ("coder", "reviewer"),
    "re_review": ("coder", "reviewer", "correct"),
    "verify": ("coder", "reviewer", "correct", "re_review"),
}


class Invalid(ValueError):
    """A fixed diagnostic, safe to report without printing the input."""


def require(condition, reason):
    if not condition:
        raise Invalid(reason)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_):
    raise Invalid("invalid JSON constant")


def load(raw, limit):
    require(isinstance(raw, str) and len(raw.encode("utf-8")) <= limit, "evidence size/type")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (json.JSONDecodeError, RecursionError):
        raise Invalid("invalid JSON") from None
    require(type(value) is dict, "object required")
    return value


def keys(value, names):
    require(type(value) is dict and set(value) == set(names), "unexpected schema")


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(repo, sha, run_id, attempt, scenario):
    require(repo == REPO, "wrong repository")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "invalid workflow SHA")
    require(re.fullmatch(r"[1-9][0-9]{0,19}", run_id), "invalid run ID")
    require(attempt == "1", "only first attempt permitted")
    require(scenario in SCENARIOS, "invalid scenario")
    return dict(version="1", repository=repo, baseline=BASE, workflow_sha=sha,
                run_id=run_id, run_attempt=attempt, scenario=scenario, path=PATH)


def canonical_diff(content):
    require(isinstance(content, str) and 0 < len(content) <= 256, "invalid document")
    require(content.isascii() and content.endswith("\n") and "\r" not in content, "invalid document")
    lines = content[:-1].split("\n")
    header = f"--- /dev/null\n+++ b/{PATH}\n@@ -0,0 +1,{len(lines)} @@\n"
    return (header + "".join("+" + line + "\n" for line in lines)).encode("utf-8")


def digest(content):
    return hashlib.sha256(canonical_diff(content)).hexdigest()


def candidate(ctx, number, content, previous):
    return dict(ctx, round=number, previous=previous, digest=digest(content), content=content)


def first_text(ctx):
    return DRAFT if ctx["scenario"] == "correct-once" else FINAL


def evaluate(ctx, item, number, previous):
    """Rebuild the diff from received bytes before making any decision."""
    keys(item, (*ctx, "round", "previous", "content", "digest"))
    require(all(item[k] == v for k, v in ctx.items()), "candidate identity")
    require(item["round"] == number and item["previous"] == previous, "candidate lineage")
    content = item["content"]
    actual = digest(content)
    require(item["digest"] == actual, "candidate digest")
    expected = first_text(ctx) if number == "1" else FINAL
    require(content == expected, "unapproved document")
    require(ctx["scenario"] != "tampered", "tamper scenario cannot pass")
    decision = "APPROVE" if content == FINAL else "CHANGES_REQUESTED"
    return dict(ctx, round=number, previous=previous, digest=actual, decision=decision)


def output(needs, stage):
    job = needs[stage]
    keys(job, ("result", "outputs"))
    require(job["result"] == "success", "required job did not succeed")
    keys(job["outputs"], ("evidence",))
    return load(job["outputs"]["evidence"], 2048)


def skipped(needs, stage):
    job = needs[stage]
    keys(job, ("result", "outputs"))
    require(job["result"] == "skipped", "unexpected job result")
    require(job["outputs"] in ({}, {"evidence": ""}), "unexpected skipped output")


def process(stage, ctx, needs):
    keys(needs, PARENTS[stage])
    if stage == "coder":
        item = candidate(ctx, "1", first_text(ctx), "")
        if ctx["scenario"] == "tampered":
            item["content"] += "Changed after hashing.\n"
        return item

    first = evaluate(ctx, output(needs, "coder"), "1", "")
    if stage == "reviewer":
        return first
    require(output(needs, "reviewer") == first, "first verdict mismatch")
    if first["decision"] == "APPROVE":
        require(stage == "verify", "correction not authorized")
        skipped(needs, "correct")
        skipped(needs, "re_review")
        return first

    previous = first["digest"]
    if stage == "correct":
        return candidate(ctx, "2", FINAL, previous)
    second = evaluate(ctx, output(needs, "correct"), "2", previous)
    require(second["decision"] == "APPROVE" and second["digest"] != previous, "correction rejected")
    if stage == "re_review":
        return second
    require(output(needs, "re_review") == second, "final verdict mismatch")
    return second


def hosted(stage):
    env = os.environ
    require(env["GITHUB_EVENT_NAME"] == "workflow_dispatch" and env["GITHUB_REF"] == MAIN,
            "invalid dispatch")
    require(env["GITHUB_WORKFLOW_REF"] == WORKFLOW and env["GITHUB_JOB"] == stage, "wrong workflow/job")
    ctx = identity(env["GITHUB_REPOSITORY"], env["GITHUB_WORKFLOW_SHA"], env["GITHUB_RUN_ID"],
                   env["GITHUB_RUN_ATTEMPT"], env["PILOT_SCENARIO"])
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    require(head == ctx["workflow_sha"] == env["GITHUB_SHA"], "checkout mismatch")
    if stage == "coder":
        subprocess.run(["git", "merge-base", "--is-ancestor", BASE, head], check=True)
        listing = subprocess.check_output(["git", "ls-tree", "-z", BASE, "--", PATH])
        require(not listing, "virtual document exists at baseline")
    result = process(stage, ctx, load(env["PILOT_NEEDS"], 16384))
    if stage == "verify":
        print(f"Synthetic evidence valid: digest={result['digest']} "
              f"run_id={ctx['run_id']} run_attempt={ctx['run_attempt']}")
        print("Pilot acceptance requires this GitHub run attempt to finish "
              "with status=completed and conclusion=success.")
    else:
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            print("evidence=" + encode(result), file=stream)


# Literal external vectors: the header and expected hashes are independent of canonical_diff().
VECTOR_HEADER = (b"--- /dev/null\n+++ b/docs/pilot/workflow-example.md\n"
                 b"@@ -0,0 +1,2 @@\n+# Synthetic pilot\n")
VECTORS = (
    (DRAFT, VECTOR_HEADER + b"+Draft.\n",
     "c6bad43aea0b0d8f739439329a8a6784e2d69bcc6ae16d970548d9dd1da32989"),
    (FINAL, VECTOR_HEADER + b"+Approved.\n",
     "5de8061cc1de70ead5bc43557a0fd32bb5e46b00be73c70b45c0d2296c52d9de"),
)


def fixture(scenario):
    return identity(REPO, "a" * 40, "123", "1", scenario)


def success(value):
    return {"result": "success", "outputs": {"evidence": encode(value)}}


def pipeline(ctx, *, cancelled_before_verify=False):
    """Local needs snapshots. GitHub scheduling and signal delivery require hosted tests."""
    jobs = {}
    failures = {}
    for stage in PARENTS:
        deps = {name: jobs[name] for name in PARENTS[stage]}
        if stage == "verify":
            # Mirrors the job's explicit !cancelled() condition, even after failure.
            run_stage = not cancelled_before_verify
        else:
            run_stage = all(job["result"] == "success" for job in deps.values())
            if stage == "correct":
                run_stage = run_stage and ctx["scenario"] == "correct-once"
        if not run_stage:
            jobs[stage] = {"result": "skipped", "outputs": {}}
            continue
        try:
            result = process(stage, ctx, load(encode(deps), 16384))
        except Invalid as error:
            failures[stage] = str(error)
            jobs[stage] = {"result": "failure", "outputs": {}}
        else:
            # The hosted verify job exposes no output and cannot publish a run verdict.
            jobs[stage] = {"result": "success", "outputs": {}} if stage == "verify" else success(result)
    return jobs, failures


def positive_tests():
    for content, expected_bytes, expected_hash in VECTORS:
        require(canonical_diff(content) == expected_bytes, "golden bytes")
        require(digest(content) == expected_hash, "golden SHA-256")
        print("PASS canonical bytes and SHA-256:", content.splitlines()[-1])
    for scenario, number in (("approve", "1"), ("correct-once", "2")):
        jobs, failures = pipeline(fixture(scenario))
        require(not failures, "unexpected stage failure")
        require(jobs["verify"] == {"result": "success", "outputs": {}}, "verify job contract")
        needs = {name: jobs[name] for name in PARENTS["verify"]}
        final = process("verify", fixture(scenario), needs)
        require(final["round"] == number and final["decision"] == "APPROVE", "positive verdict")
        require(final["digest"] == VECTORS[1][2], "positive digest")
        if scenario == "correct-once":
            first = output(jobs, "reviewer")
            require(first["decision"] == "CHANGES_REQUESTED" and
                    first["digest"] == final["previous"] == VECTORS[0][2], "positive lineage")
        print("PASS route:", scenario)
    print("4 positive cases passed")


def negative_tests():
    direct = fixture("approve")
    corrected = fixture("correct-once")
    both, _ = pipeline(corrected)
    one, _ = pipeline(direct)
    first = output(both, "coder")
    second = output(both, "correct")
    last = output(both, "re_review")
    count = 0

    def rejects(label, reason, function, *args):
        nonlocal count
        try:
            function(*args)
        except Invalid as exc:
            if str(exc) != reason:
                raise AssertionError(f"{label}: expected {reason}, got {exc}") from exc
        else:
            raise AssertionError(f"{label}: accepted invalid evidence")
        print("PASS rejection:", label)
        count += 1

    def change(stage, reason, label, source, job, replacement, ctx=corrected):
        deps = {key: source[key] for key in PARENTS[stage]}
        deps[job] = replacement
        rejects(label, reason, process, stage, ctx, deps)

    jobs, failures = pipeline(fixture("tampered"))
    require([jobs[k]["result"] for k in PARENTS] ==
            ["success", "failure", "skipped", "skipped", "failure"], "tampered route")
    require(failures == {"reviewer": "candidate digest", "verify": "candidate digest"},
            "verify must evaluate the tampered evidence after reviewer failure")
    print("PASS rejection: tampered evidence in reviewer and final verify")
    count += 1
    rejects("runtime attempt 2", "only first attempt permitted", identity, REPO, "a"*40, "123", "2", "approve")
    for key, value in (("baseline", "b"*40), ("workflow_sha", "b"*40), ("run_id", "124"),
                       ("run_attempt", "2"), ("repository", "other/repo"), ("scenario", "approve"),
                       ("path", "other.md"), ("version", True)):
        change("reviewer", "candidate identity", "wrong " + key, both, "coder", success(dict(first, **{key: value})))
    change("reviewer", "candidate digest", "wrong digest", both, "coder", success(dict(first, digest="0"*64)))
    changed = candidate(corrected, "1", "Other content.\n", "")
    change("reviewer", "unapproved document", "rehashed altered content", both, "coder", success(changed))
    change("reviewer", "unexpected schema", "extra field", both, "coder", success(dict(first, extra="x")))
    change("reviewer", "unexpected schema", "missing field", both, "coder",
           success({k: v for k, v in first.items() if k != "content"}))
    for label, raw, reason in (("invalid JSON", "{", "invalid JSON"),
                               ("duplicate key", '{"x":1,"x":2}', "duplicate JSON key"),
                               ("nonfinite number", '{"x":NaN}', "invalid JSON constant"),
                               ("oversize", " " * 2049, "evidence size/type"),
                               ("empty evidence", "", "invalid JSON")):
        change("reviewer", reason, label, both, "coder", {"result": "success", "outputs": {"evidence": raw}})
    change("correct", "first verdict mismatch", "unreviewed correction", both, "reviewer",
           success(dict(output(both, "reviewer"), run_id="124")))
    rejects("correction after approval", "correction not authorized", process, "correct", direct,
            {k: one[k] for k in PARENTS["correct"]})
    change("re_review", "candidate lineage", "invalid previous digest", both, "correct",
           success(dict(second, previous="0"*64)))
    change("re_review", "candidate lineage", "third round", both, "correct", success(dict(second, round="3")))
    change("verify", "final verdict mismatch", "stale approval digest", both, "re_review",
           success(dict(last, digest=VECTORS[0][2])))
    change("verify", "final verdict mismatch", "reused approval from first round", both, "re_review", one["reviewer"])
    change("verify", "final verdict mismatch", "second CHANGES_REQUESTED", both, "re_review",
           success(dict(last, decision="CHANGES_REQUESTED")))
    for state in ("skipped", "failure", "cancelled"):
        change("verify", "required job did not succeed", "review job " + state, both, "reviewer",
               dict(both["reviewer"], result=state))
    change("verify", "required job did not succeed", "missing re-review", both, "re_review",
           {"result": "skipped", "outputs": {}})
    change("verify", "unexpected skipped output", "output from skipped job", one, "correct",
           dict(one["correct"], outputs={"evidence": "unexpected"}), direct)
    change("verify", "unexpected job result", "unexpected correction", one, "correct", both["correct"], direct)
    rejects("missing needs", "unexpected schema", process, "verify", direct, {})
    for stage in ("coder", "correct", "re_review"):
        change("verify", "required job did not succeed", stage + " failed despite valid evidence",
               both, stage, dict(both[stage], result="failure"))

    count += cancellation_tests(direct, one)
    print(f"{count} negative cases passed")


def cancellation_tests(ctx, direct_jobs):
    """Synthetic boundaries around the actual final verifier, without a runner or network."""
    from contextlib import redirect_stdout
    from io import StringIO
    from unittest.mock import patch

    jobs, failures = pipeline(ctx, cancelled_before_verify=True)
    needs = {name: jobs[name] for name in PARENTS["verify"]}
    require(needs == {name: direct_jobs[name] for name in needs}, "unexpected upstream change")
    require(not failures and jobs["verify"] == {"result": "skipped", "outputs": {}},
            "pre-start cancellation scheduled verification")
    print("PASS simulated cancellation before verify with valid upstream evidence")

    # Exercise hosted("verify") itself. Only runner identity and git HEAD are fixtures.
    runner = dict(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF=MAIN, GITHUB_WORKFLOW_REF=WORKFLOW,
                  GITHUB_JOB="verify", GITHUB_REPOSITORY=REPO, GITHUB_WORKFLOW_SHA=ctx["workflow_sha"],
                  GITHUB_SHA=ctx["workflow_sha"], GITHUB_RUN_ID=ctx["run_id"],
                  GITHUB_RUN_ATTEMPT=ctx["run_attempt"], PILOT_SCENARIO=ctx["scenario"],
                  PILOT_NEEDS=encode(needs))
    real_evaluate = evaluate

    def interrupt_after_digest(*args):
        # Interrupt inside process("verify"), after the real digest and decision checks.
        verdict = real_evaluate(*args)
        require(verdict == output(needs, "reviewer"), "interrupt at wrong evidence")
        raise KeyboardInterrupt

    with patch.dict(os.environ, runner, clear=True), patch.object(
            subprocess, "check_output", return_value=ctx["workflow_sha"]):
        captured = StringIO()
        with redirect_stdout(captured), patch(__name__ + ".evaluate", side_effect=interrupt_after_digest):
            try:
                hosted("verify")
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("verify swallowed interruption")
        require(captured.getvalue() == "", "interrupted verify emitted a result")
        print("PASS simulated interruption during digest verification")

        # Even a fully completed verify step cannot issue the final GitHub run conclusion.
        with redirect_stdout(captured):
            hosted("verify")
        expected = (f"Synthetic evidence valid: digest={VECTORS[1][2]} "
                    f"run_id={ctx['run_id']} run_attempt={ctx['run_attempt']}\n"
                    "Pilot acceptance requires this GitHub run attempt to finish "
                    "with status=completed and conclusion=success.\n")
        require(captured.getvalue() == expected, "verify must report only scoped evidence")
        print("PASS completed verify reports evidence and defers run acceptance to GitHub")
    return 3


def main(args):
    if args == ["self-test", "positive"]:
        positive_tests()
    elif args == ["self-test", "negative"]:
        negative_tests()
    elif len(args) == 1 and args[0] in PARENTS:
        hosted(args[0])
    else:
        raise Invalid("unknown command")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Invalid as error:
        sys.exit(str(error))

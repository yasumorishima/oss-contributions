"""Check the guards added for the fork switch-off and transient cargo failures.

    python3 scripts/test_guards.py scripts <clone of yasumorishima/gyroflow>
"""
import subprocess
import sys

import yaml

sys.path.insert(0, sys.argv[1])
import auto_resolve as ar  # noqa: E402

repo = sys.argv[2]
fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}")


def raises(f, *args):
    try:
        f(*args)
    except ar.Stop:
        return True
    return False


# The banner as GitHub showed it on yasumorishima/gyroflow (2026-10-17) and on another fork (2026-10-09).
check("banner 2026-10-17", bool(ar.FORK_OFF.search(
    "Workflows are not currently being run on this fork due to the scale of GitHub Actions usage.")))
check("banner other wording", bool(ar.FORK_OFF.search(
    "Workflows aren’t being run on this fork because of its GitHub Actions usage")))
check("no banner on a normal page", not ar.FORK_OFF.search(
    "All workflows Showing runs from all workflows. This workflow has a workflow_dispatch event trigger."))

# The real release.yml (CRLF) at the 10-12 merge and at the 10-17 hand merge.
for rev in ("510196e", "b90f654"):
    text = subprocess.run(["git", "show", f"{rev}:.github/workflows/release.yml"], cwd=repo,
                          capture_output=True, check=True).stdout.decode()
    out = ar.narrow_matrix(text, ["linux", "windows"])
    targets = yaml.safe_load(out)["jobs"]["build"]["strategy"]["matrix"]["targets"]
    check(f"{rev}: matrix is linux+windows", [t["type"] for t in targets] == ["linux", "windows"])
    check(f"{rev}: oses kept", [t["os"] for t in targets] == ["ubuntu-latest", "windows-2022"])
    full = yaml.safe_load(text)
    full["jobs"]["build"]["strategy"]["matrix"]["targets"] = targets
    check(f"{rev}: nothing else changed", yaml.safe_load(out) == full)
    check(f"{rev}: CRLF kept", out.count("\r\n") == out.count("\n") and "\r\n" in out)
    check(f"{rev}: only 5 lines removed", len(text.splitlines()) - len(out.splitlines()) == 5)
    check(f"{rev}: keep a missing type stops", raises(ar.narrow_matrix, text, ["linux", "freebsd"]))
    check(f"{rev}: keeping everything stops", raises(ar.narrow_matrix, text,
          ["linux", "macos", "macstore", "windows", "windows-arm", "ios", "android"]))
    # keeping the last entry: it must stay without a comma; keeping a middle one: the comma goes
    for keep in (["android"], ["macos"], ["linux", "android"]):
        o = ar.narrow_matrix(text, keep)
        ts = yaml.safe_load(o)["jobs"]["build"]["strategy"]["matrix"]["targets"]
        check(f"{rev}: keep {keep}", [t["type"] for t in ts] == keep and ",\r\n        ]" not in o)

# The CI-only commit as a whole: matrix narrowed, github_release switched off, nothing else.
for rev in ("510196e", "b90f654"):
    text = subprocess.run(["git", "show", f"{rev}:.github/workflows/release.yml"], cwd=repo,
                          capture_output=True, check=True).stdout.decode()
    out = ar.disable_jobs(ar.narrow_matrix(text, ["linux", "windows"]), ["github_release"])
    y, full = yaml.safe_load(out), yaml.safe_load(text)
    check(f"{rev}: github_release off", y["jobs"]["github_release"].get("if") is False)
    full["jobs"]["build"]["strategy"]["matrix"]["targets"] = y["jobs"]["build"]["strategy"]["matrix"]["targets"]
    full["jobs"]["github_release"]["if"] = False
    check(f"{rev}: CI-only edit changes nothing else", y == full)
    check(f"{rev}: CRLF kept after disable", out.count("\r\n") == out.count("\n"))
    check(f"{rev}: unknown job stops", raises(ar.disable_jobs, text, ["no_such_job"]))
    check(f"{rev}: job with an if stops", raises(ar.disable_jobs, text, ["winget_release"]))

check("no matrix at all stops", raises(ar.narrow_matrix, "on: push\njobs: {}\n", ["linux"]))

# cargo stderr of 2026-10-16 (transient) and of a real version conflict (not transient).
yanked = ("error: failed to select a version for the requirement `zip = \"^7.2.0\"`\n"
          "candidate versions found which didn't match: 9.0.4, 8.6.0\n"
          "version 7.2.0 is yanked\nversion 7.3.0 is yanked\nversion 7.4.0 is yanked\n")
check("yank is transient", bool(ar.TRANSIENT.search(yanked)))
conflict = ("error: failed to select a version for the requirement `zip = \"^10\"`\n"
            "candidate versions found which didn't match: 9.0.4, 8.6.0, 7.4.0\n")
check("version conflict is not transient", not ar.TRANSIENT.search(conflict))
check("network is transient", bool(ar.TRANSIENT.search("warning: spurious network error (2 tries remaining)")))
check("crates.io 503 is transient", bool(ar.TRANSIENT.search(
    "error: failed to get successful HTTP response from `https://index.crates.io/zi/p/zip` (52.1.2.3), got 503")))
check("index config download is transient", bool(ar.TRANSIENT.search(
    "error: download of config.json failed")))
check("a 404 for a missing crate is not transient", not ar.TRANSIENT.search(
    "error: no matching package named `zipp` found"))
check("workflow push refusal (classic)", bool(ar.WORKFLOW_PUSH_REFUSED.search(
    "! [remote rejected] x -> x (refusing to allow a Personal Access Token to create or update workflow "
    "`.github/workflows/release.yml` without `workflow` scope)")))
check("workflow push refusal (fine-grained)", bool(ar.WORKFLOW_PUSH_REFUSED.search(
    "refusing to allow a Personal Access Token to create or update workflow `.github/workflows/release.yml` "
    "without `workflows` permission")))
check("other push errors are not that", not ar.WORKFLOW_PUSH_REFUSED.search(
    "! [rejected] x -> x (stale info)"))
check("Transient is a Stop", issubclass(ar.Transient, ar.Stop))

print(f"{fails} failed")
sys.exit(1 if fails else 0)

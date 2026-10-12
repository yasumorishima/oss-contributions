"""Bring a long-lived fork PR back to MERGEABLE when upstream moves under it.

Only conflicts of a kind already resolved by hand several times are handled, with the same steps;
anything else is reported in a tracker issue and nothing is pushed. The merge goes to a candidate
branch first and reaches the PR branch only after the fork's own CI has built it.

    python scripts/auto_resolve.py gyroflow                 # scheduled run
    python scripts/auto_resolve.py gyroflow --dry-run --start-sha a998fc6 --upstream-sha bf340d7
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

TRACKER = "yasumorishima/oss-contributions"
AUTHOR = ("yasumorishima", "fw_yasu11@hotmail.com")

PROJECTS = {
    "gyroflow": {
        "upstream": "gyroflow/gyroflow",
        "pr": 1143,
        "fork": "yasumorishima/gyroflow",
        "branch": "feature/ai-optical-flow",
        "upstream_branch": "master",
        # Cargo.lock conflicts were resolved by hand on 2026-06-23, 07-29, 08-28, 09-02, 09-05 and
        # 10-12 the same way: take upstream's lock and let cargo add back this PR's optional deps.
        "lockfiles": {"Cargo.lock": ".", "src/core/Cargo.lock": "src/core"},
        # crypto-common 0.1.7 pins generic-array =0.14.7 and pulls upstream's 0.14.9 down; 0.1.6 does
        # not (09-05, 10-12). Applied only when upstream's lock would otherwise lose a version.
        "pins": [("crypto-common", "0.1.6")],
        "must_contain": [("src/core/Cargo.toml", "use-burn"), ("src/controller.rs", "has_ai_optical_flow")],
        "workflow": "release.yml",
        # macOS/iOS jobs stop at "Install Mac certificates" in the fork (no Apple signing secrets).
        "required_jobs": ["Gyroflow for linux", "Gyroflow for windows", "Gyroflow for windows-arm",
                          "Gyroflow for android"],
        "ci_timeout_min": 110,
    },
}


def sh(cmd: list[str], cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.rstrip()[-3000:], flush=True)
    if r.stderr.strip():
        print(r.stderr.rstrip()[-3000:], flush=True)
    if check and r.returncode != 0:
        raise Stop(f"`{' '.join(cmd)}` exited {r.returncode}")
    return r


def gh_json(path: str) -> dict | list:
    return json.loads(sh(["gh", "api", path]).stdout)


class Stop(Exception):
    """Stop without pushing to the PR branch; the message goes to the tracker issue."""


def pr_state(p: dict) -> dict:
    for _ in range(8):  # GitHub computes mergeable lazily; null means "not yet"
        pr = gh_json(f"repos/{p['upstream']}/pulls/{p['pr']}")
        if pr["mergeable"] is not None or pr["state"] != "open":
            return pr
        time.sleep(20)
    return pr


def lock_set(text: str) -> set[tuple[str, str]]:
    return {(x["name"], x["version"]) for x in tomllib.loads(text).get("package", [])}


def vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.+-]", v))


def lost(p: dict, work: Path, upstream_sha: str) -> dict[str, list]:
    """Upstream (name, version) pairs missing from our lock, per lockfile."""
    out = {}
    for f in p["lockfiles"]:
        up = lock_set(sh(["git", "show", f"{upstream_sha}:{f}"], cwd=work).stdout)
        ours = lock_set((work / f).read_text())
        out[f] = sorted(up - ours)
    return out


def repair(p: dict, work: Path, missing: dict[str, list]) -> None:
    for f, pairs in missing.items():
        d = work / p["lockfiles"][f]
        ours = lock_set((work / f).read_text())
        for name, want in p["pins"]:
            for n, v in sorted(ours):
                if n == name and v != want:
                    sh(["cargo", "update", "-p", f"{n}@{v}", "--precise", want], cwd=d, check=False)
        ours = lock_set((work / f).read_text())
        for name, want in pairs:
            for n, v in sorted(ours):
                if n == name and vkey(v) < vkey(want):
                    sh(["cargo", "update", "-p", f"{n}@{v}", "--precise", want], cwd=d, check=False)


def wait_ci(p: dict, sha: str) -> str:
    deadline = time.time() + p["ci_timeout_min"] * 60
    run = None
    while time.time() < deadline:
        runs = gh_json(f"repos/{p['fork']}/actions/workflows/{p['workflow']}/runs?head_sha={sha}&event=push")
        if runs["workflow_runs"]:
            run = runs["workflow_runs"][0]
            if run["status"] == "completed":
                break
        time.sleep(60)
    else:
        raise Stop(f"fork CI for {sha[:9]} did not finish within {p['ci_timeout_min']} min")
    jobs = gh_json(f"repos/{p['fork']}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    got = {j["name"]: j["conclusion"] for j in jobs}
    bad = {n: got.get(n) for n in p["required_jobs"] if got.get(n) != "success"}
    print(f"fork CI {run['html_url']}: " + ", ".join(f"{n}={got.get(n)}" for n in p["required_jobs"]), flush=True)
    if bad:
        raise Stop(f"fork CI {run['html_url']} did not build the merge: {bad}")
    return run["html_url"]


def report(p: dict, key: str, ok: bool, text: str) -> None:
    title = f"🔀 [自動解消] {p['upstream']}#{p['pr']} の衝突"
    found = gh_json(f"repos/{TRACKER}/issues?state=open&per_page=100")
    issue = next((i for i in found if i["title"] == title and "pull_request" not in i), None)
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{TRACKER}/actions/runs/" \
              f"{os.environ.get('GITHUB_RUN_ID', '?')}"
    if ok:
        if issue:
            sh(["gh", "issue", "close", str(issue["number"]), "--repo", TRACKER,
                "--comment", f"自動で解消しました: {text}\n\n{run_url}"])
        return
    body = (f"https://github.com/{p['upstream']}/pull/{p['pr']} の衝突を自動では解消できませんでした。"
            f"PR のブランチには何も push していません。\n\n**理由**: {text}\n\n{run_url}\n\n"
            f"手順は memory の `oss-{key}.md`。")
    if issue:
        sh(["gh", "issue", "edit", str(issue["number"]), "--repo", TRACKER, "--body", body])
    else:
        sh(["gh", "issue", "create", "--repo", TRACKER, "--title", title, "--body", body])


def resolve(key: str, a) -> str:
    p = PROJECTS[key]
    if not a.dry_run:
        pr = pr_state(p)
        if pr["state"] != "open":
            return f"PR is {pr['state']}; nothing to do"
        if pr["mergeable"] is not False:
            return f"PR mergeable={pr['mergeable']} ({pr['mergeable_state']}); nothing to do"
        start = pr["head"]["sha"]
    else:
        start = a.start_sha
    work = Path("work") / key
    sh(["git", "clone", "-q", "--filter=blob:none", f"https://github.com/{p['fork']}.git", str(work)])
    sh(["git", "remote", "add", "upstream", f"https://github.com/{p['upstream']}.git"], cwd=work)
    sh(["git", "fetch", "-q", "--filter=blob:none", "upstream", p["upstream_branch"]], cwd=work)
    upstream_sha = a.upstream_sha or sh(["git", "rev-parse", f"upstream/{p['upstream_branch']}"], cwd=work).stdout.strip()
    if a.upstream_sha:
        sh(["git", "fetch", "-q", "--filter=blob:none", "upstream", upstream_sha], cwd=work)
    start = sh(["git", "rev-parse", start], cwd=work).stdout.strip()
    sh(["git", "checkout", "-q", "-B", "auto-merge", start], cwd=work)
    for k, v in (("user.name", AUTHOR[0]), ("user.email", AUTHOR[1])):
        sh(["git", "config", k, v], cwd=work)
    files_before = set(sh(["git", "diff", "--name-only", f"{upstream_sha}...{start}"], cwd=work).stdout.split())

    merged = sh(["git", "merge", "--no-ff", "--no-commit", upstream_sha], cwd=work, check=False)
    conflicted = set(sh(["git", "diff", "--name-only", "--diff-filter=U"], cwd=work).stdout.split())
    if merged.returncode != 0 and not conflicted:
        raise Stop("git merge failed without listing conflicted files")
    other = conflicted - set(p["lockfiles"])
    if other:
        raise Stop(f"conflicts outside the lockfiles need a person: {sorted(other)} "
                   f"(lockfiles: {sorted(conflicted & set(p['lockfiles']))})")
    for f in sorted(conflicted):
        sh(["git", "checkout", "--theirs", "--", f], cwd=work)
    for f, d in p["lockfiles"].items():
        sh(["cargo", "metadata", "--format-version", "1", "-q"], cwd=work / d)
    missing = lost(p, work, upstream_sha)
    if any(missing.values()):
        print(f"upstream versions missing before repair: {missing}", flush=True)
        repair(p, work, missing)
        missing = lost(p, work, upstream_sha)
    if any(missing.values()):
        raise Stop(f"our lock drops upstream versions even after the known pins: {missing}")
    for f, d in p["lockfiles"].items():
        sh(["cargo", "metadata", "--format-version", "1", "--locked", "-q"], cwd=work / d)
        sh(["git", "add", "--", f], cwd=work)
    for f in sh(["git", "diff", "--cached", "--name-only"], cwd=work).stdout.split():
        path = work / f
        if path.is_file() and re.search(r"^(<<<<<<<|>>>>>>>) ", path.read_text(errors="replace"), re.M):
            raise Stop(f"conflict marker left in {f}")
    for f, needle in p["must_contain"]:
        if needle not in (work / f).read_text():
            raise Stop(f"{f} no longer contains {needle!r}")
    if sh(["git", "diff", "--name-only", "--diff-filter=U"], cwd=work).stdout.strip():
        raise Stop("unmerged paths remain")
    sh(["git", "commit", "-q", "-m",
        f"Merge remote-tracking branch 'upstream/{p['upstream_branch']}' into {p['branch']}"], cwd=work)
    head = sh(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
    files_after = set(sh(["git", "diff", "--name-only", f"{upstream_sha}...{head}"], cwd=work).stdout.split())
    if files_after != files_before:
        raise Stop(f"the PR's file set changed: added {sorted(files_after - files_before)}, "
                   f"dropped {sorted(files_before - files_after)}")
    if a.expect_tree_of:
        sh(["git", "fetch", "-q", "--filter=blob:none", "origin", a.expect_tree_of], cwd=work)
        d = sh(["git", "diff", "--stat", a.expect_tree_of, head], cwd=work).stdout.strip()
        print(f"tree vs {a.expect_tree_of}: {'identical' if not d else d}", flush=True)
        if d:
            raise Stop(f"result differs from {a.expect_tree_of}")

    candidate = f"auto-merge/{p['branch'].split('/')[-1]}"
    sh(["git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{candidate}"], cwd=work)
    try:
        ci = wait_ci(p, head)
    finally:
        sh(["git", "push", "-q", "origin", "--delete", candidate], cwd=work, check=False)
    if a.dry_run:
        return f"dry run: {head[:9]} built ({ci}); PR branch untouched"
    # a plain push only fast-forwards: if the branch moved since `start`, it is rejected
    sh(["git", "push", "-q", "origin", f"HEAD:refs/heads/{p['branch']}"], cwd=work)
    return f"{start[:9]} -> {head[:9]} (upstream {upstream_sha[:9]}), fork CI {ci}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", choices=sorted(PROJECTS))
    ap.add_argument("--dry-run", action="store_true", help="never push to the PR branch or touch the issue")
    ap.add_argument("--start-sha", help="dry run: the PR head to start from")
    ap.add_argument("--upstream-sha", help="dry run: the upstream commit to merge")
    ap.add_argument("--expect-tree-of", help="dry run: fail unless the merge result has this commit's tree")
    a = ap.parse_args()
    if a.dry_run and not a.start_sha:
        ap.error("--dry-run needs --start-sha")
    p = PROJECTS[a.project]
    try:
        msg = resolve(a.project, a)
    except Stop as e:
        print(f"::error::{e}", flush=True)
        if not a.dry_run:
            report(p, a.project, False, str(e))
        return 1
    print(msg, flush=True)
    if not a.dry_run and ("->" in msg or "mergeable=True" in msg):  # also closes an issue fixed by hand
        report(p, a.project, True, msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

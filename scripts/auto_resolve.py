"""Bring a long-lived fork PR back to MERGEABLE when upstream moves under it.

Only conflicts of a kind already resolved by hand several times are handled, with the same steps;
anything else is reported in a tracker issue and nothing is pushed. The merge goes to a candidate
branch first and reaches the PR branch only after the fork's own CI has built it.

    python scripts/auto_resolve.py gyroflow                 # scheduled run
    python scripts/auto_resolve.py gyroflow --dry-run --start-sha <full sha> --upstream-sha <full sha>
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
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
        # GitHub switched the fork's workflows off on 2026-10-15 "due to the scale of GitHub Actions
        # usage": a Release run is ~110 runner-min (linux 27, windows 27, windows-arm 34, android 23;
        # the macOS/iOS jobs stop at once at "Install Mac certificates"), and every conflict cost two
        # runs (candidate + PR branch). The candidate now gets one extra commit that keeps only these
        # matrix entries, the PR branch gets the merge without it, and the PR branch's own run is
        # cancelled. For a lockfile-only merge windows-arm/android build the same crate graph on the
        # same runner image as windows.
        "candidate_matrix": ["linux", "windows"],
        # `github_release` needs only `build` and runs on macOS; it was skipped only because the macOS
        # builds failed, so with a linux+windows matrix it would start. The CI-only commit switches it off.
        "candidate_disable_jobs": ["github_release"],
        "cancel_pr_branch_run": True,
        "required_jobs": ["Gyroflow for linux", "Gyroflow for windows"],
        "ci_timeout_min": 70,  # runs take 20-35 min; a re-run gets one more hour
        "memo": "oss-gyroflow.md",
    },
    "d2": {
        "upstream": "d2lang/d2",
        "pr": 2742,
        "fork": "yasumorishima/d2",
        "branch": "feat/cycle-border-tracing",
        "upstream_branch": "master",
        # Resolved by hand on 2026-07-29 (txtar.txt), 09-08 and 10-12 (next.md): upstream's file plus
        # the lines this PR added. "section": back under the same changelog heading; "end": at the end.
        # Code conflicts (08-07 d2layouts.go, 09-06 d2svg.go) stop here.
        "append_files": {"ci/release/changelogs/next.md": "section", "e2etests/txtar.txt": "end"},
        "must_contain": [("d2layouts/d2cycle/layout.go", "func clipToShapeBorder"),
                         ("e2etests/txtar.txt", "-- cycle-diagram --")],
        # CONTRIBUTING: "Please sign your commits"; the `signed` job fails on any unsigned commit.
        "sign": True,
        "workflow": "ci.yml",
        "dispatch": True,  # ci.yml runs on push to master only; it is dispatched on the candidate
        "required_jobs": ["ci", "signed"],
        "ci_timeout_min": 45,  # runs take 7-11 min
        "memo": "oss-d2-1578.md",
    },
}


def sh(cmd: list[str], cwd: str | Path | None = None, check: bool = True,
       env: dict | None = None) -> subprocess.CompletedProcess:
    print("+ " + " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, **env) if env else None)
    if r.stdout.strip():
        print(r.stdout.rstrip()[-3000:], flush=True)
    if r.stderr.strip():
        print(r.stderr.rstrip()[-3000:], flush=True)
    if check and r.returncode != 0:
        raise Stop(f"`{' '.join(cmd)}` exited {r.returncode}")
    return r


def gh_json(path: str, tries: int = 6, env: dict | None = None) -> dict | list:
    """GitHub's API answers 502/504 now and then (a 502 ended the first dry run); retry those."""
    for i in range(tries):
        r = sh(["gh", "api", path], check=False, env=env)
        if r.returncode == 0:
            return json.loads(r.stdout)
        if i == tries - 1 or not re.search(r"HTTP 5\d\d|timeout|connection", r.stderr, re.I):
            raise Stop(f"`gh api {path}` failed: {r.stderr.strip()[-300:]}")
        time.sleep(30 * (i + 1))


class Stop(Exception):
    """Stop without pushing to the PR branch; the message goes to the tracker issue."""


class Transient(Stop):
    """Worth retrying on the next scheduled run (a crates.io yank, the network): the tracker issue is
    opened only when the same kind of stop repeats TRANSIENT_LIMIT runs in a row. On 2026-10-16
    zip 7.2.0-7.4.0 were yanked and un-yanked within a day."""


TRANSIENT_TAG = "TRANSIENT:"
NO_RUN_MIN = 15  # a push starts the fork's run within a minute or two
TRANSIENT_LIMIT = 3
TRANSIENT = re.compile(r"version \S+ is yanked|spurious network error|failed to download|"
                       r"failed to get successful HTTP response|got (?:5\d\d|429)\b|download of \S+ failed|"
                       r"Couldn't resolve host|operation timed out", re.I)
WORKFLOW_PUSH_REFUSED = re.compile(r"refusing to allow .{0,80}workflow", re.I)
# The banner GitHub shows on a fork whose workflows it switched off; the API still says `active`.
FORK_OFF = re.compile(r"(?:are not|aren['\u2019]t)(?: currently)? being run on this fork", re.I)
MATRIX_ENTRY = re.compile(r"^(\s*)(\{\s*os:\s*[^,{}]+,\s*type:\s*([\w-]+)\s*\})\s*,?\s*$")


def fork_actions_off(fork: str) -> bool:
    """True when the fork's Actions page carries the banner. An unreadable page counts as on: the
    wait for the fork's run then times out, and the page is read again at that point."""
    try:
        with urllib.request.urlopen(f"https://github.com/{fork}/actions", timeout=30) as r:
            return bool(FORK_OFF.search(r.read().decode("utf-8", "replace")))
    except OSError as e:
        print(f"::warning::could not read the Actions page of {fork}: {e}", flush=True)
        return False


def fork_off_stop(fork: str) -> Stop:
    return Stop(f"GitHub has switched off the workflows of {fork} (the Actions page says they are not "
                f"being run on this fork due to the scale of Actions usage). The owner re-enables them "
                f"with the button on https://github.com/{fork}/actions")


def narrow_matrix(text: str, keep: list[str]) -> str:
    """Keep only the matrix entries `{ os: ..., type: <keep> }` (one per line, as in gyroflow's
    release.yml), with a comma after every kept entry but the last; line endings are kept."""
    lines = text.splitlines(keepends=True)
    entries = [(i, MATRIX_ENTRY.match(ln.rstrip("\r\n"))) for i, ln in enumerate(lines)]
    entries = [(i, m) for i, m in entries if m]
    seen = [m.group(3) for _, m in entries]
    kept = [(i, m) for i, m in entries if m.group(3) in keep]
    if sorted(m.group(3) for _, m in kept) != sorted(keep) or len(seen) <= len(kept):
        raise Stop(f"the CI matrix is not the one expected: has {seen}, wanted to keep {keep}")
    if entries[-1][0] - entries[0][0] != len(entries) - 1:
        raise Stop("the CI matrix entries are not on consecutive lines")
    drop = {i for i, m in entries if m.group(3) not in keep}
    last = kept[-1][0]
    for i, m in kept:
        eol = lines[i][len(lines[i].rstrip("\r\n")):]
        lines[i] = m.group(1) + m.group(2) + ("" if i == last else ",") + eol
    return "".join(ln for i, ln in enumerate(lines) if i not in drop)


def disable_jobs(text: str, jobs: list[str]) -> str:
    """Add `if: false` under each named top-level job (two-space indent, as in gyroflow's release.yml)."""
    lines = text.splitlines(keepends=True)
    for job in jobs:
        at = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == f"  {job}:"]
        if len(at) != 1:
            raise Stop(f"job {job!r} found {len(at)} times in the workflow")
        i = at[0]
        end = next((k for k in range(i + 1, len(lines)) if re.match(r"^ {0,2}\S", lines[k])), len(lines))
        if any(re.match(r"^    if:", ln) for ln in lines[i + 1:end]):
            raise Stop(f"job {job!r} already has an `if:`")
        eol = lines[i][len(lines[i].rstrip("\r\n")):] or "\n"
        lines.insert(i + 1, "    if: false" + eol)
    return "".join(lines)


def cargo_metadata(d: Path, locked: bool = False) -> None:
    cmd = ["cargo", "metadata", "--format-version", "1"] + (["--locked"] if locked else []) + ["-q"]
    r = sh(cmd, cwd=d, check=False)
    if r.returncode != 0:
        m = TRANSIENT.search(r.stderr)
        if m:
            raise Transient(f"`{' '.join(cmd)}` in {d} failed on crates.io or the network ({m.group(0)})")
        raise Stop(f"`{' '.join(cmd)}` in {d} exited {r.returncode}")


def cancel_runs(p: dict, query: str) -> int:
    n = 0
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        runs = gh_json(f"repos/{p['fork']}/actions/workflows/{p['workflow']}/runs?{query}&status={status}")
        for r in runs["workflow_runs"]:
            r = sh(["gh", "api", "-X", "POST", f"repos/{p['fork']}/actions/runs/{r['id']}/cancel"], check=False)
            n += r.returncode == 0
    return n


def cancel_pr_branch_run(p: dict, sha: str) -> None:
    """The push to the PR branch starts a full Release run of a tree the candidate already built;
    cancel it (it appears within a minute or two). Not finding it is not an error."""
    for _ in range(10):
        time.sleep(30)
        try:
            if cancel_runs(p, f"head_sha={sha}&event=push"):
                print(f"cancelled the fork's run on the PR branch ({sha[:9]})", flush=True)
                return
        except Stop as e:
            print(f"::warning::could not cancel the PR branch run: {e}", flush=True)
            return
    print("::warning::no fork run appeared for the PR branch push within 5 min", flush=True)


def prior_transient_runs(project: str) -> int:
    """How many of this workflow's previous runs, newest first, ended this project's job with a
    TRANSIENT warning, up to the first one that did not. Read with the workflow's own token."""
    me, tok = os.environ.get("GITHUB_RUN_ID"), os.environ.get("TRACKER_TOKEN")
    if not me or not tok:
        return 0
    env = {"GH_TOKEN": tok}
    n = 0
    try:
        runs = gh_json(f"repos/{TRACKER}/actions/workflows/auto-resolve-conflicts.yml/runs"
                       f"?status=completed&per_page=20", env=env)["workflow_runs"]
        for r in runs:
            if str(r["id"]) == me or r["event"] != "schedule":  # dry runs are dispatched
                continue
            jobs = gh_json(f"repos/{TRACKER}/actions/runs/{r['id']}/jobs", env=env)["jobs"]
            job = next((j for j in jobs if j["name"] == project), None)
            if job is None or job["conclusion"] in ("skipped", "cancelled"):
                continue  # a run for the other project only, or one that never got to say anything
            anns = gh_json(f"repos/{TRACKER}/check-runs/{job['id']}/annotations", env=env)
            if not any(x.get("message", "").startswith(TRANSIENT_TAG) for x in anns):
                break
            n += 1
    except Exception as e:  # noqa: BLE001  counting must not hide the stop itself
        print(f"::warning::could not count earlier transient stops: {e}", flush=True)
    return n


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
    """SemVer order: build metadata ignored, a prerelease sorts below its release."""
    core, _, pre = v.split("+")[0].partition("-")
    rel = tuple(int(x) for x in core.split("."))
    if not pre:
        return rel, 1, ()
    return rel, 0, tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split("."))


def names(out: str) -> set[str]:
    return {x for x in out.split("\0") if x}



PKG = "\n[[package]]\n"
BARE_DEP = re.compile(r'^ "([A-Za-z0-9_-]+)",$', re.M)


def semver_range(v: str) -> tuple[int, int]:
    """Cargo lets two versions of a crate coexist only if they are in different semver ranges."""
    maj, mnr, pat = (int(x) for x in v.split("+")[0].split("-")[0].split("."))
    return (0, maj) if maj else (1, mnr) if mnr else (2, pat)


def lock_blocks(text: str, what: str) -> tuple[str, list[tuple[str, str, str]]]:
    head, *blocks = text.split(PKG)
    out = []
    for b in blocks:
        b = b.rstrip("\n") + "\n"
        if re.search(r"^\[", b, re.M):
            raise Stop(f"{what} has a table other than [[package]]; carry_pr_only does not handle it")
        x = tomllib.loads("[[package]]\n" + b)["package"][0]
        out.append((x["name"], x["version"], b))
    return head, out


def carry_pr_only(path: Path, ours_text: str) -> list[str]:
    """Keep this PR's own crates at the versions it already builds with.

    Taking upstream's lock and letting cargo add the PR's crates back re-resolved them from
    crates.io on every conflict. On 10-16 that failed: candle-core needs zip ^7.2, upstream locks
    zip 8.6, and every zip 7.x was yanked (a version already in a lock stays usable). Carried are
    the PR lock's entries whose name upstream's lock lacks, or whose version is in no semver range
    upstream locks for that name; same-range versions stay upstream's, and `lost()` still checks
    that none of upstream's versions is dropped. A bare dependency name that now has two versions
    is written as "name version", as cargo itself writes it. Entries nothing uses are pruned by the
    unlocked `cargo metadata` that follows. Cargo ignores a reference it cannot match, so a stale
    one costs a re-resolve of that edge, not an error (src/resolver/encode.rs, lookup_id)."""
    head, theirs = lock_blocks(path.read_text(), f"upstream {path.name}")
    _, ours = lock_blocks(ours_text, f"our {path.name}")
    t_ver: dict[str, set[str]] = {}
    for n, v, _ in theirs:
        t_ver.setdefault(n, set()).add(v)
    o_ver: dict[str, set[str]] = {}
    for n, v, _ in ours:
        o_ver.setdefault(n, set()).add(v)
    carried = [(n, v, b) for n, v, b in ours
               if v not in t_ver.get(n, ())
               and semver_range(v) not in {semver_range(x) for x in t_ver.get(n, ())}]
    if not carried:
        return []
    merged = {n: set(vs) for n, vs in t_ver.items()}
    for n, v, _ in carried:
        merged.setdefault(n, set()).add(v)

    def pin(b: str, own: dict[str, set[str]]) -> str:
        def one(m: re.Match) -> str:
            n = m.group(1)
            if len(merged.get(n, ())) > 1 and len(own.get(n, ())) == 1:
                return f' "{n} {next(iter(own[n]))}",'
            return m.group(0)
        return BARE_DEP.sub(one, b)

    blocks = [pin(b, t_ver) for _, _, b in theirs] + [pin(b, o_ver) for _, _, b in carried]
    path.write_text(head + PKG + "\n[[package]]\n".join(blocks))
    if len(lock_set(path.read_text())) != len(theirs) + len(carried):
        raise Stop(f"carrying the PR's entries into {path.name} lost or duplicated a package")
    return [f"{n} {v}" for n, v, _ in carried]


def lost(p: dict, work: Path, upstream_sha: str) -> dict[str, list]:
    """Upstream (name, version) pairs missing from our lock, per lockfile."""
    out = {}
    for f in p.get("lockfiles", {}):
        up = lock_set(sh(["git", "show", f"{upstream_sha}:{f}"], cwd=work).stdout)
        ours = lock_set((work / f).read_text())
        out[f] = sorted(up - ours)
    return out


def repair(p: dict, work: Path, missing: dict[str, list]) -> None:
    for f, pairs in missing.items():
        if not pairs:
            continue
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


def split(text: str) -> list[str]:
    return text.split("\n")


def strip_blank(lines: list[str]) -> list[str]:
    i, j = 0, len(lines)
    while i < j and not lines[i].strip():
        i += 1
    while j > i and not lines[j - 1].strip():
        j -= 1
    return lines[i:j]


def added_blocks(f: str, base: list[str], ours: list[str]) -> list[tuple[int, list[str]]]:
    """The lines this PR added to f, as (position in ours, lines); any other change stops."""
    out = []
    for op, i1, _, j1, j2 in difflib.SequenceMatcher(a=base, b=ours, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op != "insert":
            raise Stop(f"{f}: this PR {op}s existing lines near line {i1 + 1}; only added lines are re-applied")
        block = strip_blank(ours[j1:j2])
        if block:
            out.append((j1, block))
    return out


def reapply(f: str, mode: str, base: str, ours: str, theirs: str) -> str:
    """Upstream's file with this PR's added lines put back, the way the hand merges did it."""
    if any("\r" in x for x in (base, ours, theirs)):
        raise Stop(f"{f}: CRLF line endings; not re-applied")
    o, t = split(ours), split(theirs)
    blocks = added_blocks(f, split(base), o)
    if not blocks:
        raise Stop(f"{f}: conflicted but this PR adds nothing to it")
    for j, block in blocks:
        if any(t[k:k + len(block)] == block for k in range(len(t))):
            raise Stop(f"{f}: upstream already has {block[0]!r}")
        if mode == "section":
            heading = next((o[k] for k in range(j - 1, -1, -1) if o[k].startswith("#")), None)
            if heading is None or heading not in t:
                raise Stop(f"{f}: section {heading!r} of {block[0]!r} is not in upstream's file")
            if o.count(heading) != 1 or t.count(heading) != 1:
                raise Stop(f"{f}: heading {heading!r} is not unique")
            h = t.index(heading)
            n = next((k for k in range(h + 1, len(t)) if t[k].startswith("#") or t[k].strip() == "---"), len(t))
            last = max((k for k in range(h + 1, n) if t[k].strip()), default=h)
            if last == h:  # empty section: heading, blank, items, blank, next heading
                t = t[:h + 1] + [""] + block + [""] + t[n:]
                at = h + 2
            else:
                t = t[:last + 1] + block + t[last + 1:]
                at = last + 1
            # the block is the section's last item: no heading before it, only blank lines after it
            end = at + len(block)
            nxt = next((k for k in range(end, len(t)) if t[k].startswith("#") or t[k].strip() == "---"), len(t))
            if (t[at:end] != block or any(x.startswith("#") or x.strip() == "---" for x in t[h + 1:at])
                    or any(x.strip() for x in t[end:nxt])):
                raise Stop(f"{f}: {block[0]!r} did not land at the end of {heading!r}")
        elif mode == "end":
            names_ = [x for x in block if re.match(r"^-- .+ --$", x)]
            clash = [x for x in names_ if x in t]
            if clash:
                raise Stop(f"{f}: upstream already has a case named {clash}")
            tail = t[len(t) - 1:] if t and t[-1] == "" else []
            t = strip_blank(t) + [""] + block + tail
        else:
            raise Stop(f"{f}: unknown mode {mode!r}")
    # the result differs from upstream's file by exactly the re-added lines
    added = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=split(theirs), b=t, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op != "insert":
            raise Stop(f"{f}: re-applying changed upstream's lines near line {i1 + 1}")
        added += [x for x in t[j1:j2] if x.strip()]
    want = [x for _, b in blocks for x in b if x.strip()]
    if sorted(added) != sorted(want):
        raise Stop(f"{f}: re-applied lines do not match this PR's additions")
    return "\n".join(t)


def setup_signing(work: Path) -> None:
    """Commits are signed with the key in SIGNING_KEY_FILE (an SSH key registered for signing)."""
    key = os.environ.get("SIGNING_KEY_FILE", "")
    if not key or not Path(key).is_file():
        raise Stop("this project needs signed commits, but SIGNING_KEY_FILE is not set")
    pub = sh(["ssh-keygen", "-y", "-f", key]).stdout.strip()
    allowed = Path(key).with_name(Path(key).name + ".allowed_signers")
    allowed.write_text(f'{AUTHOR[1]} namespaces="git" {pub}\n')
    for k, v in (("gpg.format", "ssh"), ("user.signingkey", key), ("commit.gpgsign", "true"),
                 ("gpg.ssh.allowedSignersFile", str(allowed))):
        sh(["git", "config", k, v], cwd=work)


def verify_signature(p: dict, sha: str) -> None:
    """GitHub must verify the signature against the account's keys (%G? only shows it is signed).
    Re-read a few times when the commit is not visible yet or the verification service failed."""
    for i in range(4):
        r = sh(["gh", "api", f"repos/{p['fork']}/commits/{sha}"], check=False)
        if r.returncode == 0:
            v = json.loads(r.stdout)["commit"]["verification"]
            if v.get("verified"):
                return
            reason = v.get("reason")
            if reason not in ("gpgverify_unavailable", "gpgverify_error"):
                raise Stop(f"GitHub does not verify the merge signature ({reason}); "
                           "is the signing key still registered on the account?")
        elif not re.search(r"HTTP (404|422|5\d\d)|timeout|connection", r.stderr, re.I):
            raise Stop(f"reading commit {sha[:9]} failed: {r.stderr.strip()[-300:]}")
        time.sleep(20 * (i + 1))
    raise Stop(f"GitHub could not verify the signature of {sha[:9]} after 4 reads")


def wait_run(p: dict, sha: str, run_id: int | None, deadline: float, min_attempt: int = 1) -> dict:
    """Wait for the run to complete; after a re-run, only an attempt >= min_attempt counts, so the
    previous attempt's `completed` cannot be read as the result."""
    t0 = time.time()
    while time.time() < deadline:
        if run_id is None:
            event = "workflow_dispatch" if p.get("dispatch") else "push"
            runs = gh_json(f"repos/{p['fork']}/actions/workflows/{p['workflow']}/runs?head_sha={sha}&event={event}")
            run = runs["workflow_runs"][0] if runs["workflow_runs"] else None
            if run:
                # Keep the id: without it, a run still going at NO_RUN_MIN read as "never appeared"
                # (10-17 and 10-18, when the two-job candidate run took 15-16 min).
                run_id = run["id"]
            elif time.time() - t0 > NO_RUN_MIN * 60:
                if fork_actions_off(p["fork"]):
                    raise fork_off_stop(p["fork"])
                raise Stop(f"no fork run appeared for {sha[:9]} within {NO_RUN_MIN} min; if "
                           f"https://github.com/{p['fork']}/actions says workflows are not being run on this "
                           f"fork, the owner re-enables them there")
        else:
            run = gh_json(f"repos/{p['fork']}/actions/runs/{run_id}")
        if run and run["status"] == "completed" and run.get("run_attempt", 1) >= min_attempt:
            return run
        time.sleep(60)
    if run_id is not None:
        # Deleting the candidate branch does not stop its run; an abandoned run still spends the
        # fork's Actions minutes (GitHub switched the fork off for usage on 10-15).
        sh(["gh", "api", "-X", "POST", f"repos/{p['fork']}/actions/runs/{run_id}/cancel"], check=False)
    if fork_actions_off(p["fork"]):
        raise fork_off_stop(p["fork"])
    raise Stop(f"fork CI for {sha[:9]} (attempt {min_attempt}) did not finish in time")


def wait_ci(p: dict, sha: str, candidate: str) -> str:
    """The required jobs must succeed. Failed jobs are re-run once: on 2026-10-12 the linux job of
    an identical tree failed only because the AppImage runtime download answered 504."""
    deadline = time.time() + p["ci_timeout_min"] * 60
    if p.get("dispatch"):
        sh(["gh", "api", "-X", "POST", f"repos/{p['fork']}/actions/workflows/{p['workflow']}/dispatches",
            "-f", f"ref={candidate}"])
    run = wait_run(p, sha, None, deadline)
    for attempt in (1, 2):
        jobs = gh_json(f"repos/{p['fork']}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        got = {j["name"]: j["conclusion"] for j in jobs}
        bad = {n: got.get(n) for n in p["required_jobs"] if got.get(n) != "success"}
        print(f"fork CI {run['html_url']} attempt {attempt}: "
              + ", ".join(f"{n}={got.get(n)}" for n in p["required_jobs"]), flush=True)
        if not bad:
            return run["html_url"]
        if attempt == 2:
            break
        sh(["gh", "api", "-X", "POST", f"repos/{p['fork']}/actions/runs/{run['id']}/rerun-failed-jobs"])
        time.sleep(30)
        run = wait_run(p, sha, run["id"], deadline + 60 * 60, run.get("run_attempt", 1) + 1)
    raise Stop(f"fork CI {run['html_url']} did not build the merge (after one re-run): {bad}")


def report(p: dict, key: str, ok: bool, text: str) -> None:
    title = f"🔀 [自動解消] {p['upstream']}#{p['pr']} の衝突"
    found = json.loads(sh(["gh", "issue", "list", "--repo", TRACKER, "--state", "open", "--limit", "1000",
                           "--json", "number,title"]).stdout)
    issue = next((i for i in found if i["title"] == title), None)
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{TRACKER}/actions/runs/" \
              f"{os.environ.get('GITHUB_RUN_ID', '?')}"
    if ok:
        if issue:
            sh(["gh", "issue", "close", str(issue["number"]), "--repo", TRACKER,
                "--comment", f"自動で解消しました: {text}\n\n{run_url}"])
        return
    body = (f"https://github.com/{p['upstream']}/pull/{p['pr']} の衝突を自動では解消できませんでした。"
            f"PR のブランチには何も push していません。\n\n**理由**: {text}\n\n{run_url}\n\n"
            f"手順は memory の `{p['memo']}`。")
    if issue:
        old = json.loads(sh(["gh", "issue", "view", str(issue["number"]), "--repo", TRACKER,
                             "--json", "body"]).stdout)["body"]
        if f"**理由**: {text}\n" in old:
            print("the tracker issue already gives this reason; not edited", flush=True)
            return
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
        if (pr["head"]["repo"] or {}).get("full_name") != p["fork"] or pr["head"]["ref"] != p["branch"]:
            raise Stop(f"PR head is {pr['head']['label']}, not {p['fork']}:{p['branch']}")
        start = pr["head"]["sha"]
    else:
        start = a.start_sha
    if fork_actions_off(p["fork"]):  # before any work: nothing would build the candidate
        raise fork_off_stop(p["fork"])
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
    if p.get("sign"):
        setup_signing(work)
    base = sh(["git", "merge-base", start, upstream_sha], cwd=work).stdout.strip()
    files_before = names(sh(["git", "diff", "-z", "--name-only", f"{upstream_sha}...{start}"], cwd=work).stdout)

    merged = sh(["git", "merge", "--no-ff", "--no-commit", upstream_sha], cwd=work, check=False)
    conflicted = names(sh(["git", "diff", "-z", "--name-only", "--diff-filter=U"], cwd=work).stdout)
    if merged.returncode != 0 and not conflicted:
        raise Stop("git merge failed without listing conflicted files")
    lockfiles, appends = p.get("lockfiles", {}), p.get("append_files", {})
    other = conflicted - set(lockfiles) - set(appends)
    if other:
        raise Stop(f"conflicts of a kind not resolved here need a person: {sorted(other)} "
                   f"(also conflicted: {sorted(conflicted - other)})")
    for f in sorted(conflicted & set(appends)):
        show = lambda rev: sh(["git", "show", f"{rev}:{f}"], cwd=work).stdout  # noqa: E731
        (work / f).write_text(reapply(f, appends[f], show(base), show(start), show(upstream_sha)))
        sh(["git", "add", "--", f], cwd=work)
    for f in sorted(conflicted & set(lockfiles)):
        sh(["git", "checkout", "--theirs", "--", f], cwd=work)
        kept = carry_pr_only(work / f, sh(["git", "show", f"{start}:{f}"], cwd=work).stdout)
        print(f"{f}: carried {len(kept)} entries from the PR's lock", flush=True)
    for f, d in lockfiles.items():
        cargo_metadata(work / d)
    missing = lost(p, work, upstream_sha)
    if any(missing.values()):
        print(f"upstream versions missing before repair: {missing}", flush=True)
        repair(p, work, missing)
        missing = lost(p, work, upstream_sha)
    if any(missing.values()):
        raise Stop(f"our lock drops upstream versions even after the known pins: {missing}")
    for f, d in lockfiles.items():
        cargo_metadata(work / d, locked=True)
        sh(["git", "add", "--", f], cwd=work)
    for f in names(sh(["git", "diff", "-z", "--cached", "--name-only"], cwd=work).stdout):
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
    if p.get("sign"):
        status = sh(["git", "log", "-1", "--format=%G?", head], cwd=work).stdout.strip()
        if status != "G":  # only says the commit is signed with SIGNING_KEY_FILE; GitHub checks the account
            raise Stop(f"the merge commit is not signed (%G? = {status!r})")
    files_after = names(sh(["git", "diff", "-z", "--name-only", f"{upstream_sha}...{head}"], cwd=work).stdout)
    if files_after != files_before:
        raise Stop(f"the PR's file set changed: added {sorted(files_after - files_before)}, "
                   f"dropped {sorted(files_before - files_after)}")
    if a.expect_tree_of:
        sh(["git", "fetch", "-q", "--filter=blob:none", "origin", a.expect_tree_of], cwd=work)
        d = sh(["git", "diff", "--stat", a.expect_tree_of, head], cwd=work).stdout.strip()
        print(f"tree vs {a.expect_tree_of}: {'identical' if not d else d}", flush=True)
        if d:
            raise Stop(f"result differs from {a.expect_tree_of}")

    ci_sha = head
    if p.get("candidate_matrix"):
        wf = work / ".github" / "workflows" / p["workflow"]
        text = narrow_matrix(wf.read_bytes().decode(), p["candidate_matrix"])
        wf.write_bytes(disable_jobs(text, p.get("candidate_disable_jobs", [])).encode())
        sh(["git", "commit", "-q", "-am", "CI only: build " + " and ".join(p["candidate_matrix"])
            + " (this commit never reaches the PR branch)"], cwd=work)
        ci_sha = sh(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
        if sh(["git", "rev-parse", f"{ci_sha}^"], cwd=work).stdout.strip() != head:
            raise Stop("the CI-only commit is not a child of the merge")
        touched = names(sh(["git", "diff", "-z", "--name-only", head, ci_sha], cwd=work).stdout)
        if touched != {f".github/workflows/{p['workflow']}"}:
            raise Stop(f"the CI-only commit touches {sorted(touched)}")
    candidate = f"auto-merge/{p['branch'].split('/')[-1]}"
    cancel_runs(p, "branch=" + urllib.parse.quote(candidate, safe=""))  # an older candidate's run
    r = sh(["git", "push", "-q", "-f", "origin", f"{ci_sha}:refs/heads/{candidate}"], cwd=work, check=False)
    if r.returncode != 0:
        if WORKFLOW_PUSH_REFUSED.search(r.stderr):
            raise Stop("GitHub refused the CI-only commit because FORK_ADMIN_PAT may not change workflow "
                       "files: give the token the `workflow` scope (classic) or Workflows: Read and write "
                       "(fine-grained) for yasumorishima/gyroflow, then re-run")
        raise Stop(f"pushing the candidate exited {r.returncode}")
    try:
        if p.get("sign"):
            verify_signature(p, head)
        ci = wait_ci(p, ci_sha, candidate)
    finally:
        sh(["git", "push", "-q", "origin", "--delete", candidate], cwd=work, check=False)
    if a.dry_run:
        return f"dry run: {head[:9]} built ({ci}); PR branch untouched"
    # compare-and-swap: rejected unless the branch still points at `start` (HEAD descends from it)
    sh(["git", "push", "-q", f"--force-with-lease=refs/heads/{p['branch']}:{start}", "origin",
        f"{head}:refs/heads/{p['branch']}"], cwd=work)
    if p.get("cancel_pr_branch_run"):
        cancel_pr_branch_run(p, head)
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
    except Transient as e:
        n = 1 + (0 if a.dry_run else prior_transient_runs(a.project))
        if n < TRANSIENT_LIMIT:
            print(f"::warning::{TRANSIENT_TAG} {e} ({n} run(s) in a row; the tracker issue is opened "
                  f"at {TRANSIENT_LIMIT})", flush=True)
            return 0
        msg = f"{TRANSIENT_TAG} {e} (the same kind of stop {TRANSIENT_LIMIT} or more scheduled runs in a row)"
        print(f"::error::{msg}", flush=True)
        if not a.dry_run:
            report(p, a.project, False, msg)
        return 1
    except Exception as e:  # noqa: BLE001  any failure: nothing reached the PR branch; say so
        msg = str(e) if isinstance(e, Stop) else f"unexpected {type(e).__name__}: {e}"
        print(f"::error::{msg}", flush=True)
        if not a.dry_run:
            report(p, a.project, False, msg)
        return 1
    print(msg, flush=True)
    if not a.dry_run and ("->" in msg or "mergeable=True" in msg):  # also closes an issue fixed by hand
        report(p, a.project, True, msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

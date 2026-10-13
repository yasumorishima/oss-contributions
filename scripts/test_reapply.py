"""Replay the hand merges of d2#2742 through reapply() and check the stop cases.

    python3 scripts/test_reapply.py scripts <clone of yasumorishima/d2 with upstream fetched>
"""
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
import auto_resolve as ar  # noqa: E402

repo = sys.argv[2]


def show(rev, f):
    return subprocess.run(["git", "show", f"{rev}:{f}"], cwd=repo, capture_output=True, text=True, check=True).stdout


fails = 0
for merge, f, mode in [("021f780ac", "ci/release/changelogs/next.md", "section"),
                       ("39ba1f5c9", "ci/release/changelogs/next.md", "section"),
                       ("de6ccd691", "e2etests/txtar.txt", "end")]:
    ours, theirs = subprocess.run(["git", "log", "-1", "--format=%P", merge], cwd=repo, capture_output=True,
                                  text=True, check=True).stdout.split()
    base = subprocess.run(["git", "merge-base", ours, theirs], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()
    got = ar.reapply(f, mode, show(base, f), show(ours, f), show(theirs, f))
    want = show(merge, f)
    ok = got == want
    fails += not ok
    print(f"{merge} {f}: {'identical to the hand merge' if ok else 'DIFFERS'}")
    if not ok:
        import difflib
        print("".join(difflib.unified_diff(want.splitlines(True), got.splitlines(True), "hand", "auto"))[:3000])

B = "#### Features 🚀\n\n- a\n\n#### Bugfixes ⛑️\n\n- b\n"


def stops(name, *args):
    global fails
    try:
        ar.reapply(*args)
    except ar.Stop as e:
        print(f"stop ok  {name}: {e}")
        return
    fails += 1
    print(f"NO STOP  {name}")


stops("PR edits an existing line", "next.md", "section", B, B.replace("- a", "- a2") + "", B)
stops("section gone upstream", "next.md", "section", B, B.replace("- a\n", "- a\n- mine\n"),
      "#### Bugfixes ⛑️\n\n- b\n")
stops("already upstream", "next.md", "section", B, B.replace("- a\n", "- a\n- mine\n"),
      B.replace("- a\n", "- a\n- mine\n- c\n"))
T = "-- one --\na\n\n"
stops("case name taken", "txtar.txt", "end", T, T + "-- two --\nb\n", T + "-- two --\nzzz\n")
stops("nothing added", "next.md", "section", B, B, B + "- x\n")

D = "#### Features 🚀\n\n- a\n\n#### Features 🚀\n\n- z\n"
stops("heading twice upstream", "next.md", "section", B, B.replace("- a\n", "- a\n- mine\n"), D)
stops("CRLF", "next.md", "section", B, B.replace("- a\n", "- a\n- mine\n"), B.replace("\n", "\r\n"))

# empty section after a release resets next.md (the 2026-09-08 shape)
got = ar.reapply("next.md", "section", B.replace("- a\n", "- a\n- old\n"),
                 B.replace("- a\n", "- a\n- old\n- mine\n"), "#### Features 🚀\n\n#### Bugfixes ⛑️\n\n")
exp = "#### Features 🚀\n\n- mine\n\n#### Bugfixes ⛑️\n\n"
print("empty section:", "ok" if got == exp else f"BAD {got!r}")
fails += got != exp
print("FAILS", fails)
sys.exit(1 if fails else 0)

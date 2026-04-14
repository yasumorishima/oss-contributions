"""Record or refresh OSS PR contributions in README.md.

Usage:
    python scripts/record_pr.py https://github.com/optuna/optuna/pull/6435
    python scripts/record_pr.py --refresh
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CONFIG = ROOT / "config.json"


def run(cmd: list[str]) -> str | None:
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    if result.returncode != 0:
        print(f"Command failed: {' '.join(cmd)}", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return None
    return result.stdout.strip()


def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return {"projects": {}}


def save_config(config: dict) -> None:
    CONFIG.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """Parse GitHub PR URL into (owner/repo, full_repo, pr_number)."""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if not m:
        print(f"Invalid PR URL: {url}", file=sys.stderr)
        sys.exit(1)
    return m.group(1), int(m.group(2))


def fetch_pr_info(url: str) -> dict | None:
    """Fetch PR info via gh CLI. Returns None on failure."""
    raw = run(["gh", "pr", "view", url, "--json", "number,title,state,url"])
    if not raw:
        return None
    return json.loads(raw)


def get_repo_info(repo: str) -> tuple[str, str]:
    """Fetch language and description from GitHub API."""
    raw = run(["gh", "api", f"repos/{repo}", "--jq", ".language"])
    language = raw or "Unknown"
    raw2 = run(["gh", "api", f"repos/{repo}", "--jq", ".description"])
    description = raw2 or ""
    return language, description


def state_to_status(state: str) -> str:
    """Convert GitHub API state to display status."""
    mapping = {"OPEN": "Open", "CLOSED": "Closed", "MERGED": "Merged"}
    return mapping.get(state.upper(), state)


def parse_readme() -> list[str]:
    return README.read_text(encoding="utf-8").splitlines()


def find_summary_table(lines: list[str]) -> tuple[int, int]:
    """Find start and end line indices of the Summary table (inclusive)."""
    start = None
    for i, line in enumerate(lines):
        if line.startswith("| Project |"):
            start = i
            continue
        if start is not None and i == start + 1:
            continue  # header separator
        if start is not None:
            if line.startswith("|"):
                end = i
            else:
                break
    return start, end


def find_project_section(lines: list[str], repo: str) -> int | None:
    """Find the line index of a project section header. Returns None if not found."""
    pattern = f"### [{repo}]("
    for i, line in enumerate(lines):
        if line.startswith("### [") and repo in line:
            return i
    return None


def find_next_section(lines: list[str], start: int) -> int:
    """Find the start of the next ### section after `start`, or end of file."""
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("### "):
            return i
    return len(lines)


def find_pr_table_in_section(
    lines: list[str], section_start: int, section_end: int
) -> tuple[int, int] | None:
    """Find the PR table (| # | PR | ...) within a section. Returns (header_line, last_row_line)."""
    table_start = None
    table_end = None
    for i in range(section_start, section_end):
        if lines[i].startswith("| # | PR |"):
            table_start = i
            continue
        if table_start is not None and i == table_start + 1:
            continue  # separator
        if table_start is not None:
            if lines[i].startswith("|"):
                table_end = i
            else:
                break
    if table_start is not None and table_end is not None:
        return table_start, table_end
    return None


def extract_all_pr_urls(lines: list[str]) -> list[str]:
    """Extract all GitHub PR URLs from the README."""
    urls = []
    for line in lines:
        for m in re.finditer(
            r"https://github\.com/[^/]+/[^/]+/pull/\d+", line
        ):
            urls.append(m.group(0))
    return list(dict.fromkeys(urls))  # deduplicate preserving order


def get_pr_number_from_row(row: str) -> int | None:
    """Extract the PR number from a table row like '| 1 | [#123](...) | ...'."""
    m = re.search(r"\[#(\d+)\]", row)
    if m:
        return int(m.group(1))
    return None


def get_max_row_number(lines: list[str], table_start: int, table_end: int) -> int:
    """Get the maximum row number (first column) from a PR table."""
    max_num = 0
    for i in range(table_start + 2, table_end + 1):  # skip header + separator
        m = re.match(r"\|\s*(\d+)\s*\|", lines[i])
        if m:
            max_num = max(max_num, int(m.group(1)))
    return max_num


def build_pr_row(row_num: int, pr_number: int, url: str, status: str, title: str) -> str:
    return f"| {row_num} | [#{pr_number}]({url}) | {status} | {title} |"


def build_project_section(repo: str, description: str) -> list[str]:
    """Build a new project section."""
    return [
        f"### [{repo}](https://github.com/{repo})",
        "",
        description,
        "",
        "| # | PR | Status | Description |",
        "|---|---|---|---|",
    ]


def get_group_order(config: dict, repo: str) -> tuple[int, str]:
    """Return (group_index, repo) for sorting. Projects are ordered by group, then by repo name."""
    groups = config.get("groups", [])
    for idx, group in enumerate(groups):
        prefixes = group.get("prefixes", [])
        if not prefixes:
            continue
        for prefix in prefixes:
            if repo.startswith(prefix):
                return (idx, repo)
    # Fallback: "Other" group (last)
    return (len(groups) - 1, repo)


def reorder_sections(lines: list[str]) -> list[str]:
    """Reorder project sections (### [...]) by group order defined in config.json."""
    config = load_config()

    # Find the "## Contributions by Project" header
    contrib_start = None
    for i, line in enumerate(lines):
        if line.startswith("## Contributions by Project"):
            contrib_start = i
            break
    if contrib_start is None:
        return lines

    # Extract all ### sections after contrib_start
    sections: list[tuple[str, list[str]]] = []  # (repo, section_lines)
    current_repo = None
    current_lines: list[str] = []
    for i in range(contrib_start + 1, len(lines)):
        if lines[i].startswith("### ["):
            if current_repo is not None:
                # Strip trailing blank lines from previous section
                while current_lines and not current_lines[-1].strip():
                    current_lines.pop()
                sections.append((current_repo, current_lines))
            m = re.match(r"### \[([^\]]+)\]", lines[i])
            current_repo = m.group(1) if m else ""
            current_lines = [lines[i]]
        elif current_repo is not None:
            current_lines.append(lines[i])

    if current_repo is not None:
        while current_lines and not current_lines[-1].strip():
            current_lines.pop()
        sections.append((current_repo, current_lines))

    if not sections:
        return lines

    # Sort sections by group order
    sections.sort(key=lambda s: get_group_order(config, s[0]))

    # Rebuild: header portion + sorted sections
    result = lines[: contrib_start + 1]
    for _repo, sec_lines in sections:
        result.append("")
        result.extend(sec_lines)
    result.append("")

    return result


def recalculate_summary(lines: list[str]) -> list[str]:
    """Recalculate the Summary table based on actual PR tables in each section."""
    config = load_config()
    project_rows = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("### ["):
            m = re.match(r"### \[([^\]]+)\]\((https://github\.com/[^)]+)\)", lines[i])
            if not m:
                i += 1
                continue
            repo = m.group(1)
            url = m.group(2)
            section_end = find_next_section(lines, i)

            # Find config info
            proj_config = config.get("projects", {}).get(repo, {})
            language = proj_config.get("language", "Unknown")

            # Count statuses from PR table
            merged = open_count = closed = 0
            pr_table = find_pr_table_in_section(lines, i, section_end)
            if pr_table:
                for j in range(pr_table[0] + 2, pr_table[1] + 1):
                    row = lines[j]
                    # Skip rows without a PR link (e.g. "| 2 | — | Closed |")
                    if "| — |" in row or "| \u2014 |" in row:
                        continue
                    if "| Merged |" in row:
                        merged += 1
                    elif "| Open |" in row:
                        open_count += 1
                    elif "| Closed |" in row:
                        closed += 1
                    elif "| Done |" in row:
                        # "Done" entries (e.g. issue comments) count as Closed
                        closed += 1
            total = merged + open_count + closed

            if total > 0:
                short_name = repo.split("/")[-1]
                row_text = f"| [{short_name}]({url}) | {language} | {total} | {merged} | {open_count} | {closed} |"
                sort_key = get_group_order(config, repo)
                project_rows.append((sort_key, row_text))
            i = section_end
        else:
            i += 1

    # Sort by group order, then by repo name
    project_rows.sort(key=lambda x: x[0])

    grand_total = grand_merged = grand_open = grand_closed = 0
    for _, row_text in project_rows:
        parts = [x.strip() for x in row_text.split("|")]
        # parts: ['', 'name', 'lang', 'total', 'merged', 'open', 'closed', '']
        grand_total += int(parts[3])
        grand_merged += int(parts[4])
        grand_open += int(parts[5])
        grand_closed += int(parts[6])

    summary_lines = [
        "| Project | Language | PRs | Merged | Open | Closed |",
        "|---|---|---|---|---|---|",
    ]
    summary_lines.extend(row_text for _, row_text in project_rows)
    summary_lines.append(
        f"| **Total** | | **{grand_total}** | **{grand_merged}** | **{grand_open}** | **{grand_closed}** |"
    )
    return summary_lines


def is_excluded(url: str, config: dict) -> bool:
    """Check whether a PR URL is in the excludePRs list."""
    excluded = set(config.get("excludePRs", []))
    return url in excluded


def record_pr(url: str) -> None:
    """Record or update a single PR."""
    repo, pr_number = parse_pr_url(url)
    config_early = load_config()
    if is_excluded(url, config_early):
        print(f"Skipped (excluded in config.json): {url}")
        return
    pr_info = fetch_pr_info(url)
    if pr_info is None:
        print(f"Failed to fetch PR info: {url}", file=sys.stderr)
        sys.exit(1)
    status = state_to_status(pr_info["state"])
    title = pr_info["title"]

    config = load_config()
    if repo not in config.get("projects", {}):
        print(f"New project detected: {repo}. Fetching info from GitHub...")
        language, description = get_repo_info(repo)
        config.setdefault("projects", {})[repo] = {
            "language": language,
            "description": description,
        }
        save_config(config)
        print(f"  Added to config.json: language={language}")

    proj = config["projects"][repo]
    lines = parse_readme()

    section_idx = find_project_section(lines, repo)

    if section_idx is None:
        # Create new section at end of file
        new_section = build_project_section(repo, proj["description"])
        new_row = build_pr_row(1, pr_number, url, status, title)
        new_section.append(new_row)

        # Add blank line before new section if needed
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(new_section)
        print(f"Created new section for {repo}")
    else:
        section_end = find_next_section(lines, section_idx)
        pr_table = find_pr_table_in_section(lines, section_idx, section_end)

        if pr_table is None:
            # Section exists but no PR table - add one
            insert_idx = section_idx + 1
            # Skip description lines
            while insert_idx < section_end and lines[insert_idx].strip():
                insert_idx += 1
            insert_idx += 1  # skip blank line after description

            table_lines = [
                "| # | PR | Status | Description |",
                "|---|---|---|---|",
                build_pr_row(1, pr_number, url, status, title),
            ]
            for j, tl in enumerate(table_lines):
                lines.insert(insert_idx + j, tl)
            print(f"Added PR table to {repo}")
        else:
            # Check if PR already exists
            existing_row_idx = None
            for i in range(pr_table[0] + 2, pr_table[1] + 1):
                if f"[#{pr_number}]" in lines[i]:
                    existing_row_idx = i
                    break

            if existing_row_idx is not None:
                # Update existing row
                old_row = lines[existing_row_idx]
                row_num_match = re.match(r"\|\s*(\d+)\s*\|", old_row)
                row_num = int(row_num_match.group(1)) if row_num_match else 1
                lines[existing_row_idx] = build_pr_row(
                    row_num, pr_number, url, status, title
                )
                print(f"Updated PR #{pr_number} status to {status}")
            else:
                # Add new row (at top of table, with incremented number)
                max_num = get_max_row_number(lines, pr_table[0], pr_table[1])
                new_row = build_pr_row(max_num + 1, pr_number, url, status, title)
                # Insert after header separator (line pr_table[0]+1)
                lines.insert(pr_table[0] + 2, new_row)
                print(f"Added PR #{pr_number} to {repo}")

    # Reorder sections by group, then recalculate summary
    lines = reorder_sections(lines)
    summary_lines = recalculate_summary(lines)
    summary_start, summary_end = find_summary_table(lines)
    if summary_start is not None:
        lines[summary_start : summary_end + 1] = summary_lines

    README.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"README.md updated. Review changes before committing.")



def detect_untracked_repos() -> int:
    """Detect repos with PRs by author that are not in config.json.

    Uses GitHub search API to find all PRs, then auto-adds any upstream
    repos missing from config.json (skipping the author's own forks).
    Returns count of newly added repos.
    """
    config = load_config()
    author = config.get("author", "")
    if not author:
        print("No 'author' field in config.json. Skipping detection.", file=sys.stderr)
        return 0

    known_repos = set(config.get("projects", {}).keys())

    # Collect all upstream repos with PRs by author
    all_repos: set[str] = set()
    for search_flag in (["--merged"], ["--state", "open"], ["--state", "closed"]):
        raw = run(
            ["gh", "search", "prs", "--author", author, *search_flag,
             "--limit", "200", "--json", "repository",
             "--jq", ".[].repository.nameWithOwner"]
        )
        if raw:
            for repo in raw.splitlines():
                repo = repo.strip()
                # Skip author's own repos (forks) - only track upstream
                if repo and not repo.startswith(f"{author}/"):
                    all_repos.add(repo)

    new_repos = all_repos - known_repos
    if not new_repos:
        print("No untracked repos detected.")
        return 0

    print(f"Detected {len(new_repos)} untracked repo(s):")
    added = 0
    for repo in sorted(new_repos):
        print(f"  Adding: {repo}")
        language, description = get_repo_info(repo)
        config.setdefault("projects", {})[repo] = {
            "language": language,
            "description": description,
        }
        added += 1

    save_config(config)
    print(f"Added {added} repo(s) to config.json.")
    return added


def scan_new_prs() -> int:
    """Scan config.json repos for new PRs not yet in README. Returns count added."""
    config = load_config()
    author = config.get("author", "")
    if not author:
        print("No 'author' field in config.json. Skipping scan.", file=sys.stderr)
        return 0

    existing_urls = set(extract_all_pr_urls(parse_readme()))
    added = 0

    for repo in config.get("projects", {}):
        raw = run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--author", author,
                "--state", "all",
                "--json", "number,title,state,url",
                "--limit", "200",
            ]
        )
        if not raw:
            continue
        try:
            prs = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for pr in prs:
            url = pr.get("url", "")
            if url and url not in existing_urls:
                if is_excluded(url, config):
                    print(f"  Skipped (excluded): {url}")
                    continue
                print(f"  New PR found: {url}")
                record_pr(url)
                existing_urls.add(url)
                added += 1

    return added


def remove_excluded_from_readme(lines: list[str], config: dict) -> tuple[list[str], int]:
    """Remove rows for excluded PRs from README. Returns (new_lines, removed_count).

    Also collapses sections that become empty (no remaining PRs) by removing
    the entire ### section.
    """
    excluded = config.get("excludePRs", [])
    if not excluded:
        return lines, 0

    # Build set of PR numbers per repo to remove
    excluded_by_repo: dict[str, set[int]] = {}
    for url in excluded:
        try:
            repo, num = parse_pr_url(url)
        except SystemExit:
            continue
        excluded_by_repo.setdefault(repo, set()).add(num)

    removed = 0
    new_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect ### section header
        m = re.match(r"### \[([^\]]+)\]", line)
        if m and m.group(1) in excluded_by_repo:
            repo = m.group(1)
            section_end = find_next_section(lines, i)
            section = lines[i:section_end]
            nums_to_remove = excluded_by_repo[repo]
            kept_section = []
            for s_line in section:
                row_match = re.match(r"\|\s*\d+\s*\|\s*\[#(\d+)\]", s_line)
                if row_match and int(row_match.group(1)) in nums_to_remove:
                    removed += 1
                    continue
                kept_section.append(s_line)
            # Section is empty if no data rows remain (any "| N | [#... " line)
            has_data_row = any(
                re.match(r"\|\s*\d+\s*\|\s*\[#\d+\]", s) for s in kept_section
            )
            if not has_data_row:
                # Drop whole section + trailing blank lines
                while new_lines and not new_lines[-1].strip():
                    new_lines.pop()
                i = section_end
                continue
            new_lines.extend(kept_section)
            i = section_end
        else:
            new_lines.append(line)
            i += 1

    return new_lines, removed


def refresh_all() -> None:
    """Scan for new PRs and refresh status of all PRs in README.md."""
    detect_untracked_repos()
    new_count = scan_new_prs()
    if new_count:
        print(f"\nAdded {new_count} new PR(s). Now refreshing statuses...")
    else:
        print("No new PRs found.")

    # Remove excluded PRs from README first (before refresh wastes API calls on them)
    config = load_config()
    lines = parse_readme()
    lines, removed = remove_excluded_from_readme(lines, config)
    if removed > 0:
        print(f"Removed {removed} excluded PR row(s) from README.")
    urls = extract_all_pr_urls(lines)
    print(f"Found {len(urls)} PR URLs to refresh.")

    changes = 0
    for url in urls:
        repo, pr_number = parse_pr_url(url)
        pr_info = fetch_pr_info(url)
        if pr_info is None:
            print(f"  Skipping {url}: failed to fetch")
            continue

        new_status = state_to_status(pr_info["state"])

        # Find and update the row
        for i, line in enumerate(lines):
            if f"[#{pr_number}]({url})" in line:
                # Check current status
                current_status = None
                for s in ["Open", "Closed", "Merged"]:
                    if f"| {s} |" in line:
                        current_status = s
                        break
                if current_status and current_status != new_status:
                    lines[i] = line.replace(f"| {current_status} |", f"| {new_status} |")
                    print(f"  Updated {repo}#{pr_number}: {current_status} -> {new_status}")
                    changes += 1
                break

    # Always reorder sections by group (even if no status changes)
    lines = reorder_sections(lines)

    # Recalculate summary
    summary_lines = recalculate_summary(lines)
    summary_start, summary_end = find_summary_table(lines)
    if summary_start is not None:
        lines[summary_start : summary_end + 1] = summary_lines

    README.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if changes > 0:
        print(f"\n{changes} PR(s) updated. README.md reordered and updated.")
    else:
        print("\nAll PRs are up to date. README.md reordered.")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python scripts/record_pr.py <PR_URL>")
        print("  python scripts/record_pr.py --refresh")
        print("  python scripts/record_pr.py --detect-untracked")
        sys.exit(1)

    if sys.argv[1] == "--refresh":
        refresh_all()
    elif sys.argv[1] == "--detect-untracked":
        detect_untracked_repos()
    else:
        record_pr(sys.argv[1])


if __name__ == "__main__":
    main()

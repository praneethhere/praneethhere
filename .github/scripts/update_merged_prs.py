#!/usr/bin/env python3
"""Update the MERGED-PRS section in README.md.

Bot-aware version:
- Includes normal GitHub-merged PRs only when the Pulls API confirms merged=True.
- Also handles repositories such as pytorch/pytorch where a maintainer bot can land a PR,
  add a `Merged` label, and close it without GitHub setting `merged=True` on the PR.
- Still excludes open PRs and closed-unmerged PRs unless there is strong bot-merge evidence.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

USERNAME = os.getenv("GITHUB_USERNAME", "praneethhere")
MAX_PRS = int(os.getenv("MAX_PRS", "8"))
GH_TOKEN = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
README_PATH = Path(os.getenv("README_PATH", "README.md"))

START = "<!-- MERGED-PRS:START -->"
END = "<!-- MERGED-PRS:END -->"

# Search is only used to discover PRs. Every candidate is verified afterward.
SEARCH_PAGE_SIZE = max(30, min(100, MAX_PRS * 6))

# Some projects use merge bots that close PRs after landing them in a separate commit,
# so GitHub can show "Closed with unmerged commits" even though the project marked it merged.
# Keep this allowlist tight so normal closed-unmerged PRs do not leak into the profile README.
BOT_MERGE_REPOS = {
    repo.strip().lower()
    for repo in os.getenv("BOT_MERGE_REPOS", "pytorch/pytorch").split(",")
    if repo.strip()
}

MERGE_LABELS = {"merged"}
MERGE_BOT_LOGIN_HINTS = (
    "mergebot",
    "merge-bot",
    "pytorchmergebot",
)

PROJECT_DISPLAY_NAMES = {
    "numpy/numpy": "NumPy",
    "pandas-dev/pandas": "pandas",
    "excalidraw/excalidraw": "Excalidraw",
    "pre-commit/pre-commit": "pre-commit",
    "apple/containerization": "Apple Containerization",
    "pytorch/pytorch": "PyTorch",
    "tensorflow/tensorflow": "TensorFlow",
    "pytest-dev/pytest": "pytest",
    "kubernetes/kubernetes": "Kubernetes",
    "microsoft/vscode": "VS Code",
}


def github_get(url: str):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{USERNAME}-profile-readme-updater",
    }
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"

    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def repo_name_from_api_url(repository_url: str) -> str:
    return repository_url.rstrip("/").split("/repos/", 1)[-1]


def issue_api_url(repo: str, number: int) -> str:
    return f"https://api.github.com/repos/{repo}/issues/{number}"


def timeline_api_url(repo: str, number: int) -> str:
    return f"https://api.github.com/repos/{repo}/issues/{number}/timeline?per_page=100"


def format_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


def clean_title(title: str) -> str:
    return title.replace("|", "\\|").replace("\n", " ").strip()


def display_repo(repo: str) -> str:
    return PROJECT_DISPLAY_NAMES.get(repo.lower(), repo)


def label_names(obj: dict) -> set[str]:
    names: set[str] = set()
    for label in obj.get("labels", []) or []:
        if isinstance(label, dict):
            name = label.get("name")
        else:
            name = str(label)
        if name:
            names.add(name.strip().lower())
    return names


def actor_login(event: dict) -> str:
    actor = event.get("actor") or {}
    return str(actor.get("login") or "").lower()


def is_merge_bot(login: str) -> bool:
    return any(hint in login for hint in MERGE_BOT_LOGIN_HINTS)


def bot_merge_evidence(repo: str, number: int, search_item: dict, pr_details: dict) -> tuple[bool, str | None]:
    """Return (is_bot_merged, merge_date).

    This is intentionally conservative. It only runs for allowlisted bot-merge repos
    and requires the PR to be closed plus strong evidence such as a Merged label
    and/or a merge-bot close event with a commit id.
    """

    repo_key = repo.lower()
    if repo_key not in BOT_MERGE_REPOS:
        return False, None

    if pr_details.get("state") != "closed":
        return False, None

    try:
        issue = github_get(issue_api_url(repo, number))
    except Exception as exc:  # noqa: BLE001 - keep workflow resilient
        print(f"Warning: could not fetch issue details for {repo}#{number}: {exc}", file=sys.stderr)
        issue = {}

    merged_label_seen = bool(label_names(search_item) & MERGE_LABELS) or bool(label_names(issue) & MERGE_LABELS)
    bot_closed_with_commit = False
    best_date = pr_details.get("closed_at") or search_item.get("closed_at")

    try:
        timeline = github_get(timeline_api_url(repo, number))
    except Exception as exc:  # noqa: BLE001 - keep workflow resilient
        print(f"Warning: could not fetch timeline for {repo}#{number}: {exc}", file=sys.stderr)
        timeline = []

    if isinstance(timeline, list):
        for event in timeline:
            event_name = event.get("event")
            login = actor_login(event)
            created_at = event.get("created_at")

            if event_name == "labeled":
                label = event.get("label") or {}
                label_name = str(label.get("name") or "").strip().lower()
                if label_name in MERGE_LABELS:
                    merged_label_seen = True
                    best_date = created_at or best_date

            if event_name == "closed" and is_merge_bot(login):
                # Closed timeline events commonly include commit_id when the close is tied to a commit.
                if event.get("commit_id") or event.get("commit_url"):
                    bot_closed_with_commit = True
                    best_date = created_at or best_date

    return bool(merged_label_seen and bot_closed_with_commit), best_date


def verified_merge_status(item: dict, pr_details: dict) -> tuple[bool, str | None]:
    repo = repo_name_from_api_url(item["repository_url"])
    number = int(item["number"])

    # Normal GitHub merge path.
    if pr_details.get("state") == "closed" and pr_details.get("merged") is True and pr_details.get("merged_at"):
        return True, pr_details.get("merged_at")

    # Bot-merge path for repos like PyTorch.
    return bot_merge_evidence(repo, number, item, pr_details)


def fetch_recent_merged_prs() -> list[dict]:
    query = f"is:pr author:{USERNAME} -repo:{USERNAME}/{USERNAME}"
    params = urlencode(
        {
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": SEARCH_PAGE_SIZE,
        },
        quote_via=quote,
    )
    data = github_get(f"https://api.github.com/search/issues?{params}")

    prs: list[dict] = []
    seen_urls: set[str] = set()

    for item in data.get("items", []):
        if len(prs) >= MAX_PRS:
            break

        pr_api_url = item.get("pull_request", {}).get("url")
        html_url = item.get("html_url")
        if not pr_api_url or not html_url or html_url in seen_urls:
            continue

        try:
            pr_details = github_get(pr_api_url)
        except Exception as exc:  # noqa: BLE001 - keep workflow resilient
            print(f"Warning: could not fetch PR details for {html_url}: {exc}", file=sys.stderr)
            continue

        is_merged, merge_date = verified_merge_status(item, pr_details)
        if not is_merged:
            continue

        repo = repo_name_from_api_url(item["repository_url"])
        prs.append(
            {
                "repo": display_repo(repo),
                "title": clean_title(item["title"]),
                "url": html_url,
                "merged_at": format_date(merge_date),
            }
        )
        seen_urls.add(html_url)

    return prs


def build_table(prs: list[dict]) -> str:
    lines = [
        START,
        "| Project | Merged Pull Request | Merged |",
        "|---|---|---|",
    ]

    if not prs:
        lines.append("| — | No merged PRs found yet. | — |")
    else:
        for pr in prs:
            lines.append(f"| {pr['repo']} | [{pr['title']}]({pr['url']}) | {pr['merged_at']} |")

    lines.append(END)
    return "\n".join(lines)


def update_readme() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    table = build_table(fetch_recent_merged_prs())

    pattern = re.compile(rf"{re.escape(START)}.*?{re.escape(END)}", flags=re.DOTALL)
    if not pattern.search(readme):
        raise RuntimeError(f"Could not find README markers: {START} / {END}")

    README_PATH.write_text(pattern.sub(table, readme), encoding="utf-8")


if __name__ == "__main__":
    update_readme()

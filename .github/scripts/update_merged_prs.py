#!/usr/bin/env python3
"""Update the MERGED-PRS section in README.md.

Strict version: only includes pull requests whose real PR details contain
merged_at. This prevents closed-unmerged PRs or still-open PRs from appearing
if GitHub search returns noisy results.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

USERNAME = os.getenv("GITHUB_USERNAME", "praneethhere")
MAX_PRS = int(os.getenv("MAX_PRS", "8"))
GH_TOKEN = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
README_PATH = Path("README.md")

START = "<!-- MERGED-PRS:START -->"
END = "<!-- MERGED-PRS:END -->"

# Pull more than MAX_PRS, then strictly filter by merged_at.
# This avoids showing wrong PRs if the search result contains noise.
SEARCH_PAGE_SIZE = max(30, min(100, MAX_PRS * 5))


def github_get(url: str) -> dict:
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
    # Example: https://api.github.com/repos/numpy/numpy -> numpy/numpy
    return repository_url.rstrip("/").split("/repos/", 1)[-1]


def format_date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return value[:10]


def clean_title(title: str) -> str:
    # Prevent markdown table breakage.
    return title.replace("|", "\\|").replace("\n", " ").strip()


def fetch_recent_merged_prs() -> list[dict]:
    # Search is used only for discovery. Real merged status is verified below
    # using each PR's `merged_at` field from the Pulls API.
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
    search_url = f"https://api.github.com/search/issues?{params}"
    data = github_get(search_url)

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

        merged_at = pr_details.get("merged_at")
        merged = bool(pr_details.get("merged"))
        state = pr_details.get("state")

        # Strict gate: only real merged PRs are allowed into the README.
        if not merged or not merged_at or state != "closed":
            continue

        prs.append(
            {
                "repo": repo_name_from_api_url(item["repository_url"]),
                "title": clean_title(item["title"]),
                "url": html_url,
                "merged_at": format_date(merged_at),
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
            lines.append(
                f"| {pr['repo']} | [{pr['title']}]({pr['url']}) | {pr['merged_at']} |"
            )

    lines.append(END)
    return "\n".join(lines)


def update_readme() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    table = build_table(fetch_recent_merged_prs())

    pattern = re.compile(
        rf"{re.escape(START)}.*?{re.escape(END)}",
        flags=re.DOTALL,
    )

    if not pattern.search(readme):
        raise RuntimeError(f"Could not find README markers: {START} / {END}")

    updated = pattern.sub(table, readme)
    README_PATH.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    update_readme()

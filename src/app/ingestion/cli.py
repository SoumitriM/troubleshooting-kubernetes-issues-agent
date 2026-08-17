import argparse
from pathlib import Path

from app.ingestion.github_client import (
    GitHubIssueClient,
    GitHubSettings,
    save_issues_jsonl,
)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch closed GitHub issues")
    parser.add_argument("--owner", required=True, help="GitHub repository owner")
    parser.add_argument("--repo", required=True, help="GitHub repository name")
    parser.add_argument("--limit", type=positive_int, default=100)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = GitHubSettings()
    token = (
        settings.github_token.get_secret_value() if settings.github_token else None
    )

    with GitHubIssueClient(token=token) as client:
        issues = client.fetch_closed_issues(args.owner, args.repo, args.limit)

    save_issues_jsonl(issues, args.output)
    print(f"Saved {len(issues)} issues to {args.output}")


if __name__ == "__main__":
    main()

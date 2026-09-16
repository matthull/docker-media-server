#!/usr/bin/env python3
"""Fail if any relative link in README.md or docs/*.md does not resolve in the repo tree.

The guides are read from the repo, not a wiki (see CLAUDE.md), so every link must be a file
path (`sonarr.md#anchor`) that exists, with an anchor matching a real heading. Upstream wiki
URLs are rejected outright. Run: python3 scripts/check_doc_links.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)\s]+)\)")


def slugs(path):
    """GitHub-style heading anchors for a markdown file (fenced code skipped)."""
    out = set()
    in_fence = False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence or not line.startswith("#"):
            continue
        title = re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower())
        out.add(title.replace(" ", "-"))
    return out


def main():
    files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    errors = []
    checked = 0
    for f in files:
        for url in LINK.findall(f.read_text()):
            if "bcanfield/docker-media-server/wiki" in url:
                errors.append(f"{f.name}: upstream wiki link {url}")
                continue
            if re.match(r"[a-z]+:", url):
                continue
            checked += 1
            target, _, anchor = url.partition("#")
            dest = (f.parent / target).resolve() if target else f
            if not dest.exists():
                errors.append(f"{f.name}: {url} -> no such file")
            elif anchor and dest.suffix == ".md" and anchor not in slugs(dest):
                errors.append(f"{f.name}: {url} -> no heading #{anchor}")
    print(f"checked {checked} relative links in {len(files)} files")
    for e in errors:
        print("FAIL", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

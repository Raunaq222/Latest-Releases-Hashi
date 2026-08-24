#!/usr/bin/env python3
"""
Release digest POC.

Pulls releases from the GitHub Releases API for a set of repos, normalizes them
into one table, classifies each changelog line, and emits:
  - releases.csv        the normalized table (the thing everything downstream reads)
  - digest_draft.md     a human-editable draft, NOT a finished newsletter

Usage:
    export GITHUB_TOKEN=ghp_xxx          # optional, but raises rate limit 60 -> 5000/hr
    python3 release_digest.py --days 30
    python3 release_digest.py --demo      # runs offline against bundled sample data
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# The smaller products. Add or remove freely - this list is the whole config.
REPOS = [
    ("Boundary", "hashicorp/boundary"),
    ("Nomad", "hashicorp/nomad"),
    ("Packer", "hashicorp/packer"),
    ("Waypoint", "hashicorp/waypoint"),
    ("Vagrant", "hashicorp/vagrant"),
    ("Consul", "hashicorp/consul"),
]

CATEGORY_RULES = [
    ("breaking", r"\bbreaking\b|\bremoved\b|\bdeprecat"),
    ("security", r"\bsecurity\b|\bcve-\d|\bvulnerab"),
    ("feature", r"^\s*(feat|feature|added|new)\b|\badds?\b|\bnow supports\b"),
    ("fix", r"^\s*(fix|bug)\b|\bfixed\b|\bresolves?\b"),
]

CATEGORY_ORDER = ["breaking", "security", "feature", "fix", "other"]
CATEGORY_LABEL = {
    "breaking": "Breaking changes",
    "security": "Security",
    "feature": "New and improved",
    "fix": "Fixes",
    "other": "Other changes",
}


def classify(line):
    low = line.lower()
    for name, pattern in CATEGORY_RULES:
        if re.search(pattern, low):
            return name
    return "other"


def clean(line):
    """Strip markdown bullets, section prefixes, PR links, and trailing noise."""
    line = re.sub(r"^\s*[-*+]\s*", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)          # [text](url) -> text
    line = re.sub(r"\(\[?#\d+\]?[^)]*\)\s*$", "", line)           # trailing (#1234)
    line = re.sub(r"\s*\[GH-\d+\]\s*$", "", line)                 # trailing [GH-1234]
    line = re.sub(r"^\*\*([^*]+)\*\*:?\s*", r"\1: ", line)        # **area**: -> area:
    line = re.sub(r"\s+", " ", line)
    return line.strip(" .")


def parse_body(body):
    """Pull bullet lines out of a release body, skipping headers and boilerplate."""
    entries = []
    for raw in (body or "").splitlines():
        if not re.match(r"^\s*[-*+]\s+\S", raw):
            continue
        text = clean(raw)
        if len(text) < 12:
            continue
        if re.search(r"full changelog|see the changelog|download|checksum", text, re.I):
            continue
        entries.append(text)
    return entries


def fetch(repo, token):
    url = f"https://api.github.com/repos/{repo}/releases?per_page=20"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "release-digest-poc",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def collect(days, token, demo=False):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []

    if demo:
        with open(os.path.join(os.path.dirname(__file__), "sample_releases.json")) as f:
            payload = json.load(f)
        source = [(p, payload.get(r, [])) for p, r in REPOS]
    else:
        source = []
        for product, repo in REPOS:
            try:
                source.append((product, fetch(repo, token)))
            except urllib.error.HTTPError as e:
                print(f"  ! {repo}: HTTP {e.code} ({e.reason})", file=sys.stderr)
            except Exception as e:
                print(f"  ! {repo}: {e}", file=sys.stderr)

    for product, releases in source:
        for rel in releases:
            if rel.get("draft"):
                continue
            published = rel.get("published_at")
            if not published:
                continue
            when = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if when < cutoff:
                continue
            for text in parse_body(rel.get("body")):
                category = classify(text)
                text = re.sub(
                    rf"^{category}\b:?\s*", "", text, flags=re.I
                ).strip()
                text = text[0].upper() + text[1:] if text else text
                rows.append({
                    "product": product,
                    "version": rel.get("tag_name", ""),
                    "date": when.date().isoformat(),
                    "prerelease": str(bool(rel.get("prerelease"))).lower(),
                    "category": category,
                    "summary": text,
                    "url": rel.get("html_url", ""),
                })
    rows.sort(key=lambda r: (r["product"], r["date"]))
    return rows


def write_csv(rows, path):
    cols = ["product", "version", "date", "prerelease", "category", "summary", "url"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def write_digest(rows, days, path):
    by_product = defaultdict(list)
    for r in rows:
        by_product[r["product"]].append(r)

    window = f"last {days} days"
    out = [
        f"# Release digest draft - {window}",
        "",
        "> DRAFT. Auto-assembled from GitHub Releases. Every line below needs a human",
        "> pass before it goes anywhere near a customer. Cut ruthlessly - a good digest",
        "> is 5 items, not 50.",
        "",
        f"**{len(rows)} changelog entries across {len(by_product)} products.**",
        "",
        "---",
        "",
    ]

    for product in sorted(by_product):
        entries = by_product[product]
        versions = sorted({e["version"] for e in entries})
        out.append(f"## {product}")
        out.append("")
        out.append(f"*{', '.join(versions)} - {len(entries)} entries*")
        out.append("")

        grouped = defaultdict(list)
        for e in entries:
            grouped[e["category"]].append(e)

        for cat in CATEGORY_ORDER:
            if cat not in grouped:
                continue
            out.append(f"**{CATEGORY_LABEL[cat]}**")
            out.append("")
            for e in grouped[cat][:6]:
                out.append(f"- {e['summary']}")
            extra = len(grouped[cat]) - 6
            if extra > 0:
                out.append(f"- _...and {extra} more_")
            out.append("")
        out.append("")

    with open(path, "w") as f:
        f.write("\n".join(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--demo", action="store_true", help="run offline against sample data")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not (token or args.demo):
        print("note: no GITHUB_TOKEN set, you get 60 requests/hour\n", file=sys.stderr)

    rows = collect(args.days, token, demo=args.demo)
    if not rows:
        print("No entries found in the window.", file=sys.stderr)
        return 1

    csv_path = os.path.join(args.outdir, "releases.csv")
    md_path = os.path.join(args.outdir, "digest_draft.md")
    write_csv(rows, csv_path)
    write_digest(rows, args.days, md_path)

    products = sorted({r["product"] for r in rows})
    print(f"{len(rows)} entries across {len(products)} products: {', '.join(products)}")
    print(f"  -> {csv_path}")
    print(f"  -> {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

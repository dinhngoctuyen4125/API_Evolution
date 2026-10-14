"""Add GitHub release tags and commit SHAs to PyPI version records."""

import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPOSITORIES = {
    "numpy": "numpy/numpy",
    "pandas": "pandas-dev/pandas",
    "pytorch": "pytorch/pytorch",
    "tensorflow": "tensorflow/tensorflow",
    "scipy": "scipy/scipy",
    "scikit-learn": "scikit-learn/scikit-learn",
    "seaborn": "mwaskom/seaborn",
    "transformers": "huggingface/transformers",
}
DATA_DIR = Path(__file__).resolve().parent / "data"
INPUT_PATH = DATA_DIR / "pypi_versions.json"
OUTPUT_PATH = DATA_DIR / "pypi_versions_with_github.json"
NUMERIC_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
GIT_VERSION_TAG = re.compile(r"^v?([0-9]+(?:\.[0-9]+)+)$")
FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def fetch_tags(repository: str):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pypi-github-version-mapper",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    page = 1
    while True:
        query = urlencode({"per_page": 100, "page": page})
        url = f"https://api.github.com/repos/{repository}/tags?{query}"
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                tags = json.load(response)
        except HTTPError as exc:
            if exc.code in (403, 429):
                raise RuntimeError(
                    f"GitHub API returned HTTP {exc.code} for {url}. "
                    "Check the rate limit or set GITHUB_TOKEN."
                ) from exc
            raise RuntimeError(f"GitHub API returned HTTP {exc.code} for {url}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not fetch {url}: {exc}") from exc

        if not isinstance(tags, list):
            raise RuntimeError(f"Unexpected GitHub API response for {url}")
        if not tags:
            break

        yield from tags
        if len(tags) < 100:
            break
        page += 1


def tag_candidates(library: str, version: str):
    if library == "scikit-learn":
        return (version, f"v{version}")
    return (f"v{version}", version)


def version_key(version: str):
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def resolve_tag(library, version, tags_by_name, tags_by_version):
    for tag in tag_candidates(library, version):
        if tag in tags_by_name:
            return tag, tags_by_name[tag]

    equivalent = tags_by_version.get(version_key(version), [])
    if len({sha for _, sha in equivalent}) != 1:
        return None, None

    preferred_prefix = library != "scikit-learn"
    tag, sha = min(
        equivalent,
        key=lambda item: (
            item[0].startswith("v") != preferred_prefix,
            abs(len(item[0].lstrip("v").split(".")) - len(version.split("."))),
            item[0],
        ),
    )
    return tag, sha


def enrich_records(records):
    if not isinstance(records, list):
        raise ValueError(f"{INPUT_PATH} must contain a JSON array")

    versions_by_library = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Record {index} must be a JSON object")
        library = record.get("library")
        version = record.get("version")
        if (
            library not in REPOSITORIES
            or not isinstance(version, str)
            or not NUMERIC_VERSION.fullmatch(version)
        ):
            raise ValueError(f"Record {index} has an unknown library or invalid version")
        versions_by_library.setdefault(library, set()).add(version)

    resolved_by_library = {}
    for library, versions in versions_by_library.items():
        repository = REPOSITORIES[library]
        wanted_keys = {version_key(version) for version in versions}
        tags_by_name = {}
        tags_by_version = {}
        for item in fetch_tags(repository):
            tag = item["name"]
            match = GIT_VERSION_TAG.fullmatch(tag)
            if match is None:
                continue
            key = version_key(match.group(1))
            if key not in wanted_keys:
                continue
            sha = item["commit"]["sha"]
            if not FULL_SHA.fullmatch(sha):
                raise ValueError(f"Invalid commit SHA for {repository} tag {tag}: {sha}")
            tags_by_name[tag] = sha
            tags_by_version.setdefault(key, []).append((tag, sha))

        resolved = {
            version: resolve_tag(library, version, tags_by_name, tags_by_version)
            for version in versions
        }
        resolved_by_library[library] = resolved
        matched_count = sum(tag is not None for tag, _ in resolved.values())
        print(f"{library}: matched {matched_count} versions", file=sys.stderr)

    enriched = []
    for record in records:
        library = record["library"]
        version = record["version"]
        repository = REPOSITORIES[library]
        tag, sha = resolved_by_library[library][version]
        enriched.append({
            **record,
            "tag": tag,
            "commit_sha": sha,
            "source_url": f"https://github.com/{repository}/tree/{sha}" if sha else None,
        })
    return enriched


def main():
    records = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    enriched = enrich_records(records)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Saved {len(enriched)} records to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

"""Collect versions with published distribution files from PyPI."""

import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECTS = {
    "numpy": "https://pypi.org/pypi/numpy/json",
    "pandas": "https://pypi.org/pypi/pandas/json",
    "pytorch": "https://pypi.org/pypi/torch/json",
    "tensorflow": "https://pypi.org/pypi/tensorflow/json",
    "scipy": "https://pypi.org/pypi/scipy/json",
    "scikit-learn": "https://pypi.org/pypi/scikit-learn/json",
    "seaborn": "https://pypi.org/pypi/seaborn/json",
    "transformers": "https://pypi.org/pypi/transformers/json",
}
FINAL_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "pypi_versions.json"


def fetch_versions(api_url: str):
    request = Request(api_url, headers={"User-Agent": "pypi-versions-crawler"})
    try:
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"PyPI returned HTTP {exc.code} for {api_url}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not fetch {api_url}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected PyPI response for {api_url}")
    releases = data.get("releases")
    if not isinstance(releases, dict):
        raise RuntimeError(f"Unexpected PyPI response for {api_url}")

    # Keep final numeric versions with at least one non-yanked distribution file.
    versions = [
        version for version, files in releases.items()
        if FINAL_VERSION.fullmatch(version)
        and isinstance(files, list)
        and any(
            isinstance(file, dict)
            and not file.get("yanked", False)
            and not file.get("yanked_reason")
            for file in files
        )
    ]
    return sorted(versions, key=lambda version: tuple(map(int, version.split("."))))


def main():
    records = []
    for library, api_url in PROJECTS.items():
        versions = fetch_versions(api_url)
        records.extend({"library": library, "version": version} for version in versions)
        print(f"{library}: {len(versions)} versions", file=sys.stderr)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Saved {len(records)} records to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

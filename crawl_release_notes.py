"""Collect raw HTML release notes for curated PyPI/GitHub versions."""

import argparse
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
INPUT_PATH = DATA_DIR / "pypi_versions_with_github_diy.json"
DEFAULT_OUTPUT_PATH = DATA_DIR / "release_notes.json"
CACHE_DIR = ROOT_DIR / "cache"
LIBRARIES = ["numpy"]
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def numpy_release_note_urls(version: str):
    parts = version.split(".")
    if len(parts) < 2:
        return ()
    series = ".".join(parts[:2])
    filename = f"{version}-notes.html"
    return (
        f"https://numpy.org/doc/{series}/release/{filename}",
        f"https://numpy.org/doc/stable/release/{filename}",
        f"https://numpy.org/devdocs/release/{filename}",
    )


# Every new library needs its own URL resolver because documentation sites differ.
URL_BUILDERS = {"numpy": numpy_release_note_urls}


class TitleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.parts.append(data)


def is_numpy_release_note(html: str, version: str):
    parser = TitleParser()
    parser.feed(html)
    title = " ".join("".join(parser.parts).split())
    accepted_versions = [version]
    parts = version.split(".")
    if len(parts) == 3 and parts[-1] == "0":
        # Some NumPy x.y.0 pages use "NumPy x.y Release Notes" as their title.
        accepted_versions.append(".".join(parts[:2]))
    return any(
        re.search(rf"\bNumPy\s+{re.escape(candidate)}\s+Release Notes\b", title, re.I)
        for candidate in accepted_versions
    )


def fetch_html(url: str):
    request = Request(url, headers={"User-Agent": "api-evolution-release-notes/1.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                if response.headers.get_content_type() != "text/html":
                    raise RuntimeError(f"Expected HTML at {url}")
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset), response.geturl()
        except HTTPError as exc:
            if exc.code in (404, 410):
                return None
            if exc.code not in RETRYABLE_STATUS or attempt == 2:
                raise RuntimeError(f"HTTP {exc.code} while fetching {url}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Could not fetch {url}: {exc}") from exc
        time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch {url}")


def find_release_note(library: str, version: str):
    for url in URL_BUILDERS[library](version):
        fetched = fetch_html(url)
        if fetched is None:
            continue
        content, release_url = fetched
        if library == "numpy" and not is_numpy_release_note(content, version):
            print(f"Skipping page with a different title: {release_url}", file=sys.stderr)
            continue
        return content, release_url
    return None


def load_versions():
    records = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{INPUT_PATH} must contain a JSON array")
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"Invalid record in {INPUT_PATH}")
        library, version = record.get("library"), record.get("version")
        if not isinstance(library, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", library):
            raise ValueError(f"Missing or invalid library in {INPUT_PATH}: {library!r}")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", version):
            raise ValueError(f"Missing or invalid version for {library}: {version!r}")
        key = library, version
        if key in seen:
            raise ValueError(f"Duplicate version in {INPUT_PATH}: {key}")
        seen.add(key)
    return records


def cache_path(library: str, version: str):
    return CACHE_DIR / library / version / "docs" / "release_notes.html"


def relative_path(path: Path):
    return path.relative_to(ROOT_DIR).as_posix()


def save_html(path: Path, content: str, *, replace_existing=False):
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return
        if not replace_existing:
            raise ValueError(f"Cached HTML differs from existing release note: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.parent.joinpath("source").mkdir(exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def load_existing(path: Path, valid_keys):
    if not path.exists():
        return {}
    existing = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(existing, list):
        raise ValueError(f"{path} must contain a JSON array")
    by_key = {}
    for document in existing:
        if not isinstance(document, dict):
            raise ValueError(f"Invalid record in {path}")
        key = document.get("library"), document.get("version")
        if key not in valid_keys:
            raise ValueError(f"Existing release note is absent from curated input: {key}")
        if key in by_key:
            raise ValueError(f"Duplicate release note in {path}: {key}")
        by_key[key] = document
    return by_key


def merge_existing(records, existing):
    result = []
    for input_record in records:
        record = dict(input_record)
        key = record["library"], record["version"]
        old = existing.get(key, {})
        path = cache_path(*key)
        content = old.get("content")
        if content is not None:
            if not isinstance(content, str) or not content:
                raise ValueError(f"Invalid embedded HTML for {key}")
            save_html(path, content)
            release_url = old.get("release_url") or old.get("source_url")
        else:
            release_url = old.get("release_url")
            old_path = old.get("release_path")
            if old_path and old_path != relative_path(path):
                raise ValueError(f"Unexpected release_path for {key}: {old_path}")
        if release_url is not None and not isinstance(release_url, str):
            raise ValueError(f"Invalid release URL for {key}")
        if release_url and path.is_file():
            record["release_url"] = release_url
            record["release_path"] = relative_path(path)
        else:
            record["release_url"] = None
            record["release_path"] = None
        result.append(record)
    return result


def save_output(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    try:
        temp_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def available_documents(records):
    return [r for r in records if r["release_url"] and r["release_path"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="append", dest="versions", help="Crawl one version; repeat to select more")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--delay", type=float, default=0.25, help="Seconds between versions (default: 0.25)")
    parser.add_argument("--refresh", action="store_true", help="Download again even when cached HTML exists")
    parser.add_argument("--migrate-only", action="store_true", help="Move embedded HTML to cache without network requests")
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay must not be negative")
    if args.refresh and args.migrate_only:
        parser.error("--refresh and --migrate-only cannot be used together")
    unsupported = set(LIBRARIES) - URL_BUILDERS.keys()
    if unsupported:
        parser.error(f"No release-note URL builder for: {', '.join(sorted(unsupported))}")

    input_records = load_versions()
    valid_keys = {(r["library"], r["version"]) for r in input_records}
    selected = [r for r in input_records if r["library"] in LIBRARIES and
                (not args.versions or r["version"] in args.versions)]
    if args.versions and not set(args.versions).issubset({r["version"] for r in selected}):
        parser.error("Some requested versions are absent from the selected libraries in the input file")
    if not selected and not args.migrate_only:
        parser.error("No versions selected")

    existing = load_existing(args.output, valid_keys)
    records = merge_existing(input_records, existing)
    by_key = {(r["library"], r["version"]): r for r in records}
    missing = []
    downloaded = 0
    if not args.migrate_only:
        for input_record in selected:
            library, version = input_record["library"], input_record["version"]
            record = by_key[library, version]
            if record["release_url"] and record["release_path"] and not args.refresh:
                continue
            if downloaded:
                time.sleep(args.delay)
            found = find_release_note(library, version)
            if found is None:
                missing.append((library, version))
                print(f"No release note found: {library} {version}", file=sys.stderr)
                continue
            content, release_url = found
            path = cache_path(library, version)
            save_html(path, content, replace_existing=True)
            record["release_url"] = release_url
            record["release_path"] = relative_path(path)
            save_output(args.output, available_documents(records))
            downloaded += 1
            print(f"Fetched: {library} {version} -> {release_url}", file=sys.stderr)

    # The input is the source of metadata, but the output lists only versions
    # whose release-note HTML is available locally.
    documents = available_documents(records)
    save_output(args.output, documents)
    print(f"Saved {len(documents)} release notes to {args.output}; downloaded: {downloaded}; missing: {len(missing)}")


if __name__ == "__main__":
    main()

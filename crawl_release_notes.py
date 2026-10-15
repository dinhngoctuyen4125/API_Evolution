"""Collect raw HTML release notes for curated PyPI/GitHub versions."""

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATA_DIR = Path(__file__).resolve().parent / "data"
INPUT_PATH = DATA_DIR / "pypi_versions_with_github_diy.json"
DEFAULT_OUTPUT_PATH = DATA_DIR / "release_notes.json"
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
        content, source_url = fetched
        if library == "numpy" and not is_numpy_release_note(content, version):
            print(f"Skipping page with a different title: {source_url}", file=sys.stderr)
            continue
        return {
            "library": library,
            "version": version,
            "document_type": "release_note",
            "source_url": source_url,
            "content": content,
        }
    return None


def load_versions(only_versions):
    records = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{INPUT_PATH} must contain a JSON array")
    selected = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"Invalid record in {INPUT_PATH}")
        library, version = record.get("library"), record.get("version")
        if not isinstance(library, str) or not library:
            raise ValueError(f"Missing or invalid library in {INPUT_PATH}: {record!r}")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", version):
            raise ValueError(f"Missing or invalid version for {library}: {version!r}")
        if library not in LIBRARIES or (only_versions and version not in only_versions):
            continue
        key = library, version
        if key not in seen:
            selected.append(key)
            seen.add(key)
    return selected


def merge_documents(existing, updates):
    if not isinstance(existing, list):
        raise ValueError("Existing release notes must be a JSON array")
    by_key = {}
    for document in existing:
        if not isinstance(document, dict):
            raise ValueError("Existing release notes contain a non-object record")
        key = document.get("library"), document.get("version")
        if key in by_key:
            raise ValueError(f"Duplicate release note in existing output: {key}")
        by_key[key] = document
    for document in updates:
        by_key[document["library"], document["version"]] = document

    ordered = []
    for key in load_versions(set()):
        if key in by_key:
            ordered.append(by_key.pop(key))
    ordered.extend(by_key.values())
    return ordered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="append", dest="versions", help="Crawl one version; repeat to select more")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--delay", type=float, default=0.25, help="Seconds between versions (default: 0.25)")
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay must not be negative")
    unsupported = set(LIBRARIES) - URL_BUILDERS.keys()
    if unsupported:
        parser.error(f"No release-note URL builder for: {', '.join(sorted(unsupported))}")

    selected = load_versions(set(args.versions or []))
    if args.versions and not set(args.versions).issubset({v for _, v in selected}):
        parser.error("Some requested versions are absent from the selected libraries in the input file")
    if not selected:
        parser.error("No versions selected")

    documents = []
    missing = []
    for index, (library, version) in enumerate(selected):
        if index:
            time.sleep(args.delay)
        document = find_release_note(library, version)
        if document is None:
            missing.append((library, version))
            print(f"No release note found: {library} {version}", file=sys.stderr)
            continue
        documents.append(document)
        print(f"Fetched: {library} {version} -> {document['source_url']}", file=sys.stderr)

    if not documents:
        raise RuntimeError("No release notes found; output was not written")
    if args.versions and args.output.exists():
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        documents = merge_documents(existing, documents)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(documents, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(documents)} release notes to {args.output}; missing: {len(missing)}")


if __name__ == "__main__":
    main()

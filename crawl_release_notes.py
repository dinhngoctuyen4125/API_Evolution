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
LIBRARIES = [
    "numpy", "pandas", "pytorch", "scipy", "scikit-learn", "seaborn",
    "tensorflow", "transformers",
]
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
GITHUB_REPOS = {
    "pandas": "pandas-dev/pandas",
    "pytorch": "pytorch/pytorch",
    "scipy": "scipy/scipy",
    "scikit-learn": "scikit-learn/scikit-learn",
    "seaborn": "mwaskom/seaborn",
    "tensorflow": "tensorflow/tensorflow",
    "transformers": "huggingface/transformers",
}
NUMPY_151_RELEASE_URL = "https://docs.scipy.org/doc/numpy-1.5.x/release.html"
NUMPY_151_FRAGMENT = "#numpy-1-5-1-release-notes"


def numpy_release_note_urls(version: str):
    parts = version.split(".")
    if len(parts) < 2:
        return ()
    series = ".".join(parts[:2])
    filename = f"{version}-notes.html"
    urls = (
        f"https://numpy.org/doc/{series}/release/{filename}",
        f"https://numpy.org/doc/stable/release/{filename}",
        f"https://numpy.org/devdocs/release/{filename}",
    )
    if version == "1.5.1":
        urls += (NUMPY_151_RELEASE_URL,)
    return urls


def pandas_release_note_urls(version: str):
    series = ".".join(version.split(".")[:2])
    return (
        f"https://pandas.pydata.org/docs/whatsnew/v{version}.html",
        f"https://pandas.pydata.org/pandas-docs/version/{series}/whatsnew/v{version}.html",
        f"https://pandas.pydata.org/pandas-docs/version/{version}/whatsnew/v{version}.html",
    )


def scipy_release_note_urls(version: str):
    filename = f"{version}-notes.html"
    urls = (
        f"https://docs.scipy.org/doc/scipy-{version}/release/{filename}",
        f"https://docs.scipy.org/doc/scipy/release/{filename}",
    )
    if int(version.split(".")[0]) == 0:
        urls += (f"https://sourceforge.net/projects/scipy/files/scipy/{version}/",)
    return urls


def scikit_learn_release_note_urls(version: str):
    series = ".".join(version.split(".")[:2])
    if tuple(map(int, series.split("."))) <= (0, 12):
        return (
            "https://scikit-learn.org/stable/whats_new/older_versions.html",
            "https://scikit-learn.org/0.17/whats_new.html",
        )
    filename = f"v{series}.html"
    return (
        f"https://scikit-learn.org/{series}/whats_new/{filename}",
        f"https://scikit-learn.org/stable/whats_new/{filename}",
    )


def seaborn_release_note_urls(version: str):
    series = ".".join(version.split(".")[:2])
    versions = (version, f"{version}.0") if version.count(".") == 1 else (version,)
    return tuple(
        f"{base}/whatsnew/v{candidate}.html"
        for base in ("https://seaborn.pydata.org", f"https://seaborn.pydata.org/archive/{series}")
        for candidate in versions
    )


def no_documentation_urls(version: str):
    return ()


# GitHub release pages are used when versioned documentation is unavailable.
# For Transformers, a matching /releases/tag/<tag> page is sufficient evidence
# even when its release-note body is empty. Bare source-tree pages are excluded.
URL_BUILDERS = {
    "numpy": numpy_release_note_urls,
    "pandas": pandas_release_note_urls,
    "pytorch": no_documentation_urls,
    "scipy": scipy_release_note_urls,
    "scikit-learn": scikit_learn_release_note_urls,
    "seaborn": seaborn_release_note_urls,
    "tensorflow": no_documentation_urls,
    "transformers": no_documentation_urls,
}


class TitleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.parts = []
        self.heading_level = None
        self.heading_parts = []
        self.headings = []
        self.heading_links = []
        self.heading_link = None
        self.in_heading_link = False
        self.release_body_depth = 0
        self.release_body_parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in {"h1", "h2", "h3", "h4"}:
            self.heading_level = tag
            self.heading_parts = []
            self.heading_link = None
        if tag == "a" and self.heading_level and "headerlink" in dict(attrs).get("class", "").split():
            self.in_heading_link = True
            self.heading_link = dict(attrs).get("href")
        if tag == "div":
            attributes = dict(attrs)
            if self.release_body_depth:
                self.release_body_depth += 1
            elif (attributes.get("data-test-selector") == "body-content"
                  and "markdown-body" in attributes.get("class", "").split()):
                self.release_body_depth = 1

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == self.heading_level:
            heading = " ".join("".join(self.heading_parts).split())
            self.headings.append(heading)
            self.heading_links.append((tag, heading, self.heading_link))
            self.heading_level = None
        if tag == "a":
            self.in_heading_link = False
        if tag == "div" and self.release_body_depth:
            self.release_body_depth -= 1

    def handle_data(self, data):
        if self.in_title:
            self.parts.append(data)
        if self.heading_level and not self.in_heading_link:
            self.heading_parts.append(data)
        if self.release_body_depth:
            self.release_body_parts.append(data)


def page_title(parser: TitleParser):
    return " ".join("".join(parser.parts).split())


def version_pattern(version: str):
    return rf"(?<![\d.])v?{re.escape(version)}(?![\d.])"


def scikit_learn_release_labels(version: str):
    labels = [version]
    if version.count(".") == 2 and version.endswith(".0"):
        # The first release in a series can be headed "Version x.y".
        labels.append(version.rsplit(".", 1)[0])
    return labels


def scikit_learn_release_heading(parser: TitleParser, version: str):
    for label in scikit_learn_release_labels(version):
        matches = [
            heading for heading in parser.heading_links
            if re.fullmatch(rf"Version\s+{version_pattern(label)}", heading[1], re.I)
        ]
        if matches:
            return next((heading for heading in matches if heading[0] != "h1"), matches[0])
    return None


def is_numpy_release_note(html: str, version: str):
    parser = TitleParser()
    parser.feed(html)
    title = page_title(parser)
    accepted_versions = [version]
    parts = version.split(".")
    if len(parts) == 3 and parts[-1] == "0":
        # Some NumPy x.y.0 pages use "NumPy x.y Release Notes" as their title.
        accepted_versions.append(".".join(parts[:2]))
    title_matches = any(
        re.search(rf"\bNumPy\s+{re.escape(candidate)}\s+Release Notes\b", title, re.I)
        for candidate in accepted_versions
    )
    heading_matches = any(
        re.fullmatch(rf"NumPy\s+{re.escape(candidate)}\s+Release Notes", heading, re.I)
        for candidate in accepted_versions for heading in parser.headings
    )
    return title_matches or heading_matches


def is_release_note(html: str, library: str, version: str, *, github_tag=None):
    if library == "numpy":
        return is_numpy_release_note(html, version)
    parser = TitleParser()
    parser.feed(html)
    title = page_title(parser)
    if github_tag is not None:
        return bool(
            re.search(r"^Release\s+", title, re.I)
            and "GitHub" in title
            and (library != "transformers" or GITHUB_REPOS[library] in title)
            and (library == "transformers" or "".join(parser.release_body_parts).strip())
        )
    if library == "pandas":
        return bool(re.search(rf"(?:What.s new in|Version)\s+{version_pattern(version)}", title, re.I))
    if library == "scipy":
        if re.search(rf"\bSciPy\s+{version_pattern(version)}\s+Release Notes\b", title, re.I):
            return True
        return bool(
            re.search(rf"Browse\s+/scipy/{re.escape(version)}\b", title, re.I)
            and "SourceForge.net" in title
            and any(re.fullmatch(rf"SciPy\s+{version_pattern(version)}\s+Release Notes", heading, re.I)
                    for heading in parser.headings)
        )
    if library == "seaborn":
        versions = (version, f"{version}.0") if version.count(".") == 1 else (version,)
        return any(re.search(version_pattern(candidate), title, re.I) for candidate in versions)
    if library == "scikit-learn":
        return scikit_learn_release_heading(parser, version) is not None
    raise ValueError(f"Unsupported library: {library}")


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


def find_release_note(record):
    library, version = record["library"], record["version"]
    for url in URL_BUILDERS[library](version):
        fetched = fetch_html(url)
        if fetched is None:
            continue
        content, release_url = fetched
        if not is_release_note(content, library, version):
            print(f"Skipping page with a different title: {release_url}", file=sys.stderr)
            continue
        if library == "scikit-learn":
            heading_parser = TitleParser()
            heading_parser.feed(content)
            heading = scikit_learn_release_heading(heading_parser, version)
            if heading and heading[2] and heading[2].startswith("#"):
                release_url += heading[2]
        if library == "numpy" and version == "1.5.1" and release_url == NUMPY_151_RELEASE_URL:
            release_url += NUMPY_151_FRAGMENT
        return content, release_url
    tag = record.get("tag")
    if library in GITHUB_REPOS and isinstance(tag, str) and tag:
        url = f"https://github.com/{GITHUB_REPOS[library]}/releases/tag/{tag}"
        fetched = fetch_html(url)
        if fetched is not None:
            content, release_url = fetched
            if release_url.rstrip("/") == url.rstrip("/") and is_release_note(
                content, library, version, github_tag=tag
            ):
                return content, release_url
            print(f"Skipping GitHub page without accepted release evidence for {tag}: {release_url}",
                  file=sys.stderr)
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
    return CACHE_DIR / library / "docs" / version / "release_notes.html"


def legacy_cache_path(library: str, version: str):
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
            legacy_path = legacy_cache_path(*key)
            if old_path and old_path not in (relative_path(path), relative_path(legacy_path)):
                raise ValueError(f"Unexpected release_path for {key}: {old_path}")
            if old_path == relative_path(legacy_path) and legacy_path.is_file():
                if path.is_file():
                    if path.read_bytes() != legacy_path.read_bytes():
                        raise ValueError(f"Cached HTML differs between old and new paths for {key}")
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    legacy_path.replace(path)
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
    parser.add_argument("--library", action="append", choices=LIBRARIES,
                        help="Select a library; repeat to select more (default: all eight)")
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
    selected_libraries = set(args.library or LIBRARIES)
    selected = [r for r in input_records if r["library"] in selected_libraries and
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
    attempted = 0
    if not args.migrate_only:
        for input_record in selected:
            library, version = input_record["library"], input_record["version"]
            record = by_key[library, version]
            if record["release_url"] and record["release_path"] and not args.refresh:
                continue
            if attempted:
                time.sleep(args.delay)
            attempted += 1
            found = find_release_note(input_record)
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

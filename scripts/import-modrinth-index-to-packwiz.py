#!/usr/bin/env python3

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class Candidate:
    path: str
    project_id: str
    version_id: str
    filename: str
    download_url: str


@dataclass(frozen=True)
class MissingLink:
    path: str
    downloads: list[str]


@dataclass(frozen=True)
class Failure:
    path: str
    project_id: str
    version_id: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RefreshResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Modrinth manifest files into a packwiz pack."
    )
    parser.add_argument(
        "manifest_path",
        nargs="?",
        help="Path to modrinth.index.json. Defaults to modrinth.index.json.",
    )
    parser.add_argument(
        "--manifest",
        dest="manifest_option",
        help="Path to modrinth.index.json. Overrides the positional path.",
    )
    parser.add_argument("--pack-file", default="pack.toml")
    parser.add_argument("--meta-folder", default="mods")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Manifest file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Manifest file is not valid JSON: {path}: {exc}")

    if not isinstance(manifest, dict):
        raise SystemExit(f"Manifest root must be a JSON object: {path}")
    if not isinstance(manifest.get("files"), list):
        raise SystemExit(f"Manifest must contain a files list: {path}")
    return manifest


def parse_modrinth_cdn_url(url: str) -> tuple[str, str, str] | None:
    parsed = urlparse(url)
    if parsed.netloc != "cdn.modrinth.com":
        return None

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[0] != "data" or parts[2] != "versions":
        return None

    project_id = parts[1]
    version_id = parts[3]
    filename = parts[4]
    if not project_id or not version_id or not filename:
        return None
    return project_id, version_id, filename


def build_candidates(files: list[object]) -> tuple[list[Candidate], list[MissingLink]]:
    candidates: list[Candidate] = []
    missing_links: list[MissingLink] = []

    for index, entry in enumerate(files, start=1):
        if not isinstance(entry, dict):
            missing_links.append(MissingLink(f"<entry {index}>", []))
            continue

        path = str(entry.get("path") or f"<entry {index}>")
        downloads_value = entry.get("downloads") or []
        downloads = [url for url in downloads_value if isinstance(url, str)] if isinstance(downloads_value, list) else []

        candidate = None
        for url in downloads:
            parsed = parse_modrinth_cdn_url(url)
            if parsed is None:
                continue
            project_id, version_id, url_filename = parsed
            manifest_filename = PurePosixPath(path).name
            filename = manifest_filename or url_filename
            candidate = Candidate(path, project_id, version_id, filename, url)
            break

        if candidate is None:
            missing_links.append(MissingLink(path, downloads))
        else:
            candidates.append(candidate)

    return candidates, missing_links


def find_manual_override_jars(manifest_paths: set[str]) -> list[Path]:
    override_root = Path("overrides/mods")
    if not override_root.is_dir():
        return []

    manual = []
    for jar in sorted(override_root.glob("*.jar")):
        virtual_path = f"mods/{jar.name}"
        if virtual_path not in manifest_paths:
            manual.append(jar)
    return manual


def build_import_command(candidate: Candidate, pack_file: Path, meta_folder: str) -> list[str]:
    return [
        "packwiz",
        "--pack-file",
        str(pack_file),
        "--meta-folder",
        meta_folder,
        "-y",
        "modrinth",
        "add",
        "--project-id",
        candidate.project_id,
        "--version-id",
        candidate.version_id,
        "--version-filename",
        candidate.filename,
    ]


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def print_list(title: str, items: list[str]) -> None:
    print(title)
    if not items:
        print("  None")
        return
    for item in items:
        print(f"  - {item}")


def print_summary(
    total: int,
    candidates: list[Candidate],
    succeeded: int,
    failures: list[Failure],
    missing_links: list[MissingLink],
    manual_jars: list[Path],
    refresh_result: RefreshResult | None,
    dry_run: bool,
) -> None:
    print()
    print("Summary")
    print(f"Total manifest files: {total}")
    print(f"Import candidates: {len(candidates)}")
    print(f"Succeeded: {succeeded}/{total}")
    print(f"Failed imports: {len(failures)}")
    print(f"Manifest files without usable Modrinth links: {len(missing_links)}")
    print(f"Dry run: {'yes' if dry_run else 'no'}")

    if refresh_result is None:
        print("Refresh: not run")
    elif refresh_result.returncode == 0:
        print("Refresh: succeeded")
    else:
        print(f"Refresh: failed with exit code {refresh_result.returncode}")
        if refresh_result.stderr.strip():
            print("Refresh stderr:")
            print(refresh_result.stderr.strip())

    if failures:
        print()
        print("Failed imports")
        for failure in failures:
            print(f"  - {failure.path}")
            print(f"    project_id: {failure.project_id}")
            print(f"    version_id: {failure.version_id}")
            print(f"    exit_code: {failure.returncode}")
            print(f"    command: {shlex.join(failure.command)}")
            if failure.stderr.strip():
                print(f"    stderr: {failure.stderr.strip()}")
            elif failure.stdout.strip():
                print(f"    stdout: {failure.stdout.strip()}")

    print()
    print("Manifest files without usable Modrinth links")
    if not missing_links:
        print("  None")
    for missing in missing_links:
        print(f"  - {missing.path}")
        if missing.downloads:
            for url in missing.downloads:
                print(f"    download: {url}")
        else:
            print("    download: None")

    print()
    print_list(
        "Local override JARs not represented in the manifest",
        [str(path) for path in manual_jars],
    )


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest_option or args.manifest_path or "modrinth.index.json")
    pack_file = Path(args.pack_file)

    if not pack_file.is_file():
        print(f"Pack file not found: {pack_file}", file=sys.stderr)
        return 2

    manifest = load_manifest(manifest_path)
    files = manifest["files"]
    candidates, missing_links = build_candidates(files)
    manifest_paths = {
        str(entry.get("path"))
        for entry in files
        if isinstance(entry, dict) and entry.get("path")
    }
    manual_jars = find_manual_override_jars(manifest_paths)

    if args.dry_run:
        for candidate in candidates:
            command = build_import_command(candidate, pack_file, args.meta_folder)
            print(f"DRY RUN {candidate.path}: {shlex.join(command)}")
        print_summary(
            len(files),
            candidates,
            0,
            [],
            missing_links,
            manual_jars,
            None,
            True,
        )
        return 0

    if shutil.which("packwiz") is None:
        print("packwiz is not installed or is not on PATH", file=sys.stderr)
        print_summary(
            len(files),
            candidates,
            0,
            [],
            missing_links,
            manual_jars,
            None,
            False,
        )
        return 2

    succeeded = 0
    failures: list[Failure] = []
    for index, candidate in enumerate(candidates, start=1):
        command = build_import_command(candidate, pack_file, args.meta_folder)
        print(f"[{index}/{len(candidates)}] Importing {candidate.path}")
        result = run_command(command)
        if result.returncode == 0:
            succeeded += 1
            print("  OK")
        else:
            failures.append(
                Failure(
                    candidate.path,
                    candidate.project_id,
                    candidate.version_id,
                    command,
                    result.returncode,
                    result.stdout,
                    result.stderr,
                )
            )
            print(f"  FAILED exit {result.returncode}")

    refresh_result = None
    if succeeded:
        refresh_command = ["packwiz", "--pack-file", str(pack_file), "refresh"]
        refresh = run_command(refresh_command)
        refresh_result = RefreshResult(
            refresh_command,
            refresh.returncode,
            refresh.stdout,
            refresh.stderr,
        )

    print_summary(
        len(files),
        candidates,
        succeeded,
        failures,
        missing_links,
        manual_jars,
        refresh_result,
        False,
    )
    return 1 if failures or missing_links or (refresh_result and refresh_result.returncode != 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())

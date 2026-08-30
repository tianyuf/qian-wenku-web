"""Command-line validation for a prepared corpus artifact."""

import argparse
import os
from pathlib import Path

from .artifacts import artifact_is_valid, validate_artifact


def run_preflight(artifact_dir=None):
    configured = artifact_dir or os.environ.get("WENKU_ARTIFACT_DIR", "artifacts")
    return validate_artifact(Path(configured))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help="Directory containing corpus.db, page_images.json, and manifest.json",
    )
    args = parser.parse_args()

    checks = run_preflight(artifact_dir=args.artifact_dir)
    for name, ok, detail in checks:
        status = "ok" if ok else "FAIL"
        print(f"{status:4} {name}: {detail}")
    raise SystemExit(0 if artifact_is_valid(checks) else 1)


if __name__ == "__main__":
    main()

"""Scan only the files supplied by pre-commit."""

import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> int:
    # A temporary tree excludes local credentials and deployment state.
    with TemporaryDirectory(prefix="cloudbox-gitleaks-") as directory:
        root = Path(directory)
        for filename in sys.argv[1:]:
            source = Path(filename)
            if source.is_absolute() or ".." in source.parts or source.is_symlink():
                raise ValueError(f"Expected a repository file: {filename}")
            target = root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        return subprocess.run(
            ["gitleaks", "dir", "--redact", "--verbose", "--no-banner", str(root)],
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())

"""Build the ONE zip package that all 7 Lambda functions run.

    uv run python scripts/build_lambda.py            # -> dist/lambda-<hash>.zip

Why a zip and not a container image: every native library we use (pypdfium2,
Pillow, pydantic-core, lxml) publishes ready-built Linux ARM64 wheels. So the
package can be assembled on any machine, without Docker or CPU emulation, and
there is no image registry (ECR) to run. Size: ~48 MB zipped, ~107 MB unpacked;
the Lambda limit is 250 MB unpacked.

What goes in:
  1. the runtime dependencies from uv.lock (exact tested versions), as wheels for
     Lambda's platform: Linux, ARM64 (Graviton), glibc 2.28+, CPython 3.12;
  2. boto3 too, although Lambda has one built in: the built-in version changes
     without notice, and Bedrock features (Converse, prompt caching) need a recent one;
  3. the `app/` package (no tests, no docs, no scripts).

Each function then uses a different Handler, e.g. "app.lambda_handlers.worker"
(see app/lambda_handlers.py and infra/cloudformation/app.yaml).

The file name contains a hash of the contents, so a rebuild of unchanged code
produces the same name, and CloudFormation only updates functions when the code
really changed.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

# Lambda runtime python3.12 on arm64 = Amazon Linux 2023 (glibc 2.34) on Graviton.
PLATFORM = "aarch64-manylinux_2_28"
PYTHON_VERSION = "3.12"

# Never ship these: they are only needed to build or test.
_SKIP_DIRS = {"__pycache__", "tests", "test"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True).stdout


def build() -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pkg = tmp_path / "pkg"

        # 1. Exact dependency versions from the lock file.
        requirements = tmp_path / "requirements.txt"
        requirements.write_text(_run(["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project"]))

        # 2. Download Linux ARM64 wheels for them (binary only: nothing is compiled here).
        _run(["uv", "pip", "install", "--quiet", "--target", str(pkg),
              "--python-platform", PLATFORM, "--python-version", PYTHON_VERSION,
              "--only-binary=:all:", "-r", str(requirements)])

        # 3. Our code.
        shutil.copytree(ROOT / "app", pkg / "app",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        # 4. Zip it with a fixed file order and fixed timestamps, so identical
        #    inputs give an identical zip (and therefore the same hash and name).
        files = sorted(p for p in pkg.rglob("*") if p.is_file()
                       and not (_SKIP_DIRS & set(p.relative_to(pkg).parts))
                       and p.suffix not in _SKIP_SUFFIXES)
        staging = tmp_path / "lambda.zip"
        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in files:
                info = zipfile.ZipInfo(str(path.relative_to(pkg)), date_time=(2026, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16          # readable by the Lambda runtime user
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())

        digest = hashlib.sha256(staging.read_bytes()).hexdigest()[:16]
        DIST.mkdir(exist_ok=True)
        target = DIST / f"lambda-{digest}.zip"
        shutil.move(str(staging), target)
        unpacked_mb = sum(p.stat().st_size for p in files) / 1e6

    print(f"Built {target.relative_to(ROOT)}  ({target.stat().st_size / 1e6:.1f} MB zipped, "
          f"{unpacked_mb:.0f} MB unpacked, limit 250 MB)")
    return target


if __name__ == "__main__":
    sys.exit(0 if build() else 1)

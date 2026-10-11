#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .build/lambda
# Lambda runs x86_64 Linux, including when packaging from ARM or macOS.
.venv/bin/python -m pip install \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 \
  --only-binary=:all: --upgrade --target .build/lambda \
  'boto3>=1.40,<2' 'httpx>=0.28,<1'
cp -r probe .build/lambda/
.venv/bin/python - <<'PY'
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
root = Path(".build/lambda")
with ZipFile(".build/lambda.zip", "w", ZIP_DEFLATED) as archive:
    for file in root.rglob("*"):
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
            archive.write(file, file.relative_to(root))
PY

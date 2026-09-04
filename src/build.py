#!/usr/bin/env python3
"""
Embed src/credit_notifier.py into main.yaml as gzip + base64.

The single-file template carries the handler source inside it, because the real
handler is far larger than CloudFormation's 4096-byte Code.ZipFile limit. This
script is the only thing that writes that blob, so src/credit_notifier.py stays
the single source of truth and the two cannot drift apart unnoticed.

  ./build.py            regenerate the blob in main.yaml
  ./build.py --check    verify the blob matches src/ (exit 1 if stale)

Why gzip and not plain text: the source embeds at roughly 27 KB once YAML
indentation is added, which puts the template at ~94% of the 51,200-byte
console upload limit. Compressed it is ~9 KB, leaving real headroom. It also
decodes byte-for-byte back to src/credit_notifier.py, so it can be audited with
a diff rather than by eye.
"""
import base64
import gzip
import hashlib
import pathlib
import re
import sys

# This script lives in src/ alongside the handler it embeds; the template it
# writes is one level up at the repo root.
HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
SOURCE = HERE / "credit_notifier.py"
TEMPLATE = REPO / "main.yaml"
MARKER = "__HANDLER_SOURCE_GZ_B64__"
INDENT = " " * 8
WRAP = 76
CONSOLE_LIMIT = 51200


def build_blob(raw: bytes) -> str:
    """mtime=0 keeps the output byte-stable so --check does not false-positive."""
    packed = gzip.compress(raw, compresslevel=9, mtime=0)
    b64 = base64.b64encode(packed).decode()
    return "\n".join(INDENT + b64[i:i + WRAP] for i in range(0, len(b64), WRAP))


def current_blob(template: str) -> str | None:
    m = re.search(r"^      SourceGz: \|\n((?:^ {8}\S.*\n)+)", template, re.M)
    return m.group(1).rstrip("\n") if m else None


def replace_blob(template: str, blob: str) -> str:
    # Replace either the unfilled marker or a previously generated blob.
    pattern = r"(^      SourceGz: \|\n)(?:^ {8}\S.*\n)+"
    if not re.search(pattern, template, re.M):
        sys.exit("ERROR: could not find the 'SourceGz: |' block in main.yaml")
    return re.sub(pattern, lambda m: m.group(1) + blob + "\n", template, count=1, flags=re.M)


def main() -> int:
    check = "--check" in sys.argv

    if not SOURCE.exists():
        sys.exit(f"ERROR: {SOURCE} not found")
    raw = SOURCE.read_bytes()
    template = TEMPLATE.read_text()

    blob = build_blob(raw)
    existing = current_blob(template)

    if check:
        if existing is None:
            print("FAIL  no SourceGz block found in main.yaml")
            return 1
        if MARKER in existing:
            print("FAIL  main.yaml still holds the placeholder - run ./build.py")
            return 1
        if existing != blob:
            print("FAIL  embedded handler is STALE - run ./build.py")
            return 1
        # Prove the embedded blob really decodes back to the source.
        decoded = gzip.decompress(base64.b64decode("".join(existing.split())))
        if decoded != raw:
            print("FAIL  embedded blob does not decode to src/credit_notifier.py")
            return 1
        print(f"PASS  embedded handler is current and decodes byte-identically")
        print(f"      sha256 {hashlib.sha256(raw).hexdigest()[:16]}  {len(raw):,} bytes")
        print(f"      template {len(template):,} bytes "
              f"({len(template) / CONSOLE_LIMIT * 100:.0f}% of the {CONSOLE_LIMIT:,} console limit)")
        return 0

    updated = replace_blob(template, blob)
    TEMPLATE.write_text(updated)

    size = len(updated)
    pct = size / CONSOLE_LIMIT * 100
    print(f"embedded {len(raw):,} bytes of handler "
          f"(sha256 {hashlib.sha256(raw).hexdigest()[:16]})")
    print(f"  as {len(blob.splitlines())} lines of gzip+base64")
    print(f"main.yaml now {size:,} bytes - {pct:.0f}% of the "
          f"{CONSOLE_LIMIT:,}-byte console upload limit")
    if pct > 90:
        print("WARNING: close to the limit. Templates over 51,200 bytes must be "
              "uploaded via S3 rather than pasted into the console.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

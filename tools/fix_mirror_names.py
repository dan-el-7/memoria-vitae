"""One-off mirror fixer: the .offline-m2 mirror was populated from a Gradle
cache, which stores KMP artifacts under variant filenames (e.g. material3.aar),
while repository resolution expects <artifact>-<version>.aar. Copy each
mismatched file to the expected name (originals stay intact). Also removes
copies made by the earlier buggy run (<version>-<artifact>.aar).
"""

from __future__ import annotations

from pathlib import Path

MIRROR = Path(__file__).resolve().parent.parent / "android" / ".offline-m2"

removed = 0
fixed = 0
for version_dir in MIRROR.rglob("*"):
    if not version_dir.is_dir():
        continue
    version = version_dir.name
    artifact = version_dir.parent.name
    expected_prefix = f"{artifact}-{version}"
    # Undo the earlier swapped-name copies.
    for f in version_dir.iterdir():
        if f.is_file() and f.name.startswith(f"{version}-") and f.suffix in (".aar", ".jar") \
                and not f.name.startswith(expected_prefix) and "-" in f.name:
            swapped = artifact in f.name and f.name.index(artifact) < f.name.index(version) \
                if version in f.name and artifact in f.name else False
            if swapped:
                f.unlink()
                removed += 1
    for f in version_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix in (".aar", ".jar") and not f.name.startswith(expected_prefix):
            target = version_dir / f"{expected_prefix}{f.suffix}"
            if not target.exists():
                target.write_bytes(f.read_bytes())
                fixed += 1
print(f"removed swapped: {removed}; fixed: {fixed}")

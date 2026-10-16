"""Checks a built .macroDeckIconPack the way Macro Deck's import and the Store's signing step read it.

The rules mirror IconPackArchive, IconPackRestoreService and the signer's declared-file check in the
Macro Deck SDK and host, so a pack that fails here would be refused or lose icons there.
"""

import hashlib
import json
import re
import sys
import zipfile

MAX_UNSIGNED_ENTRIES = 30_000 - 4
MAX_UNSIGNED_MANIFEST_BYTES = 32 * 1024 * 1024 - 64 * 1024
MAX_ICON_NAME_LENGTH = 128
MAX_APPEARANCES = 8
MAX_TRAITS = 4
TRAIT_TOKEN = re.compile(r"^[a-z][a-zA-Z0-9]{0,31}$")
MASTER_PATH = re.compile(r"^icons/([0-9a-fA-F-]{36})/master\.webp$")


def check(path):
    problems = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) > MAX_UNSIGNED_ENTRIES:
            problems.append(f"{len(names)} entries, limit {MAX_UNSIGNED_ENTRIES}")
        if "pack.json" not in names:
            return ["no pack.json"], None

        manifest_bytes = archive.read("pack.json")
        if len(manifest_bytes) > MAX_UNSIGNED_MANIFEST_BYTES:
            problems.append(f"pack.json is {len(manifest_bytes)} bytes, limit {MAX_UNSIGNED_MANIFEST_BYTES}")
        manifest = json.loads(manifest_bytes)

        for key in ("id", "name"):
            if not manifest.get(key):
                problems.append(f"pack.json has no {key}")
        if not isinstance(manifest.get("ai"), dict):
            problems.append("pack.json declares nothing about AI")

        declared = {entry["path"]: entry for entry in manifest.get("files", [])}
        for name in names:
            if name != "pack.json" and name not in declared:
                problems.append(f"undeclared file {name}")
        for file_path, entry in declared.items():
            if file_path not in names:
                problems.append(f"declared file missing: {file_path}")
                continue
            data = archive.read(file_path)
            if entry["size"] != len(data) or entry["sha256"] != "sha256:" + hashlib.sha256(data).hexdigest():
                problems.append(f"digest or size mismatch: {file_path}")

        masters = {match.group(1).lower(): name for name in names if (match := MASTER_PATH.match(name))}

        def check_master(entry, label):
            master = masters.get(entry["id"].lower())
            if master is None:
                problems.append(f"{label} has no master image")
            elif entry.get("masterContentHash") != "sha256:" + hashlib.sha256(archive.read(master)).hexdigest():
                problems.append(f"{label} fails its declared master hash")

        seen = set()
        icons = manifest.get("icons", [])
        appearance_count = 0
        for icon in icons:
            name = icon.get("name", "")
            if (not name.strip() or name.strip() != name or "/" in name or len(name) > MAX_ICON_NAME_LENGTH
                    or any(ord(c) < 32 or ord(c) == 127 for c in name)):
                problems.append(f"unusable icon name {name!r}")
            if name.lower() in seen:
                problems.append(f"duplicate icon name {name!r}")
            seen.add(name.lower())
            check_master(icon, f"icon {name!r}")

            appearances = icon.get("appearances") or []
            if len(appearances) > MAX_APPEARANCES:
                problems.append(f"icon {name!r} has {len(appearances)} appearances, limit {MAX_APPEARANCES}")
            trait_keys = set()
            for appearance in appearances:
                traits = appearance.get("traits") or {}
                if not 1 <= len(traits) <= MAX_TRAITS or not all(
                        TRAIT_TOKEN.match(k) and TRAIT_TOKEN.match(v) for k, v in traits.items()):
                    problems.append(f"icon {name!r} has an appearance with invalid traits {traits}")
                key = ";".join(f"{k}={v}" for k, v in sorted(traits.items()))
                if key in trait_keys:
                    problems.append(f"icon {name!r} repeats appearance {key}")
                trait_keys.add(key)
                check_master(appearance, f"appearance {key} of {name!r}")
                appearance_count += 1

    return problems, (len(icons), appearance_count, len(names))


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: check_pack.py <pack.macroDeckIconPack>")

    problems, counts = check(sys.argv[1])
    for problem in problems[:50]:
        print(f"error: {problem}")
    if problems:
        sys.exit(f"{len(problems)} problem(s) found")

    icons, appearances, entries = counts
    print(f"OK: {icons} icons, {appearances} appearances, {entries} archive entries")


if __name__ == "__main__":
    main()

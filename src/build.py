"""Builds the Powered Steel icon pack for Macro Deck 3 as a single .macroDeckIconPack.

Each Font Awesome icon becomes one Macro Deck icon whose default image is the Active style, with
Inactive and Clear as custom appearances.

Requires Inkscape 1.2 or newer on PATH (or --inkscape) and Pillow with WebP support.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from pathlib import Path

from PIL import Image

SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent
TEMPLATE_PATH = SRC_DIR / "Icon_Powered_Steel.svg"

FA_VERSION = "6.2.0"
FA_URL = f"https://use.fontawesome.com/releases/v{FA_VERSION}/fontawesome-free-{FA_VERSION}-desktop.zip"
FA_SHA256 = "c81b76ee7cd89e0cd2125a430ee95d5e2b8ae22e0289658f1f82d70ec39955fd"
# Same precedence the Macro Deck 2 build ended up with when an icon exists in several styles.
FA_STYLES = ("solid", "regular", "brands")

PACK_NAME = "Powered Steel"
PACK_AUTHOR = "DodecaDev-LLC"
PACK_DESCRIPTION = "Font Awesome 6 Free icons on a brushed steel button, with Active, Inactive and Clear styles."
# Fixed ids keep icons on users' buttons across pack updates, since Macro Deck correlates by them.
ID_NAMESPACE = uuid.UUID("5d0f6f2e-8f1b-4f55-9d8e-6a1c2b7f3e10")
PACK_ID = uuid.uuid5(ID_NAMESPACE, "pack")
AI_DECLARATION = {"interaction": False, "generatedContent": False, "generatedAssets": False}

ICON_SIZE = 200
STYLES = ("Active", "Inactive", "Clear")
DEFAULT_STYLE = "Active"

# Mirrors IconPackArchiveLimits and IconAppearanceTraits in the Macro Deck SDK and host.
MAX_UNSIGNED_ENTRIES = 30_000 - 4
MAX_UNSIGNED_MANIFEST_BYTES = 32 * 1024 * 1024 - 64 * 1024
MAX_ICON_NAME_LENGTH = 128
TRAIT_TOKEN = re.compile(r"^[a-z][a-zA-Z0-9]{0,31}$")

# The Macro Deck 2 export pipeline, unchanged so the images match the old packs.
RENDER_ACTIONS = (
    "select-clear; export-area-drawing; export-type:png; "
    f"export-width:{ICON_SIZE}; export-height:{ICON_SIZE}; "
    "select-by-id:icon; object-align:hcenter vcenter drawing; "
    "select-by-id:inactive; select-by-id:active; object-set-attribute:style,display:inline; "
    "export-filename:{out}_Active.png; export-do; "
    "select-clear; select-by-id:active; object-set-attribute:style,display:none; "
    "export-filename:{out}_Inactive.png; export-do; "
    "select-clear; select-by-id:inactive; object-set-attribute:style,display:none; "
    "export-filename:{out}_Clear.png; export-do"
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="Pack version, for example 2.0.0. A leading v is dropped.")
    parser.add_argument("--out", type=Path, default=ROOT_DIR / "dist", help="Directory for the built pack.")
    parser.add_argument("--work", type=Path, default=SRC_DIR / "output", help="Directory for rendered PNGs.")
    parser.add_argument("--cache", type=Path, default=SRC_DIR / "lib", help="Directory for the Font Awesome download.")
    parser.add_argument("--inkscape", default="inkscape", help="Inkscape executable.")
    parser.add_argument("--icons", help="Comma-separated icon names to build instead of all, for quick checks.")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="Parallel Inkscape processes.")
    return parser.parse_args()


def fetch_fontawesome(cache_dir):
    target = cache_dir / f"fontawesome-free-{FA_VERSION}-desktop"
    if target.is_dir():
        return target

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / f"fontawesome-free-{FA_VERSION}-desktop.zip"
    if not archive_path.exists():
        print(f"Downloading {FA_URL}")
        # The CDN answers 403 to Python's default User-Agent.
        request = urllib.request.Request(FA_URL, headers={"User-Agent": "powered-steel-build"})
        with urllib.request.urlopen(request) as response, open(archive_path, "wb") as file:
            shutil.copyfileobj(response, file)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if digest != FA_SHA256:
        archive_path.unlink()
        sys.exit(f"Font Awesome download has SHA-256 {digest}, expected {FA_SHA256}")

    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            if member.startswith("/") or ".." in Path(member).parts:
                sys.exit(f"Unsafe path in Font Awesome archive: {member}")
        archive.extractall(cache_dir)

    return target


def load_icons(fa_dir, only):
    # Every free icon, including the brand logos Font Awesome lists in no category.
    icons = {}
    for style in FA_STYLES:
        for svg in sorted((fa_dir / "svgs" / style).glob("*.svg")):
            icons.setdefault(svg.stem, svg)

    if only:
        wanted = sorted({name.strip() for name in only.split(",") if name.strip()})
        unknown = [name for name in wanted if name not in icons]
        if unknown:
            sys.exit(f"Not a free Font Awesome {FA_VERSION} icon: {', '.join(unknown)}")
        return {name: icons[name] for name in wanted}

    return dict(sorted(icons.items()))


def render(icons, work_dir, inkscape, jobs):
    svg_dir = work_dir / "svg"
    png_dir = work_dir / "png"
    svg_dir.mkdir(parents=True, exist_ok=True)
    png_dir.mkdir(parents=True, exist_ok=True)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if 'xlink:href="icon-import.svg"' not in template:
        sys.exit("The template no longer links icon-import.svg")

    commands = []
    for name, source in icons.items():
        if all((png_dir / f"{name}_{style}.png").exists() for style in STYLES):
            continue

        document = svg_dir / f"{name}.svg"
        document.write_text(
            template.replace('xlink:href="icon-import.svg"', f'xlink:href="{source.resolve().as_posix()}"'),
            encoding="utf-8")
        out = (png_dir / name).resolve().as_posix()
        commands.append(f"file-open:{document.resolve().as_posix()}; {RENDER_ACTIONS.format(out=out)}; file-close")

    print(f"Rendering {len(commands)} of {len(icons)} icons with {jobs} Inkscape process(es)")
    # Bounded batches keep each Inkscape process short-lived, so memory does not grow across the whole set.
    batches = [commands[i:i + 50] for i in range(0, len(commands), 50)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for result in pool.map(lambda batch: run_inkscape(inkscape, batch), batches):
            if result.returncode != 0:
                sys.exit(f"Inkscape failed ({result.returncode}):\n{result.stderr}")

    missing = [f"{name}_{style}.png" for name in icons for style in STYLES
               if not (png_dir / f"{name}_{style}.png").exists()]
    if missing:
        sys.exit(f"Inkscape did not produce {len(missing)} image(s), first: {missing[0]}")

    return png_dir


def run_inkscape(inkscape, batch):
    script = "\n".join(batch) + "\nquit\n"
    return subprocess.run([inkscape, "--shell"], input=script, capture_output=True, text=True)


def to_webp(png_path):
    with Image.open(png_path) as image:
        if image.size != (ICON_SIZE, ICON_SIZE):
            raise ValueError(f"{png_path.name} is {image.size[0]}x{image.size[1]}, expected {ICON_SIZE}x{ICON_SIZE}")
        buffer = io.BytesIO()
        image.convert("RGBA").save(buffer, "WEBP", lossless=True, quality=100, method=6)
        return buffer.getvalue()


def content_hash(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def build_pack(icons, png_dir, version, out_dir, jobs):
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    masters = {}
    entries = []

    # Lossless WebP encoding dominates the build time, so it runs across all cores.
    pngs = [png_dir / f"{name}_{style}.png" for name in icons for style in STYLES]
    print(f"Encoding {len(pngs)} images as WebP")
    with concurrent.futures.ProcessPoolExecutor(max_workers=max(1, jobs)) as pool:
        webps = dict(zip(pngs, pool.map(to_webp, pngs, chunksize=32)))

    for name in icons:
        if len(name) > MAX_ICON_NAME_LENGTH or name.strip() != name or "/" in name:
            sys.exit(f"Icon name not usable in Macro Deck: {name!r}")

        def entry_for(style):
            key = name if style == DEFAULT_STYLE else f"{name}#{style.lower()}"
            entry_id = str(uuid.uuid5(ID_NAMESPACE, key))
            data = webps[png_dir / f"{name}_{style}.png"]
            masters[f"icons/{entry_id}/master.webp"] = data
            return {
                "id": entry_id,
                "name": name,
                "width": ICON_SIZE,
                "height": ICON_SIZE,
                "isAnimated": False,
                "masterContentHash": content_hash(data),
                "originalFileName": f"{name}_{style}.png",
                "originalFormat": "png",
                "state": "Ready",
                "availableSizes": [],
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }

        entry = entry_for(DEFAULT_STYLE)
        appearances = []
        for style in STYLES:
            if style == DEFAULT_STYLE:
                continue
            if not TRAIT_TOKEN.match(style.lower()):
                sys.exit(f"Appearance name not usable as a trait: {style}")
            appearance = entry_for(style)
            appearance["traits"] = {"variant": style.lower()}
            appearances.append(appearance)
        entry["appearances"] = appearances
        entries.append(entry)

    files = [{"path": path, "sha256": content_hash(data), "size": len(data)} for path, data in sorted(masters.items())]
    manifest = {
        "id": str(PACK_ID),
        "name": PACK_NAME,
        "description": PACK_DESCRIPTION,
        "author": PACK_AUTHOR,
        "version": version,
        "isDefault": False,
        "isReadOnly": False,
        "sourceType": "User",
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "icons": entries,
        "ai": AI_DECLARATION,
        "files": files,
    }
    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")

    if len(masters) + 1 > MAX_UNSIGNED_ENTRIES:
        sys.exit(f"{len(masters) + 1} archive entries exceed the limit of {MAX_UNSIGNED_ENTRIES}")
    if len(manifest_bytes) > MAX_UNSIGNED_MANIFEST_BYTES:
        sys.exit(f"pack.json is {len(manifest_bytes)} bytes, over the limit of {MAX_UNSIGNED_MANIFEST_BYTES}")

    out_dir.mkdir(parents=True, exist_ok=True)
    pack_path = out_dir / f"Powered-Steel-{version}.macroDeckIconPack"
    with zipfile.ZipFile(pack_path, "w") as archive:
        # Masters are stored uncompressed like Macro Deck's own export; WebP does not shrink further.
        for path, data in sorted(masters.items()):
            archive.writestr(zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0)), data, zipfile.ZIP_STORED)
        archive.writestr("pack.json", manifest_bytes, zipfile.ZIP_DEFLATED)

    return pack_path, len(entries), len(masters)


def main():
    args = parse_args()
    version = args.version[1:] if args.version.startswith("v") else args.version
    if not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", version):
        sys.exit(f"Version {version!r} is not a semantic version")

    fa_dir = fetch_fontawesome(args.cache)
    icons = load_icons(fa_dir, args.icons)
    png_dir = render(icons, args.work, args.inkscape, args.jobs)
    pack_path, icon_count, file_count = build_pack(icons, png_dir, version, args.out, args.jobs)
    print(f"Built {pack_path} with {icon_count} icons and {file_count} images")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build one Arctic Fuse Kodi repository channel.

Channels:
  alpha   - latest upstream commit + the fork patch
  release - latest upstream commit that bumped the addon version + the fork patch
  stable  - the fork's own branch, rebuilt when its base version changes or forced

The fork is a single commit on top of upstream. Its addon.xml/strings.po edits
collide with upstream, so the patch is rebuilt with those two files reset to
their pre-fork state (see build_patch), cherry-picked cleanly, then re-applied
programmatically.

The workflow prepares the checkouts and publishes the built channel directories;
this script synchronizes one channel at a time.
"""

import argparse
import hashlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

UPSTREAM_REPO = "https://github.com/jurialmunkey/skin.arctic.fuse.3.git"
UPSTREAM_BRANCH = "omega"
UPSTREAM_REF = f"refs/remotes/upstream/{UPSTREAM_BRANCH}"
ADDON_ID = "skin.arctic.fuse.3"
CUSTOM_PROVIDER = "ch1re"
FORK_MAJOR_OFFSET = 3

ADDON_XML = "addon.xml"
PO_PATH = "language/resource.language.en_gb/strings.po"

VERSION_PATTERN = re.compile(r"(?P<base>\d+\.\d+\.\d+)(?:\.(?P<revision>\d+))?(?:-[A-Za-z0-9.-]+)?")


def split_version(value):
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"Unsupported version: {value}")
    return match.group("base"), int(match.group("revision") or 0)


def fork_base_version(source_version):
    base, _ = split_version(source_version)
    major, remainder = base.split(".", 1)
    return f"{int(major) + FORK_MAJOR_OFFSET}.{remainder}"


def next_version(source_version, *existing_versions, suffix=""):
    base = fork_base_version(source_version)
    revisions = []
    for current in filter(None, existing_versions):
        current_base, revision = split_version(current)
        if current_base == base:
            revisions.append(revision)
    return f"{base}.{max(revisions, default=0) + 1}{suffix}"


def validate_addon(root, source):
    if root.tag != "addon" or root.get("id") != ADDON_ID or not root.get("version"):
        raise ValueError(f"Invalid addon manifest: {source}")
    return root


def addon_root(path):
    return validate_addon(ET.parse(path).getroot(), path)


def update_addon(path, version):
    text = path.read_text(encoding="utf-8")
    opening_tag = re.search(r"<addon\b.*?>", text, re.DOTALL)
    if not opening_tag:
        raise ValueError(f"Missing addon element: {path}")

    tag = opening_tag.group()
    provider = re.search(r'\sprovider-name\s*=\s*"([^"]*)"', tag)
    if not provider:
        raise ValueError(f"Missing provider-name in {path}")
    provider_names = {name.strip() for name in provider.group(1).split(",")}
    if CUSTOM_PROVIDER not in provider_names:
        position = provider.end(1)
        tag = f"{tag[:position]}, {CUSTOM_PROVIDER}{tag[position:]}"

    version_match = re.search(r'\sversion\s*=\s*"([^"]*)"', tag)
    if not version_match:
        raise ValueError(f"Missing version in {path}")
    start, end = version_match.span(1)
    tag = f"{tag[:start]}{version}{tag[end:]}"

    updated = text[: opening_tag.start()] + tag + text[opening_tag.end():]
    path.write_text(updated, encoding="utf-8")


def po_contexts(text):
    return set(re.findall(r'^msgctxt\s+"([^"]+)"', text, re.MULTILINE))


def merge_appended_po(upstream, addition):
    if not addition:
        return upstream

    duplicate_contexts = po_contexts(upstream) & po_contexts(addition)
    if duplicate_contexts:
        duplicates = ", ".join(sorted(duplicate_contexts))
        raise ValueError(f"Duplicate strings.po contexts: {duplicates}")

    return f"{upstream.rstrip()}\n\n{addition}\n"


def write_package(source, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        tracked_files = git(source, "ls-files", "-z", strip=False).split("\0")
        for name in filter(None, tracked_files):
            relative = Path(name)
            if any(part.startswith(".") for part in relative.parts):
                continue
            path = source / relative
            if path.is_symlink():
                raise ValueError(f"Unsupported symbolic link in package: {relative}")
            if path.is_file():
                archive.write(path, f"{ADDON_ID}/{relative.as_posix()}")


def copy_assets(source, destination, root):
    for asset in root.iterfind("extension/assets/*"):
        asset_path = Path(asset.text or "")
        if not asset.text or asset_path.is_absolute() or ".." in asset_path.parts:
            raise ValueError(f"Invalid metadata asset path: {asset.text}")
        asset_source = (source / asset_path).resolve()
        if not asset_source.is_relative_to(source) or not asset_source.is_file():
            raise ValueError(f"Missing metadata asset: {asset.text}")
        asset_destination = destination / asset_path
        asset_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset_source, asset_destination)


def build_repository(source, target):
    source = Path(source).resolve()
    target = Path(target).absolute()
    if target.is_symlink():
        raise OSError(f"Unsupported output symbolic link: {target}")
    root = addon_root(source / ADDON_XML)
    version = root.get("version")

    with tempfile.TemporaryDirectory(prefix=f".{target.name}-", dir=target.parent) as directory:
        temporary = Path(directory)
        addon_directory = temporary / ADDON_ID
        addon_directory.mkdir()
        package = addon_directory / f"{ADDON_ID}-{version}.zip"
        shutil.copy2(source / ADDON_XML, addon_directory / ADDON_XML)
        copy_assets(source, addon_directory, root)
        write_package(source, package)

        addons = ET.Element("addons")
        addons.append(root)
        ET.indent(addons, space="  ")
        manifest_bytes = ET.tostring(addons, encoding="utf-8", xml_declaration=True)
        (temporary / "addons.xml").write_bytes(manifest_bytes)
        digest = hashlib.md5(manifest_bytes).hexdigest()
        (temporary / "addons.xml.md5").write_text(digest, encoding="utf-8")

        if target.exists():
            shutil.rmtree(target)
        temporary.replace(target)

    print(f"Built {ADDON_ID} {version} in {target}")


# --- git helpers -----------------------------------------------------------


def git(repo, *args, strip=True):
    result = subprocess.run(["git", "-C", str(repo), *args], encoding="utf-8", capture_output=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)}: {detail}")
    return result.stdout.strip() if strip else result.stdout


def git_text(repo, spec):
    """Return `repo`'s content at `spec` (rev:path) as UTF-8 text."""
    return git(repo, "show", spec, strip=False)


def addon_version_at(repo, rev):
    """Version of `repo`'s addon.xml at commit `rev`, read straight from git."""
    spec = f"{rev}:{ADDON_XML}"
    return validate_addon(ET.fromstring(git_text(repo, spec)), spec).get("version")


def ref_exists(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", ref]).returncode == 0


def worktree(repo, path, commit):
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--detach", str(path), commit)
    return path


# --- orchestration ---------------------------------------------------------


def published_version(channel):
    addon = Path(channel, ADDON_ID, ADDON_XML)
    return addon_root(addon).get("version") if addon.is_file() else ""


def generated_branch_state(fork_dir, branch):
    remote_ref = f"refs/remotes/origin/{branch}"
    if not ref_exists(fork_dir, remote_ref):
        return "", "", ""
    commit, parent = git(fork_dir, "rev-parse", remote_ref, f"{remote_ref}^").splitlines()
    return commit, parent, addon_version_at(fork_dir, remote_ref)


def build_patch(fork_dir, stable_branch, scratch):
    """Fork commit with addon.xml/strings.po reset to pre-fork state, so it
    cherry-picks without conflicts. Returns (commit, appended_po)."""
    custom_ref = f"refs/remotes/origin/{stable_branch}"
    parent_ref = f"{custom_ref}^"
    fork_before_po = git_text(fork_dir, f"{parent_ref}:{PO_PATH}")
    fork_after_po = git_text(fork_dir, f"{custom_ref}:{PO_PATH}")
    if not fork_after_po.startswith(fork_before_po):
        raise ValueError("Fork strings.po changes are not append-only")
    po_addition = fork_after_po[len(fork_before_po):].strip("\n")

    patch_tree = worktree(fork_dir, scratch / "patch", custom_ref)
    git(patch_tree, "restore", "--source", parent_ref, "--staged", "--worktree", "--", ADDON_XML, PO_PATH)
    git(patch_tree, "commit", "--amend", "--no-edit")
    return git(patch_tree, "rev-parse", "HEAD"), po_addition


def resolve_target_commit(fork_dir, channel):
    """Upstream commit the channel tracks."""
    if channel == "alpha":
        return git(fork_dir, "rev-parse", UPSTREAM_REF)

    source_version = addon_version_at(fork_dir, UPSTREAM_REF)
    version_pattern = rf'<addon[[:space:]][^>]*[[:space:]]version[[:space:]]*=[[:space:]]*"{re.escape(source_version)}"'
    target = git(fork_dir, "log", "-n", "1", "--format=%H", "--pickaxe-regex", "-S", version_pattern, UPSTREAM_REF, "--", ADDON_XML)
    if not target:
        raise RuntimeError(f"No upstream commit bumps version to {source_version}")
    return target


def process_generated(fork_dir, stable_branch, scratch_dir, channel, force):
    scratch = scratch_dir / channel
    label = channel.capitalize()
    branch = f"{stable_branch}-{channel}"
    suffix = "-rc" if channel == "alpha" else ""

    if not ref_exists(fork_dir, UPSTREAM_REF):
        git(fork_dir, "fetch", "--no-tags", UPSTREAM_REPO, f"{UPSTREAM_BRANCH}:{UPSTREAM_REF}")
    target_commit = resolve_target_commit(fork_dir, channel)
    current_commit, current_parent, current_version = generated_branch_state(fork_dir, branch)

    published = published_version(channel)

    if not force and current_parent == target_commit and (not suffix or current_version.endswith(suffix)):
        if published == current_version:
            print(f"{label} is up to date with upstream {target_commit}.")
        else:
            # Branch is current but its package never reached the repository (a previous publish failed).
            # Rebuild it without re-pushing the branch.
            recovery = worktree(fork_dir, scratch / "republish", current_commit)
            build_repository(recovery, channel)
            print(f"Recovered the unpublished {label} package.")
        return

    source_version = addon_version_at(fork_dir, target_commit)
    target_version = next_version(source_version, current_version, published, suffix=suffix)
    patch_commit, po_addition = build_patch(fork_dir, stable_branch, scratch)

    tree = worktree(fork_dir, scratch / "skin", target_commit)
    git(tree, "cherry-pick", patch_commit)
    update_addon(tree / ADDON_XML, target_version)
    strings = tree / PO_PATH
    merged = merge_appended_po(strings.read_text(encoding="utf-8"), po_addition)
    strings.write_text(merged, encoding="utf-8")
    git(tree, "add", "--", ADDON_XML, PO_PATH)
    git(tree, "commit", "--amend", "--no-edit")

    lease = f"--force-with-lease=refs/heads/{branch}:{current_commit}"
    git(tree, "push", lease, "origin", f"HEAD:refs/heads/{branch}")
    build_repository(tree, channel)


def process_stable(fork_dir, stable_branch, scratch_dir, force):
    channel = "stable"
    published = published_version(channel)

    custom_ref = f"refs/remotes/origin/{stable_branch}"
    source_version = addon_version_at(fork_dir, custom_ref)

    # Stable rebuilds when forced, its base changes, or the suffix is missing.
    if not force and published.endswith("-stable") and split_version(published)[0] == fork_base_version(source_version):
        print(f"Stable is up to date with {stable_branch}.")
        return

    version = next_version(source_version, published, suffix="-stable")
    tree = worktree(fork_dir, scratch_dir / channel / "skin", custom_ref)
    update_addon(tree / ADDON_XML, version)
    build_repository(tree, channel)


def main():
    parser = argparse.ArgumentParser(description="Build one Arctic Fuse Kodi channel")
    parser.add_argument("channel", choices=("alpha", "release", "stable"))
    parser.add_argument("--fork-dir", type=Path, required=True)
    parser.add_argument("--stable-branch", required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.channel == "stable":
        process_stable(args.fork_dir, args.stable_branch, args.scratch_dir, args.force)
    else:
        process_generated(args.fork_dir, args.stable_branch, args.scratch_dir, args.channel, args.force)


if __name__ == "__main__":
    main()

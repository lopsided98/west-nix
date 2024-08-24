import hashlib
import json
import os
import subprocess
import types
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent, indent

from west.commands import WestCommand, extension_commands
from west.manifest import Manifest
from west.util import west_dir


@dataclass
class LinkPath:
    path: Path
    src: str
    is_dir: bool


class Nix(WestCommand):
    def __init__(self):
        super().__init__(
            "nix",  # gets stored as self.name
            "generate Nix code for fetching manifest sources and blobs",  # self.help
            # self.description:
            dedent(
                """
                Generate a west.nix that produces a Nix derivation containing
                all manifest sources and binary blobs, linked in the proper
                locations."""
            ),
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name, help=self.help, description=self.description
        )

        return parser

    def do_run(self, args, unknown_args):
        manifest: Manifest = self.manifest

        # Path to the manifest YAML
        manifest_path = Path(manifest.abspath)
        # Directory containing the manifest YAML
        manifest_dir = manifest_path.parent
        west_nix_path = manifest_dir / "west.nix"
        cache_path = Path(west_dir()) / "west-nix-cache.json"
        top_dir = Path(manifest.topdir)
        top_dir_relative_to_manifest_dir = Path(os.path.relpath(top_dir, manifest_dir))

        zephyr_base = os.environ.get("ZEPHYR_BASE")
        if zephyr_base is not None:
            zephyr_base = Path(zephyr_base)

        blobs = []
        # Locate and call the Zephyr "blobs" extension command. This is probably
        # a bit brittle.
        zephyr_cmds = extension_commands(self.config, manifest).get("zephyr")
        if zephyr_cmds is not None:
            self.dbg("found Zephyr extension commands")
            blob_cmd_spec = next(
                (cmd for cmd in zephyr_cmds if cmd.name == "blobs"), None
            )
            if blob_cmd_spec is not None:
                self.dbg("found Zephyr blobs commands")
                blob_cmd = blob_cmd_spec.factory()
                blob_cmd.topdir = self.topdir
                blob_cmd.manifest = self.manifest
                blob_cmd.config = self.config
                blobs = blob_cmd.get_blobs(types.SimpleNamespace(modules=[]))

        try:
            with open(cache_path) as cache_file:
                cache = json.load(cache_file)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
        project_hashes = cache.setdefault("project_hashes", {})

        # Only projects that are still included in the manifest are written back
        # to the cache
        new_cache = {}
        new_project_hashes = new_cache.setdefault("project_hashes", {})

        zephyr_modules = []
        paths = []
        for project in manifest.projects:
            if not self.manifest.is_active(project):
                self.dbg(f"{project.name}: skipping inactive project")
                continue

            if project.url:
                # Hash project data into a cache key
                cache_key = hashlib.sha256()
                cache_key.update(project.url.encode("utf-8"))
                cache_key.update(project.revision.encode("utf-8"))
                cache_key.update(b"manifest-rev")
                cache_key = cache_key.hexdigest()

                # Find the source hash using the cache or nix-prefetch-git
                hash_str = project_hashes.get(cache_key, {}).get("hash")
                if hash_str is None:
                    prefetch = self._nix_prefetch_git(project.url, project.revision)
                    hash_str = prefetch["hash"]
                new_project_hashes[cache_key] = {
                    # These attributes are just for informational purposes
                    "url": project.url,
                    "rev": project.revision,
                    "hash": hash_str,
                }

                paths.append(
                    LinkPath(
                        path=Path(project.path),
                        src=dedent(
                            f"""
                        fetchgit {{
                            url = "{project.url}";
                            rev = "{project.revision}";
                            branchName = "manifest-rev";
                            hash = "{hash_str}";
                        }}"""
                        ),
                        is_dir=True,
                    )
                )
            else:
                paths.append(
                    LinkPath(
                        path=Path(project.path),
                        src=f'"${{{top_dir_relative_to_manifest_dir / project.path}}}"',
                        is_dir=True,
                    )
                )

            if (Path(project.path) / "zephyr" / "module.yml").exists():
                zephyr_modules.append(project.path)

        for blob in blobs:
            paths.append(
                LinkPath(
                    path=Path(os.path.relpath(blob["abspath"], top_dir)),
                    src=dedent(
                        f"""
                    fetchurl {{
                        url = "{blob["url"]}";
                        hash = "sha256:{blob["sha256"]}";
                    }}"""
                    ),
                    is_dir=False,
                )
            )

        with open(west_nix_path, "w") as west_nix:
            print(
                """
{ lib, runCommand, lndir, fetchgit, fetchurl }:

runCommand "west-workspace" {
nativeBuildInputs = [ lndir ];
} ''""",
                file=west_nix,
            )

            for path in paths:
                src = indent(f"${{lib.escapeShellArg ({path.src})}}", "        ")
                if path.is_dir:
                    print(
                        f"""
    mkdir -p "$out"/'{path.path}'
    lndir -silent \\
{src} \\
        "$out"/'{path.path}'""",
                        file=west_nix,
                    )
                else:
                    print(
                        f"""
    mkdir -p "$out"/'{path.path.parent}'
    ln -s \\
{src} \\
        "$out"/'{path.path}'""",
                        file=west_nix,
                    )

            if zephyr_base is not None:
                zephyr_base_placeholder = (
                    '${placeholder "out"}' / zephyr_base.relative_to(top_dir)
                )
                zephyr_modules_placeholder = (
                    f'${{placeholder "out"}}/{m}' for m in zephyr_modules
                )
                print(
                    f"""
    cat << EOF > "$out/.zephyr-env"
    export ZEPHYR_BASE=${{lib.escapeShellArg "{zephyr_base_placeholder}"}}
    export ZEPHYR_MODULES=${{lib.escapeShellArg "{";".join(zephyr_modules_placeholder)}"}}
    EOF
''""",
                    file=west_nix,
                )

            with open(cache_path, "w") as cache_file:
                json.dump(new_cache, cache_file, indent=2)

    def _nix_prefetch_git(self, url, rev):
        result = subprocess.run(
            [
                "nix-prefetch-git",
                "--url",
                url,
                "--rev",
                rev,
                "--branch-name",
                "manifest-rev",
                "--quiet",
            ],
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

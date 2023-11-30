import hashlib
import json
import os
import subprocess
from pathlib import Path
from textwrap import dedent
from typing import Sequence

from west import log
from west.commands import WestCommand
from west.manifest import Manifest, Project
from west.util import west_dir


class Nix(WestCommand):
    def __init__(self):
        super().__init__(
            "nix",  # gets stored as self.name
            "generate Nix code for fetching manifest sources",  # self.help
            # self.description:
            dedent(
                """
                Generate a west.nix that generates a reproducible West workspace
                for the manifest, stored in the Nix store."""
            ),
        )
        self._project_hashes = {}

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)

        return parser

    def do_run(self, args, unknown_args):
        manifest: Manifest = self.manifest

        manifest_path = Path(manifest.path)
        manifest_dir = manifest_path.parent
        west_nix_path = manifest_dir / "west.nix"
        cache_path = Path(west_dir()) / "west-nix-cache.json"
        topdir = Path(manifest.topdir)
        topdir_relative_to_manifest = Path(os.path.relpath(topdir, manifest_dir))

        try:
            with open(cache_path) as cache_file:
                cache = json.load(cache_file)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
        project_hashes = cache.setdefault("project_hashes", {})

        with open(west_nix_path, "w") as west_nix:
            print(
                '{ lib, linkFarm, fetchgit, writeText }: (linkFarm "west-workspace" [',
                file=west_nix,
            )
            for project in manifest.projects:
                if not self.manifest.is_active(project):
                    self.dbg(f'{project.name}: skipping inactive project')
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
                        project_hashes[cache_key] = {
                            # These attributes are just for informational purposes
                            "url": project.url,
                            "rev": project.revision,
                            "hash": hash_str,
                        }

                    print(
                        dedent(
                            f"""
                            {{
                                name = "{project.path}";
                                path = fetchgit {{
                                    url = "{project.url}";
                                    rev = "{project.revision}";
                                    branchName = "manifest-rev";
                                    hash = "{hash_str}";
                                    leaveDotGit = true;
                                }};
                            }}"""
                        ),
                        file=west_nix,
                    )
                else:
                    print(
                        dedent(
                            f"""
                            {{
                                name = "{project.path}";
                                path = "${{{topdir_relative_to_manifest / project.path}}}";
                            }}"""
                        ),
                        file=west_nix,
                    )
            # Create West config. Must not be a symlink to the store because
            # West modifies it during the build.
            print(
                dedent(
                    f"""
                    ]).overrideAttrs ({{ buildCommand, ... }}: {{
                        buildCommand = buildCommand + ''
                            mkdir -p .west
                            cat << EOF > .west/config
                            [manifest]
                            path = {manifest_dir.relative_to(topdir)}
                            file = {manifest_path.relative_to(manifest_dir)}
                            EOF
                        '';
                    }})"""
                ),
                file=west_nix,
            )

            with open(cache_path, "w") as cache_file:
                json.dump(cache, cache_file)

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
                "--leave-dotGit",
                "--quiet",
            ],
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

import json
import os
import subprocess
from textwrap import dedent
from typing import Sequence
import hashlib

from west import log
from west.commands import WestCommand
from west.manifest import Manifest, Project


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

        manifest_dir = os.path.dirname(manifest.path)
        west_nix_path = os.path.join(manifest_dir, "west.nix")
        cache_path = os.path.join(manifest_dir, "west-nix-cache.json")
        topdir_relative_to_manifest = os.path.relpath(manifest.topdir, manifest_dir)

        try:
            with open(cache_path) as cache_file:
                cache = json.load(cache_file)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
        project_hashes = cache.setdefault("project_hashes", {})

        with open(west_nix_path, "w") as west_nix:
            print('{ lib, fetchgit, linkFarm }: linkFarm "west-src" [', file=west_nix)
            for project in manifest.projects:
                if project.url:
                    # Hash project data into a cache key
                    cache_key = hashlib.sha256()
                    cache_key.update(project.url.encode("utf-8"))
                    cache_key.update(project.revision.encode("utf-8"))
                    cache_key.update(b"manifest-rev")
                    cache_key = cache_key.hexdigest()

                    # Find the source SHA256 using the cache or nix-prefetch-git
                    sha256 = project_hashes.get(cache_key, {}).get("sha256")
                    if sha256 is None:
                        prefetch = self._nix_prefetch_git(project.url, project.revision)
                        sha256 = prefetch["sha256"]
                        project_hashes[cache_key] = {
                            # These attributes are just for informational purposes
                            "url": project.url,
                            "rev": project.revision,
                            "sha256": sha256,
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
                                    sha256 = "{sha256}";
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
                                path = "${{{os.path.join(topdir_relative_to_manifest, project.path)}}}";
                            }}"""
                        ),
                        file=west_nix,
                    )
            print("]", file=west_nix)

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

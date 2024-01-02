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

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name, help=self.help, description=self.description
        )

        return parser

    def do_run(self, args, unknown_args):
        manifest: Manifest = self.manifest

        # Path to the manifest YAML
        manifest_path = Path(manifest.abspath)
        # Repository directory containing the manifest
        manifest_repo = Path(manifest.repo_abspath)
        # Directory containing the manifest YAML. Normally the same as
        # manifest_repo, but may be a subdirectory.
        manifest_dir = manifest_path.parent
        west_nix_path = manifest_dir / "west.nix"
        cache_path = Path(west_dir()) / "west-nix-cache.json"
        top_dir = Path(manifest.topdir)
        top_dir_relative_to_manifest_dir = Path(os.path.relpath(top_dir, manifest_dir))

        zephyr_base = os.environ.get("ZEPHYR_BASE")
        if zephyr_base is not None:
            zephyr_base = Path(zephyr_base)

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

        with open(west_nix_path, "w") as west_nix:
            print(
                dedent(
                    """
                    { lib, runCommand, symlinkJoin, fetchgit }: let
                      linkPath = { link, path }: runCommand "west-link" {} ''
                        outLink="$out"/${lib.escapeShellArg link}
                        mkdir -p "$(dirname "$outLink")"
                        ln -s ${lib.escapeShellArg path} "$outLink"
                      '';
                    in symlinkJoin {
                      name = "west-workspace";
                      paths = ["""
                ),
                file=west_nix,
            )
            zephyr_modules = []
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

                    print(
                        dedent(
                            f"""
                            (linkPath {{
                                link = "{project.path}";
                                path = fetchgit {{
                                    url = "{project.url}";
                                    rev = "{project.revision}";
                                    branchName = "manifest-rev";
                                    hash = "{hash_str}";
                                }};
                            }})"""
                        ),
                        file=west_nix,
                    )
                else:
                    print(
                        dedent(
                            f"""
                            (linkPath {{
                                link = "{project.path}";
                                path = "${{{top_dir_relative_to_manifest_dir / project.path}}}";
                            }})"""
                        ),
                        file=west_nix,
                    )

                if (Path(project.path) / "zephyr" / "module.yml").exists():
                    zephyr_modules.append(project.path)

            if zephyr_base is not None:
                zephyr_base_placeholder = (
                    '${placeholder "out"}' / zephyr_base.relative_to(top_dir)
                )
                zephyr_modules_placeholder = (
                    f'${{placeholder "out"}}/{m}' for m in zephyr_modules
                )
                print(
                    dedent(
                        f"""
                          ];
                          postBuild = ''
                            cat << EOF > "$out/.zephyr-env"
                              export ZEPHYR_BASE=${{lib.escapeShellArg "{zephyr_base_placeholder}"}}
                              export ZEPHYR_MODULES=${{lib.escapeShellArg "{";".join(zephyr_modules_placeholder)}"}}
                            EOF
                          '';
                        }}"""
                    ),
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

# Copyright (C) 2022 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import collections
import dataclasses
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Iterable
import xml.dom.minidom
import xml.parsers.expat

_FAKE_KERNEL_VERSION = "99.99.99"
_FIXED_WORKSPACE_STATUS_FILE = "workspace_status.json"


@dataclasses.dataclass
class PathCollectible(object):
    """Represents a path and the result of an asynchronous task."""
    path: pathlib.Path

    def collect(self) -> str:
        raise NotImplementedError


@dataclasses.dataclass
class PathPopen(PathCollectible):
    """Consists of a path and the result of a subprocess."""
    popen: subprocess.Popen
    result: str | None = None

    def collect(self) -> str:
        if self.result is not None:
            return self.result
        self.result = collect(self.popen)
        return self.result


@dataclasses.dataclass
class PresetResult(PathCollectible):
    """Consists of a path and a pre-defined result."""
    result: str

    def collect(self) -> str:
        return self.result


@dataclasses.dataclass(kw_only=True)
class LocalversionResult(PathPopen):
    """Consists of results of localversion."""
    suffix: str

    def collect(self) -> str:
        return super().collect() + self.suffix


@dataclasses.dataclass
class GitProgram:
    """Helper class to parse --git option."""

    strategy: str

    def __post_init__(self):
        if self.strategy == "always":
            ret = shutil.which("git")
            if not ret:
                self._path = None
                return
            self._path = pathlib.Path(ret)
            return

        if self.strategy == "never":
            self._path = None
            return

        logging.error("--git must be one of always or never")

    def __bool__(self):
        self._check_valid()
        return self._path is not None

    def __str__(self):
        self._check_valid()
        if self._path is None:
            raise ValueError("Must check __bool__ before calling __str__")
        return str(self._path)

    def _check_valid(self):
        if self.strategy == "always" and self._path is None:
            logging.error("git not found, but --git=always")
            sys.exit(1)


def load_attribute_from_json[T](json_file: pathlib.Path, attr_name: str, attr_type: type[T]) \
        -> T | None:
    """Returns value of attribute of given type from json file."""
    if json_file.is_file():
        json_file_content = json.loads(json_file.read_text())
        if value := json_file_content.get(attr_name):
            if not isinstance(value, attr_type):
                logging.error("'%s' in %s is not of type %s: %s",
                              attr_name, json_file, attr_type, value)
                sys.exit(1)
            return value
    return None


def get_localversion_suffix() -> str:
    if (build_number := os.environ.get("BUILD_NUMBER")):
        return "-ab" + build_number
    return ""


def get_localversion_from_git(
        git: GitProgram,
        project: pathlib.Path,
        fallback_sha: str | None) -> PathCollectible | None:
    """Calculate localversion without calling setlocalversion script.

    Args:
      git: path to the git executable
      project: relative path to the project
      fallback_sha: if there is no git or the directory does not exist,
        use the given sha.
    Return:
      A PathCollectible object that resolves to the result, or None if bin or
      project does not exist.
    """

    if not project.is_dir() or not git:
        if not fallback_sha:
            return None
        return PresetResult(
            project,
            "-g" + fallback_sha[:12] + get_localversion_suffix())

    # Note: To ensure hermeticity as much as possible, clear PATH unless
    # executing git commands.
    script = f"""
        GIT_OLD_PATH="$PATH"
        PATH=
        if head=$(PATH=$GIT_OLD_PATH {git} rev-parse --verify --short=12 HEAD 2>/dev/null); then
            echo -n -g"$head"
        fi
        if (
            export PATH=$GIT_OLD_PATH
            {git} --no-optional-locks status -uno --porcelain 2>/dev/null ||
            {git} diff-index --name-only HEAD
        ) | read placeholder; then
            echo -n -dirty
        fi
    """
    popen = subprocess.Popen(script, shell=True, text=True,
                             stdout=subprocess.PIPE, cwd=project)
    return LocalversionResult(
        path=project,
        popen=popen,
        suffix=get_localversion_suffix(),
    )


def _find_repo(curdir: pathlib.Path) -> pathlib.Path | None:
    """Find repo installation."""
    while curdir.parent != curdir:  # is not root
        maybe_dot_repo = curdir / ".repo"
        if maybe_dot_repo.is_dir():
            return curdir
        curdir = curdir.parent
    return None


def list_project_shas() -> dict[pathlib.Path, str | None]:
    """Lists projects in the repository.

    Returns:
        a list of Git projects relative to CWD.
    """
    repo_root_s, repo_manifest = os.environ.get(
        "KLEAF_REPO_MANIFEST", ":").split(":")
    if repo_root_s:
        repo_root = pathlib.Path(repo_root_s).resolve()
    else:
        repo_root = _find_repo(pathlib.Path(".").resolve())

    if not repo_root:
        logging.warning(
            "Unable to determine repo root. Please specify --repo_manifest.")
        return {}

    if repo_manifest:
        with open(repo_manifest) as repo_manifest_file:
            return parse_repo_manifest(repo_root, repo_manifest_file.read())

    try:
        output = subprocess.check_output(["repo", "manifest", "-r"], text=True)
        return parse_repo_manifest(repo_root, output)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logging.warning("Unable to execute repo manifest -r: %s", e)
        return {}


def parse_repo_manifest(repo_root: pathlib.Path, manifest: str) \
        -> dict[pathlib.Path, str | None]:
    """Parses a repo manifest file.

    Returns:
        key: paths to all projects in the repository; value: git SHA
    """
    kleaf_repo_dir = pathlib.Path(".").resolve()
    try:
        dom = xml.dom.minidom.parseString(manifest)
    except xml.parsers.expat.ExpatError as e:
        logging.error("Unable to parse repo manifest: %s", e)
        return {}
    if not dom.documentElement:
        logging.error(
            "Unable to parse repo manifest. DOM has no root element!")
        return {}
    projects = dom.documentElement.getElementsByTagName("project")
    ret = dict[pathlib.Path, str | None]()
    for project in projects:
        # https://gerrit.googlesource.com/git-repo/+/master/docs/manifest-format.md#element-project
        path_below_repo = pathlib.Path(project.getAttribute("path") or
                                       project.getAttribute("name"))
        candidate_paths = [path_below_repo]
        for linkfile in project.getElementsByTagName("linkfile"):
            if linkfile.getAttribute("src") == ".":
                dest = linkfile.getAttribute("dest")
                if dest:
                    candidate_paths.append(pathlib.Path(dest))
        matched = False
        for candidate in candidate_paths:
            realpath = repo_root / candidate
            if realpath.is_relative_to(kleaf_repo_dir):
                ret[realpath.relative_to(kleaf_repo_dir)] = \
                    project.getAttribute("revision") or None
                matched = True
        if not matched:
            logging.warning("Skipping project %s because it is not below %s",
                            repo_root / path_below_repo, kleaf_repo_dir)
    return ret


def collect(popen_obj: subprocess.Popen) -> str:
    """Collect the result of a Popen object.

    Terminates the program if return code is non-zero.

    Return:
      stdout of the subprocess.
    """
    stdout, _ = popen_obj.communicate()
    if popen_obj.returncode != 0:
        logging.error("return code is %d", popen_obj.returncode)
        sys.exit(1)
    return stdout.strip()


@dataclasses.dataclass
class Stamp(object):
    git_program: GitProgram

    def __post_init__(self):

        self.ignore_missing_projects = os.environ.get(
            "KLEAF_IGNORE_MISSING_PROJECTS") == "true"

        self.bzlmod_mapping = self._init_bzlmod_mapping()

        self.fallback_shas = list_project_shas()
        self.projects = list(self.fallback_shas.keys())
        extra_git_project_env_var = os.environ.get("KLEAF_EXTRA_GIT_PROJECTS")
        if extra_git_project_env_var:
            self.projects.extend(pathlib.Path(value) for value in
                                 extra_git_project_env_var.split(":"))

        self.init_for_dot_source_date_epoch_dir()

    def _init_bzlmod_mapping(self) -> dict[pathlib.Path, set[pathlib.Path]]:
        """Returns value for self.bzlmod_mapping.

        Key: source path relative to the workspace (e.g. external/kleaf)
        Value: set of source paths relative to the execroot.
            (e.g. {external/kleaf~})
        """
        output_base_s = os.environ.get("KLEAF_OUTPUT_BASE")
        if not output_base_s:
            return {}
        output_base = pathlib.Path(output_base_s)

        # Implementation note: We use absolute() instead of resolve() to handle
        # the edge cases for symlinks. That is, for symlinks, we use the
        # absolute path, not the realpath.

        abs_workspace = pathlib.Path().absolute()
        ret = collections.defaultdict(set)
        for child in (output_base / "external").iterdir():
            if not child.is_dir():
                # Skip marker files
                continue
            if not child.is_symlink():
                # Skip this child. This is e.g. a `new_local_repository`,
                # so it isn't a symlink below the output_base.
                continue
            # For repositories that aren't present, Bazel did not fetch it,
            # indicating that it is not a dependency of the currently requested
            # target. Hence there is no point reading Git metadata from it.

            # For the repositories that are present, create workspace_rel ->
            # external/<canonical name> mapping.
            # We create an entry for EACH workspace_rel along the symlink chain
            # until we hit the destination. This is to cover project symlinks
            # in the source tree.
            execroot_rel = child.relative_to(output_base)
            while child.is_symlink():
                abs_link_dest = child.readlink().absolute()
                if abs_link_dest.is_relative_to(abs_workspace):
                    ret[abs_link_dest.relative_to(abs_workspace)].add(
                        execroot_rel)
                child = child.readlink()
        return ret

    def init_for_dot_source_date_epoch_dir(self) -> None:
        kleaf_repo_dir = pathlib.Path(".").resolve()
        self.kernel_dir = pathlib.Path(".source_date_epoch_dir").resolve()
        if not self.kernel_dir.is_dir():
            self.kernel_dir = None
        if self.kernel_dir:
            if self.kernel_dir.is_relative_to(kleaf_repo_dir):
                self.kernel_rel = self.kernel_dir.relative_to(kleaf_repo_dir)
            elif os.path.islink(".source_date_epoch_dir"):
                link_target = kleaf_repo_dir / os.readlink(".source_date_epoch_dir")
                if link_target.is_dir() and link_target.is_relative_to(kleaf_repo_dir):
                    self.kernel_rel = link_target.relative_to(kleaf_repo_dir)
                else:
                    self.kernel_dir = None
            else:
                self.kernel_dir = None

    def main(self) -> int:
        scmversion_map = self.get_localversion_all()

        source_date_epoch_map = self.async_get_source_date_epoch_all()

        scmversion_result_map = self.collect_map(scmversion_map)

        source_date_epoch_result_map = self.collect_map(source_date_epoch_map)

        self.print_result(
            scmversion_result_map=scmversion_result_map,
            source_date_epoch_result_map=source_date_epoch_result_map,
        )
        return 0

    def get_localversion_all(self) -> dict[pathlib.Path, PathCollectible]:
        all_projects: set[pathlib.Path] = set()
        if self.kernel_dir:
            all_projects.add(self.kernel_rel)
        all_projects |= set(self.projects)

        filtered_projects: Iterable[pathlib.Path] = all_projects
        if self.ignore_missing_projects:
            filtered_projects = filter(pathlib.Path.is_dir, filtered_projects)

        scmversion_map = {}
        for project in filtered_projects:
            if not project.is_dir():
                logging.error(
                    "Project %s in repo manifest does not exist on disk.",
                    project)
                sys.exit(1)

            path_popen = self.get_localversion(project)
            if path_popen:
                for execroot_rel in self.get_execroot_rel_paths(project):
                    scmversion_map[execroot_rel] = path_popen

        return scmversion_map

    def get_localversion(self, project: pathlib.Path) -> PathCollectible | None:
        if (scmversion := load_attribute_from_json(
            project / _FIXED_WORKSPACE_STATUS_FILE, "SCMVERSION", str
        )) is not None:
            return PresetResult(project, scmversion)

        return get_localversion_from_git(self.git_program, project,
                                         self.fallback_shas.get(project))

    def async_get_source_date_epoch_all(self) \
            -> dict[pathlib.Path, PathCollectible]:

        all_projects: set[pathlib.Path] = set()
        if self.kernel_dir:
            all_projects.add(self.kernel_rel)
        all_projects |= set(self.projects)

        filtered_projects: Iterable[pathlib.Path] = all_projects
        if self.ignore_missing_projects:
            filtered_projects = filter(pathlib.Path.is_dir, filtered_projects)

        ret = {}
        for proj in filtered_projects:
            for execroot_rel in self.get_execroot_rel_paths(proj):
                ret[execroot_rel] = self.async_get_source_date_epoch(proj)
        return ret

    def async_get_source_date_epoch(self, rel_path: pathlib.Path) -> PathCollectible:
        env_val = os.environ.get("SOURCE_DATE_EPOCH")
        if env_val:
            return PresetResult(rel_path, env_val)

        if (source_date_epoch := load_attribute_from_json(
            rel_path / _FIXED_WORKSPACE_STATUS_FILE, "SOURCE_DATE_EPOCH", int
        )) is not None:
            return PresetResult(rel_path, f"{source_date_epoch}")

        # TODO - b/454390534: implement SOURCE_DATE_EPOCH when no git
        if self.git_program:
            args = [
                str(self.git_program), "-C",
                str(rel_path.resolve()), "log", "-1", "--pretty=%ct"
            ]
            popen = subprocess.Popen(args, text=True, stdout=subprocess.PIPE)
            return PathPopen(rel_path, popen)
        return PresetResult(rel_path, "0")

    def get_execroot_rel_paths(self, project: pathlib.Path) -> \
            Iterable[pathlib.Path]:
        """Returns all possible paths of the project within the execroot.

        Args:
            project: path to the Git project, relative to the workspace."""
        candidates = [
            (workspace_rel_path, canonical)
            for workspace_rel_path, canonical in self.bzlmod_mapping.items()
            if project.is_relative_to(workspace_rel_path)]
        if not candidates:
            if project.parts[0] == "external":
                # For --nosource_date_epoch_fix, keep the external/ path so the
                # hack in build_utils.sh works
                if "KLEAF_OUTPUT_BASE" not in os.environ and \
                        len(project.parts) >= 2 and project.parts[1] == "kleaf":
                    return (project,)

                # These Git projects aren't available in the execroot because
                # `//external` is not a valid package under the root repository,
                # and these Git projects may be one of the following:
                # - It is not a Bazel module or Bazel external repository
                # - It is a Bazel module or Bazel external repository, but not
                #   fetched.
                # Hence, we should skip them to not provide false information.
                return ()

            # Regular source projects (packages in the root repository)
            # show up directly in the execroot.
            # Example: private/<manufacturer_name>/<device_name>
            return (project,)

        # Get the tuple in candidates that has the most specific
        #   workspace_rel_path. This is to handle potential Git submodules.
        sorted_candidates = sorted(candidates,
                                   key=lambda x: len(x[0].parts), reverse=True)
        workspace_rel_path, canonical_paths = sorted_candidates[0]

        # Example:
        #  project: external/kleaf/external/toybox
        #  workspace_rel_path: external/kleaf
        #  canonical_paths: {external/kleaf~, external/kleaf2~}
        # Return: {external/kleaf~/external/toybox,
        #          external/kleaf2~/external/toybox}
        return {canonical / (project.relative_to(workspace_rel_path))
                for canonical in canonical_paths}

    def collect_map(
        self,
        legacy_map: dict[pathlib.Path, PathCollectible],
    ) -> dict[pathlib.Path, str]:
        return {
            path: path_popen.collect()
            for path, path_popen in legacy_map.items()
        }

    def print_result(
        self,
        scmversion_result_map: dict[pathlib.Path, str],
        source_date_epoch_result_map: dict[pathlib.Path, str],
    ) -> None:
        stable_source_date_epochs = json.dumps({
            str(key): value for key, value in source_date_epoch_result_map.items()
        }, sort_keys=True)
        print("STABLE_SOURCE_DATE_EPOCHS", stable_source_date_epochs)

        # If the list is empty, this prints "STABLE_SCMVERSIONS", and is
        # filtered by Bazel.
        stable_scmversions = json.dumps({
            str(key): value for key, value in scmversion_result_map.items()
        }, sort_keys=True)
        print("STABLE_SCMVERSIONS", stable_scmversions)


if __name__ == '__main__':
    logging.basicConfig(stream=sys.stderr,
                        level=logging.WARNING,
                        format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--git",
                        dest="git_program",
                        type=GitProgram,
                        default="always",
                        help="""Whether to execute `git`.
                            always: assume git exists on the host machine.
                            never: do not execute git.
                        """)

    sys.exit(Stamp(**vars(parser.parse_args())).main())

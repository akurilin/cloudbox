"""Select one configuration and two isolated local Terraform states."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cloudbox.common import ROOT, CloudboxError

ENVIRONMENT_NAMES = ("test", "prod")
DEFAULT_WORKSPACE = "default"
LOCAL_BACKEND = "local"
STATE_FILENAME = "terraform.tfstate"
LOCK_FILENAME = ".terraform.lock.hcl"
SOURCE_DIRECTORY = "source"
COMMAND_TIMEOUT_SECONDS = 3600
READ_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Environment:
    name: str

    def __post_init__(self):
        if self.name not in ENVIRONMENT_NAMES:
            raise CloudboxError("invalid_environment", "Select test or prod.")

    @property
    def main_root(self):
        return ROOT / "infra"

    @property
    def bootstrap_root(self):
        return self.main_root / "bootstrap"

    @property
    def roots(self):
        return self.bootstrap_root, self.main_root

    @property
    def input_path(self):
        return self.main_root / "environments" / f"{self.name}.tfvars.json"

    @property
    def key_path(self):
        return ROOT / f".env.{self.name}"

    def stage_directory(self, directory):
        directory = Path(directory).resolve()
        stages = {self.bootstrap_root: "bootstrap", self.main_root: "main"}
        if directory not in stages:
            raise CloudboxError(
                "invalid_terraform_root", "Use a Cloudbox Terraform root."
            )
        return ROOT / ".cloudbox" / "environments" / self.name / stages[directory]

    def state_path(self, directory):
        return self.stage_directory(directory) / STATE_FILENAME

    def data_dir(self, directory):
        return self.stage_directory(directory) / ".terraform"

    def execution_directory(self, directory):
        return self.stage_directory(directory) / SOURCE_DIRECTORY

    def prepare_directory(self, directory):
        # Shared source links keep Terraform away from any environment's implicit state.
        working = self.execution_directory(directory)
        working.mkdir(parents=True, exist_ok=True)
        implicit_state = working / STATE_FILENAME
        if implicit_state.exists() or implicit_state.is_symlink():
            raise CloudboxError(
                "unexpected_state",
                "Inspect the unexpected state in the Terraform source directory before init.",
            )
        source = Path(directory).resolve()
        files = {
            path.name: path
            for path in source.iterdir()
            if path.is_file()
            and (path.name.endswith((".tf", ".tf.json")) or path.name == LOCK_FILENAME)
        }
        for path in working.iterdir():
            if path.name in {
                "terraform.tfvars",
                "terraform.tfvars.json",
            } or path.name.endswith((".auto.tfvars", ".auto.tfvars.json")):
                raise CloudboxError(
                    "unexpected_config",
                    "Remove automatic variable files from the generated Terraform directory.",
                )
            if path.name.endswith((".tf", ".tf.json")) and path.name not in files:
                if path.is_symlink() and path.readlink() == source / path.name:
                    path.unlink()
                else:
                    raise CloudboxError(
                        "unexpected_config",
                        "A generated Terraform directory contains an unknown file.",
                    )
        for name, target in files.items():
            self.link_source(working / name, target)
        module_directory = (
            working / "modules"
            if source == self.main_root
            else working.parent / "modules"
        )
        self.link_source(module_directory, self.main_root / "modules")

    @staticmethod
    def link_source(destination, source):
        if destination.is_symlink() and destination.readlink() == source:
            return
        if destination.exists() or destination.is_symlink():
            raise CloudboxError(
                "unexpected_config",
                "A generated Terraform path is not the expected source link.",
            )
        destination.symlink_to(source, target_is_directory=source.is_dir())

    def check_workspace(self, directory):
        workspace = self.data_dir(directory) / "environment"
        if os.environ.get("TF_WORKSPACE", DEFAULT_WORKSPACE) != DEFAULT_WORKSPACE:
            raise CloudboxError(
                "unsupported_state", "Cloudbox uses the default Terraform workspace."
            )
        if workspace.exists() and workspace.read_text().strip() != DEFAULT_WORKSPACE:
            raise CloudboxError(
                "unsupported_state", "Cloudbox uses the default Terraform workspace."
            )

    def check_backend(self, directory, *, required=True):
        # Refuse a reused backend before Terraform can read or move another state.
        self.check_workspace(directory)
        metadata = self.data_dir(directory) / STATE_FILENAME
        if not metadata.exists():
            if required:
                raise CloudboxError(
                    "terraform_not_initialized",
                    f"Initialize the {self.name} environment first.",
                )
            return
        try:
            backend = json.loads(metadata.read_bytes())["backend"]
            backend_path = backend["config"]["path"]
        except (ValueError, KeyError, TypeError) as error:
            raise CloudboxError(
                "unsupported_state", "Terraform backend metadata is invalid."
            ) from error
        if not isinstance(backend_path, str) or not backend_path:
            raise CloudboxError(
                "unsupported_state", "Terraform must use an explicit local state path."
            )
        selected = Path(backend_path)
        if not selected.is_absolute():
            selected = self.execution_directory(directory) / selected
        if (
            backend.get("type") != LOCAL_BACKEND
            or selected.resolve() != self.state_path(directory).resolve()
        ):
            raise CloudboxError(
                "state_mismatch",
                "The Terraform backend belongs to another environment.",
            )

    def command_environment(self, directory):
        self.check_workspace(directory)
        # Ignore shell overrides so commands cannot select another state or leak debug data.
        values = {
            key: value for key, value in os.environ.items() if not key.startswith("TF_")
        }
        values.update(
            {
                "TF_DATA_DIR": str(self.data_dir(directory)),
                "TF_WORKSPACE": DEFAULT_WORKSPACE,
                "TF_IN_AUTOMATION": "1",
                "TF_INPUT": "0",
            }
        )
        return values

    def terraform(self, directory, *arguments, capture=False, input_text=None):
        if not arguments:
            raise CloudboxError(
                "invalid_terraform_command", "Supply a Terraform command."
            )
        initializing = arguments[0] == "init"
        self.check_backend(directory, required=not initializing)
        selected = list(arguments)
        if initializing:
            self.prepare_directory(directory)
            selected.extend(
                (
                    f"-backend-config=path={self.state_path(directory)}",
                    "-lockfile=readonly",
                )
            )
        if selected[0] in {"plan", "console"}:
            # Use only the selected inputs; never load another environment's variable files.
            selected.append(f"-var-file={self.input_path}")
        try:
            result = subprocess.run(
                [
                    "terraform",
                    f"-chdir={self.execution_directory(directory)}",
                    *selected,
                ],
                cwd=ROOT,
                env=self.command_environment(directory),
                text=True,
                input=input_text,
                stdout=subprocess.PIPE if capture else sys.stderr,
                stderr=subprocess.PIPE if capture else sys.stderr,
                timeout=READ_TIMEOUT_SECONDS
                if arguments[0] in {"output", "console"}
                else COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise CloudboxError(
                "terraform_failed",
                "Terraform could not finish. Check the command and try again.",
            ) from error
        if result.returncode:
            raise CloudboxError(
                "terraform_failed",
                "Terraform stopped. Correct the error, then run this command again.",
            )
        if initializing:
            self.check_backend(directory)
        return result.stdout


def get_environment(name):
    return Environment(name)


def add_environment_argument(parser):
    parser.add_argument(
        "--env",
        choices=ENVIRONMENT_NAMES,
        required=True,
        help="Select isolated configuration and state.",
    )

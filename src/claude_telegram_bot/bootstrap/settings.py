import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class RuntimePaths:
    project_root: Path
    instance_name: Optional[str]
    env_path: Path
    state_file_path: Path


def parse_instance_name() -> Optional[str]:
    """Parse optional --instance flag without consuming app-level args."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--instance",
        type=str,
        default=None,
        help="Instance name (loads instances/<name>.env)",
    )
    args, _ = parser.parse_known_args()
    return args.instance


def build_runtime_paths(project_root: Path, instance_name: Optional[str]) -> RuntimePaths:
    """Resolve env and state paths for default or named instance mode."""
    if instance_name:
        env_path = project_root / "instances" / f"{instance_name}.env"
        state_file_path = project_root / "instances" / f"{instance_name}.state.yaml"
    else:
        env_path = project_root / ".env"
        state_file_path = project_root / ".bot-state.yaml"

    return RuntimePaths(
        project_root=project_root,
        instance_name=instance_name,
        env_path=env_path,
        state_file_path=state_file_path,
    )


def load_runtime_env(paths: RuntimePaths) -> None:
    """Load dotenv for selected runtime mode; hard fail if named env is missing."""
    if paths.instance_name and not paths.env_path.exists():
        print(f"Error: {paths.env_path} not found", file=sys.stderr)
        sys.exit(1)
    load_dotenv(paths.env_path, override=True)

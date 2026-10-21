"""Validate instance-local data paths before reading credentials or configuration."""
from __future__ import annotations

import os
from pathlib import Path


class InstancePathError(RuntimeError):
    """The data path cannot be established as local to this checkout."""


def resolve_instance_data_directory(base_dir: str | Path) -> Path:
    """Reject redirected roots and external/dangling links without reading files.

    This verifies filesystem paths at startup. Separate containers can still
    mount the same storage at the same path; deployment isolation must establish
    volume ownership independently.
    """
    try:
        application_root = Path(base_dir).resolve(strict=True)
        if not application_root.is_dir():
            raise InstancePathError("instance_root_unavailable")
        data_dir = application_root / "data"
        is_link = data_dir.is_symlink() or data_dir.is_junction()
        if not data_dir.exists() and not is_link:
            return data_dir
        if data_dir.resolve(strict=True) != data_dir or not data_dir.is_dir():
            raise InstancePathError("instance_data_redirected")

        def reject_walk_error(_error: OSError) -> None:
            raise InstancePathError("instance_path_unavailable")

        for current, directories, files in os.walk(
            data_dir, followlinks=False, onerror=reject_walk_error,
        ):
            linked_directories = set()
            for name in directories + files:
                candidate = Path(current) / name
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(data_dir):
                    raise InstancePathError("instance_path_mismatch")
                if candidate.is_symlink() or candidate.is_junction():
                    linked_directories.add(name)
            # os.walk does not follow symlinks; explicitly exclude Windows
            # junctions too. Their targets were checked above using metadata.
            directories[:] = [name for name in directories if name not in linked_directories]
        return data_dir
    except InstancePathError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise InstancePathError("instance_path_unavailable") from None

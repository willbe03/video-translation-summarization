"""Job directories, the manifest, and stage timing.

Every command reads and writes one job directory, so an interrupted run can be
resumed by pointing the next command at the same directory. Paths in the
manifest are absolute, which lets a job directory be moved without rewriting
the artifacts inside it.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ARTIFACTS_ENV = "VSTUDIO_ARTIFACTS"
PROVIDER_ENV = "VSTUDIO_ASR_PROVIDER"


def config_path() -> Path:
    """Where install writes the choices made at setup time.

    Honours XDG_CONFIG_HOME so the location is the user's, not this machine's.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "vstudio" / "config.env"


def load_config() -> dict[str, str]:
    """Read the config file into the environment without overriding a real env var.

    Every command calls this, so an agent cannot forget to source the file and
    silently get the wrong artifacts directory or ASR provider.
    """
    path = config_path()
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("\"'")
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def write_config(values: dict[str, str]) -> Path:
    """Merge new settings into the config file, keeping what is already there."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                merged[key.strip()] = value.strip()
    merged.update({k: v for k, v in values.items() if v})
    path.write_text(
        "# Written by `vstudio configure`. Real environment variables win.\n"
        + "".join(f"{k}={v}\n" for k, v in sorted(merged.items())),
        encoding="utf-8",
    )
    return path


def artifacts_root() -> Path:
    """Where jobs are created when the caller does not say.

    Set VSTUDIO_ARTIFACTS at install time. Without it, jobs land in a `runs`
    directory beside the current working directory rather than anywhere
    absolute, so nothing in this package pins a path to one machine.
    """
    configured = os.environ.get(ARTIFACTS_ENV)
    return Path(configured).expanduser() if configured else Path.cwd() / "runs"


def slugify(value: str, *, limit: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip()
    slug = re.sub(r"[\s_]+", "-", slug).strip("-").lower()
    return (slug[:limit].rstrip("-") or "job")


def new_job_dir(title: str, *, root: Path | None = None) -> Path:
    base = (root or artifacts_root()).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    job = base / f"{stamp}-{slugify(title)}"
    job.mkdir(parents=True, exist_ok=False)
    return job


def rename_job(job: Path, title: str) -> Path:
    """Rename a job directory once the real title is known.

    A URL's title is only available after the download, so the directory starts
    out named after the source and is corrected here. Any manifest value that
    points inside the job is rewritten, which is why nothing else may cache a
    path across this call.
    """
    target = job.parent / f"{job.name.split('-', 2)[0]}-{job.name.split('-', 2)[1]}-{slugify(title)}"
    if target == job or target.exists():
        return job
    job.rename(target)
    manifest = read_manifest(target)
    old, new = str(job), str(target)
    for key, value in list(manifest.items()):
        if isinstance(value, str) and value.startswith(old):
            manifest[key] = new + value[len(old):]
    manifest["job"] = new
    write_manifest(target, manifest)
    return target


def manifest_path(job: Path) -> Path:
    return job / "manifest.json"


def read_manifest(job: Path) -> dict[str, Any]:
    path = manifest_path(job)
    if not path.is_file():
        raise RuntimeError(
            f"{job} is not a vstudio job directory (no manifest.json). "
            f"Run `vstudio new <source>` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(job: Path, manifest: dict[str, Any]) -> None:
    manifest_path(job).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def update_manifest(job: Path, **fields: Any) -> dict[str, Any]:
    manifest = read_manifest(job)
    manifest.update(fields)
    write_manifest(job, manifest)
    return manifest


def timing_path(job: Path) -> Path:
    return job / "timing.json"


def record_timing(job: Path, step: str, seconds: float, **detail: Any) -> None:
    path = timing_path(job)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    entries.append(
        {
            "step": step,
            "seconds": round(seconds, 3),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **detail,
        }
    )
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def timed(job: Path, step: str, **detail: Any) -> Iterator[dict[str, Any]]:
    """Time a step and record it even when it fails, so a slow failure is visible."""
    extra: dict[str, Any] = {}
    started = time.monotonic()
    try:
        yield extra
    except BaseException:
        record_timing(job, step, time.monotonic() - started, failed=True, **detail, **extra)
        raise
    record_timing(job, step, time.monotonic() - started, **detail, **extra)


def timing_report(job: Path) -> str:
    path = timing_path(job)
    if not path.is_file():
        return "no timing recorded yet"
    entries = json.loads(path.read_text(encoding="utf-8"))
    total = sum(float(e["seconds"]) for e in entries)
    width = max((len(str(e["step"])) for e in entries), default=4)
    lines = [f"{'step'.ljust(width)}  {'seconds':>9}  {'share':>6}  detail"]
    for entry in entries:
        seconds = float(entry["seconds"])
        share = f"{seconds / total * 100:5.1f}%" if total else "    - "
        detail = ", ".join(
            f"{k}={v}" for k, v in entry.items() if k not in {"step", "seconds", "at"}
        )
        lines.append(f"{str(entry['step']).ljust(width)}  {seconds:9.2f}  {share}  {detail}")
    lines.append(f"{'total'.ljust(width)}  {total:9.2f}")
    return "\n".join(lines)

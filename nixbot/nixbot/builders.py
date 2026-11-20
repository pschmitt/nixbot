"""The build machines nix dispatches to, for the builders page.

Read from the nix `builders` setting: a list of machine entries
separated by newlines or `;`, where `@FILE` includes a machines file
(one entry per line, `#` comments). Columns per entry: store URI,
systems, SSH key, max jobs, speed factor, supported features,
mandatory features, base64 host key; `-` means unset. The SSH key and
host key are never exposed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from .executor import local_host, machine_host, short_host

logger = logging.getLogger(__name__)

# Explicit machines file (on autoscaled hosts the one nix reads), else
# `nix config show builders`.
BUILDERS_FILE_ENV = "NIXBOT_BUILDERS_FILE"
# Machines file of the static builders: entries not in it are pool
# workers. Without it, entries addressed by IP count as pool workers.
STATIC_BUILDERS_FILE_ENV = "NIXBOT_STATIC_BUILDERS_FILE"
# Space-separated hosts that run evaluations (remote evaluation).
EVALUATORS_ENV = "NIXBOT_EVALUATORS"


@dataclass(frozen=True)
class Machine:
    uri: str
    host: str
    systems: list[str] = field(default_factory=list)
    max_jobs: int = 1
    speed_factor: int = 1
    supported_features: list[str] = field(default_factory=list)
    mandatory_features: list[str] = field(default_factory=list)
    kind: str = "static"  # static | pool | local


def _list(value: str) -> list[str]:
    return [] if value in ("", "-") else [v for v in value.split(",") if v]


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def parse_machine(line: str) -> Machine | None:
    """One machines-file entry, or None for a blank or comment line."""
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    cols = line.split()
    cols += ["-"] * (8 - len(cols))
    uri = cols[0]
    host = machine_host(uri)
    return Machine(
        uri=uri,
        host=short_host(host) if host else uri,
        systems=_list(cols[1]),
        max_jobs=_int(cols[3], 1) if cols[3] != "-" else 1,
        speed_factor=_int(cols[4], 1) if cols[4] != "-" else 1,
        supported_features=_list(cols[5]),
        mandatory_features=_list(cols[6]),
    )


def parse_builders(spec: str, read_file=None, _depth: int = 0) -> list[Machine]:  # noqa: ANN001
    """Machines of a `builders` value: entries separated by newlines or
    `;`, `@FILE` includes a machines file (nested includes too)."""
    read = read_file or (lambda p: Path(p).read_text())
    machines: list[Machine] = []
    for raw in spec.replace(";", "\n").splitlines():
        entry = raw.split("#", 1)[0].strip()
        if not entry:
            continue
        if entry.startswith("@"):
            if _depth > 4:  # noqa: PLR2004
                continue
            try:
                text = read(entry[1:])
            except OSError as e:
                logger.warning("cannot read builders file %s: %s", entry[1:], e)
                continue
            machines += parse_builders(text, read, _depth + 1)
        elif machine := parse_machine(entry):
            machines.append(machine)
    return machines


def classify(machines: list[Machine], static_uris: set[str] | None) -> list[Machine]:
    """Mark pool workers: entries missing from the static machines file
    when one is known, else entries addressed by an IP address."""
    out = []
    for m in machines:
        pool = m.uri not in static_uris if static_uris is not None else _is_ip(m.host)
        out.append(replace(m, kind="pool" if pool else "static"))
    return out


async def _nix_config(name: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nix",
            "--extra-experimental-features",
            "nix-command",
            "config",
            "show",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (OSError, TimeoutError):
        return None
    return out.decode(errors="replace").strip() if proc.returncode == 0 else None


class BuildersSource:
    """Machines for the builders page. The builders file is re-read on
    every call (an autoscaler rewrites it); `nix config show` runs once."""

    def __init__(self) -> None:
        self._nix_builders: str | None = None
        self._local_jobs: int | None = None
        self._probed = False
        self._lock = asyncio.Lock()

    async def _probe(self) -> None:
        async with self._lock:
            if self._probed:
                return
            if not os.environ.get(BUILDERS_FILE_ENV):
                self._nix_builders = await _nix_config("builders")
            jobs = await _nix_config("max-jobs")
            # "auto" means the core count.
            self._local_jobs = (
                os.cpu_count() or 1 if jobs == "auto" else _int(jobs or "", 0)
            )
            self._probed = True

    async def machines(self) -> list[Machine]:
        await self._probe()
        path = os.environ.get(BUILDERS_FILE_ENV)
        spec = f"@{path}" if path else (self._nix_builders or "")
        static_uris = None
        if static := os.environ.get(STATIC_BUILDERS_FILE_ENV):
            static_uris = {m.uri for m in parse_builders(f"@{static}")}
        remote = classify(parse_builders(spec), static_uris)
        local = Machine(
            uri="local",
            host=local_host(),
            max_jobs=self._local_jobs or 0,
            kind="local",
        )
        return [local, *remote]


def evaluators() -> list[str]:
    """Short names of the remote evaluation hosts (NIXBOT_EVALUATORS)."""
    return [
        short_host(machine_host(h) or h)
        for h in os.environ.get(EVALUATORS_ENV, "").split()
    ]

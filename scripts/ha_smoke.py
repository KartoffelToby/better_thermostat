"""Boot a real Home Assistant with Better Thermostat and check that it works.

The test suite runs against the Home Assistant the lock file pins, so a
release that removes something Better Thermostat relies on is only noticed
once users install it. This script runs the integration in the Home Assistant
container image of any tag instead, ``dev`` and ``beta`` included, without the
test harness: the harness itself lags behind those tags.

The run boots the container twice on one configuration directory. The first
boot sets the thermostat up from a stored config entry, with a
``generic_thermostat`` as its TRV and a template sensor as its room
temperature, and an automation that changes the target. The second boot runs
without that automation. Each boot fails when Home Assistant does not start,
when it logs an error or a deprecation report for Better Thermostat, or when
the thermostat's entity was not live when Home Assistant stopped, or when it
did not hold the target the automation set: the first boot checks that the
change took, the second that it survived the restart.

Only the standard library is used, so the script runs without the project
environment::

    uv run --no-project python scripts/ha_smoke.py --tag dev
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "better_thermostat"
IMAGE = "ghcr.io/home-assistant/home-assistant"
CONTAINER = f"bt-ha-smoke-{os.getpid()}"

THERMOSTAT_ENTITY = "climate.smoke"
TARGET_SET_ON_FIRST_BOOT = 23.5

# Seconds Home Assistant gets to report that it started, and how long the
# thermostat then runs before Home Assistant is stopped. The first boot runs
# longer, so the automation's delay has passed and the new target was saved.
START_TIMEOUT_SECONDS = 300
RUN_SECONDS = {1: 90, 2: 45}
STOP_TIMEOUT_SECONDS = 120
# How long one docker call may take. A call that hangs ends the run as a
# failure, so the job reports it instead of running into its own timeout.
DOCKER_TIMEOUT_SECONDS = 60
PULL_TIMEOUT_SECONDS = 300

STARTED_LINE = "Home Assistant initialized"

# Home Assistant reports a deprecated use with this sentence, naming the
# integration, releases before the use stops working.
DEPRECATION_REPORT = "custom integration 'better_thermostat'"
# Home Assistant colours its console log.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
LOG_LEVEL = re.compile(r"^\S+ \S+ (ERROR|CRITICAL|WARNING) ")

CONFIGURATION = """\
homeassistant:
  name: Smoke
  unit_system: metric
  time_zone: Europe/Berlin

logger:
  default: warning
  logs:
    homeassistant.bootstrap: info

input_boolean:
  heater:
    name: Heater

template:
  - sensor:
      - name: Room temperature
        unique_id: smoke_room_temperature
        state: "19.5"
        unit_of_measurement: "°C"
        device_class: temperature
        state_class: measurement

climate:
  - platform: generic_thermostat
    name: TRV
    unique_id: smoke_trv
    heater: input_boolean.heater
    target_sensor: sensor.room_temperature
    initial_hvac_mode: heat
    target_temp: 20
"""

TARGET_AUTOMATION = f"""
automation:
  - alias: Change the thermostat's target
    triggers:
      - trigger: homeassistant
        event: start
    actions:
      - delay: 30
      - action: climate.set_temperature
        target:
          entity_id: {THERMOSTAT_ENTITY}
        data:
          temperature: {TARGET_SET_ON_FIRST_BOOT}
"""

# The entry the config flow stores for a generic_thermostat TRV. Stored in
# the oldest storage layout, so every Home Assistant migrates it the same way.
CONFIG_ENTRIES = {
    "version": 1,
    "minor_version": 1,
    "key": "core.config_entries",
    "data": {
        "entries": [
            {
                "entry_id": "01SMOKE0000000000000000000",
                "version": 18,
                "minor_version": 2,
                "domain": "better_thermostat",
                "title": "Smoke",
                "data": {
                    "name": "Smoke",
                    "thermostat": [
                        {
                            "trv": "climate.trv",
                            "integration": "generic_thermostat",
                            "model": "Generic",
                            "advanced": {
                                "calibration": "target_temp_based",
                                "calibration_mode": "default",
                                "protect_overheating": False,
                                "no_off_system_mode": False,
                                "heat_auto_swapped": False,
                                "valve_maintenance": False,
                                "child_lock": False,
                                "homematicip": False,
                            },
                        }
                    ],
                    "temperature_sensor": "sensor.room_temperature",
                    "model": "Generic",
                    "target_temp_step": "0.5",
                    "tolerance": 0.3,
                    "off_temperature": 5,
                },
                "options": dict[str, object](),
                "pref_disable_new_entities": False,
                "pref_disable_polling": False,
                "source": "user",
                "unique_id": None,
                "disabled_by": None,
            }
        ]
    },
}


@dataclass(frozen=True)
class Boot:
    """One start of Home Assistant on the shared configuration directory."""

    number: int
    started_at: datetime
    log: str


def _write_configuration(config_dir: Path, boot: int, integration: Path) -> None:
    """Write the configuration for ``boot``, and the config entry once."""
    automation = TARGET_AUTOMATION if boot == 1 else ""
    (config_dir / "configuration.yaml").write_text(
        CONFIGURATION + automation, encoding="utf-8"
    )
    storage = config_dir / ".storage"
    if not storage.exists():
        storage.mkdir()
        (storage / "core.config_entries").write_text(
            json.dumps(CONFIG_ENTRIES, indent=2), encoding="utf-8"
        )
        shutil.copytree(
            integration,
            config_dir / "custom_components" / "better_thermostat",
            ignore=shutil.ignore_patterns("__pycache__"),
        )


def _docker(
    *args: str, timeout: float = DOCKER_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run ``docker`` with ``args`` and return what it printed.

    A call that takes longer than ``timeout`` seconds ends the run as a
    failed smoke test.
    """
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        sys.exit(f"docker {args[0]} did not return within {timeout:g} s")


def _wait_for_start(container: str) -> bool:
    """Return whether Home Assistant reported its start before the timeout."""
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        logs = _docker("logs", container)
        if STARTED_LINE in logs.stdout + logs.stderr:
            return True
        if (
            _docker("inspect", "-f", "{{.State.Running}}", container).stdout.strip()
            != "true"
        ):
            return False
        time.sleep(5)
    return False


def _cleanup(*args: str) -> None:
    """Run a docker cleanup call, reporting rather than raising a failure.

    It runs while the script may already be ending on another failure, which
    it must not replace.
    """
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=DOCKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"cleanup: docker {args[0]} did not return within {DOCKER_TIMEOUT_SECONDS} s"
        )
        return
    if result.returncode != 0:
        print(f"cleanup: docker {args[0]} failed: {result.stderr.strip()}")


def _run_boot(image: str, config_dir: Path, boot: int) -> tuple[Boot, bool]:
    """Start Home Assistant, let it run, stop it, and return what it logged.

    The container is removed however the boot ends.
    """
    _docker("rm", "-f", CONTAINER)
    started_at = datetime.now(UTC)
    try:
        run = _docker(
            "run",
            "-d",
            "--name",
            CONTAINER,
            "-v",
            f"{config_dir}:/config",
            "-e",
            "TZ=Europe/Berlin",
            image,
        )
        if run.returncode != 0:
            sys.exit(f"docker run failed: {run.stderr.strip()}")
        started = _wait_for_start(CONTAINER)
        if started:
            time.sleep(RUN_SECONDS[boot])
        _docker(
            "stop",
            "-t",
            str(STOP_TIMEOUT_SECONDS),
            CONTAINER,
            timeout=STOP_TIMEOUT_SECONDS + DOCKER_TIMEOUT_SECONDS,
        )
        logs = _docker("logs", CONTAINER)
    finally:
        _cleanup("rm", "-f", CONTAINER)
    return Boot(boot, started_at, logs.stdout + logs.stderr), started


def _return_ownership(image: str, config_dir: Path) -> None:
    """Hand the files Home Assistant wrote back to the user running the script.

    Home Assistant runs as root in its container, so the configuration
    directory holds files the runner cannot delete without this.
    """
    _cleanup(
        "run",
        "--rm",
        "-v",
        f"{config_dir}:/config",
        "--entrypoint",
        "chown",
        image,
        "-R",
        f"{os.getuid()}:{os.getgid()}",
        "/config",
    )


def _reported_lines(log: str) -> list[str]:
    """Return the log lines that report a problem with Better Thermostat.

    An error or critical line counts when it names the integration, and a
    warning counts when it is Home Assistant's report of a deprecated use.
    A traceback is reported through the line that logged it.
    """
    found: list[str] = []
    for line in ANSI_ESCAPE.sub("", log).splitlines():
        level = LOG_LEVEL.match(line)
        if level is None:
            continue
        if level.group(1) == "WARNING":
            if DEPRECATION_REPORT in line:
                found.append(line)
        elif "better_thermostat" in line:
            found.append(line)
    return found


def _stored_thermostat(config_dir: Path) -> dict[str, object] | None:
    """Return the thermostat's entry in the restore cache Home Assistant saved."""
    path = config_dir / ".storage" / "core.restore_state"
    if not path.exists():
        return None
    stored = json.loads(path.read_text(encoding="utf-8"))
    for item in stored["data"]:
        if item["state"]["entity_id"] == THERMOSTAT_ENTITY:
            return item
    return None


def _check_boot(config_dir: Path, boot: Boot, started: bool) -> list[str]:
    """Return what went wrong in ``boot``; an empty list when nothing did."""
    if not started:
        tail = "\n".join(boot.log.splitlines()[-40:])
        return [f"Home Assistant did not start:\n{tail}"]
    problems = [f"logged: {line}" for line in _reported_lines(boot.log)]
    item = _stored_thermostat(config_dir)
    if item is None:
        return [*problems, f"{THERMOSTAT_ENTITY} was never saved"]
    last_seen = datetime.fromisoformat(str(item["last_seen"]))
    state = item["state"]
    assert isinstance(state, dict)
    if last_seen < boot.started_at:
        problems.append(f"{THERMOSTAT_ENTITY} was not live in this boot")
    elif state["state"] in ("unavailable", "unknown"):
        problems.append(f"{THERMOSTAT_ENTITY} ended {state['state']}")
    target = state["attributes"].get("temperature")
    if target != TARGET_SET_ON_FIRST_BOOT:
        held = "was not set to" if boot.number == 1 else "did not come back up with"
        problems.append(
            f"{THERMOSTAT_ENTITY} {held} the target {TARGET_SET_ON_FIRST_BOOT}: "
            f"it held {target}"
        )
    return problems


def main() -> int:
    """Run both boots against the image tag and report what went wrong."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tag",
        default="dev",
        help="tag of the Home Assistant image (dev, beta, stable)",
    )
    parser.add_argument(
        "--integration",
        type=Path,
        default=INTEGRATION_DIR,
        help="the integration directory to install, by default this checkout's",
    )
    args = parser.parse_args()
    image = f"{IMAGE}:{args.tag}"
    pull = _docker("pull", "-q", image, timeout=PULL_TIMEOUT_SECONDS)
    if pull.returncode != 0:
        sys.exit(f"docker pull {image} failed: {pull.stderr.strip()}")
    version = _docker(
        "run",
        "--rm",
        "--entrypoint",
        "python3",
        image,
        "-c",
        "from homeassistant.const import __version__; print(__version__)",
    ).stdout.strip()
    print(f"Home Assistant {version} ({image})")

    failed = False
    with tempfile.TemporaryDirectory(
        prefix="bt-ha-smoke-", ignore_cleanup_errors=True
    ) as tmp:
        config_dir = Path(tmp)
        try:
            for number in (1, 2):
                _write_configuration(config_dir, number, args.integration)
                boot, started = _run_boot(image, config_dir, number)
                problems = _check_boot(config_dir, boot, started)
                print(f"boot {number}: {'ok' if not problems else 'FAILED'}")
                for problem in problems:
                    print(f"  {problem}")
                failed = failed or bool(problems)
                if not started:
                    break
        finally:
            _return_ownership(image, config_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""demo.py — Run a customisable Docker container on AWS via AWSContainerRunner.

Configuration
-------------
Environment variables (required unless noted):

    CONTAINER_IMAGE     Docker image to run, e.g. ``ubuntu:22.04``.
    CONTAINER_ENTRYPOINT  (optional) Entrypoint override as a shell string,
                        e.g. ``"python -u script.py"``. If omitted, the image's
                        default entrypoint is used.

Container environment variables are loaded from a ``run.env`` file in the
current working directory (if it exists). Each non-empty, non-comment line
must be in ``KEY=VALUE`` format.

Usage
-----
    # Minimal
    CONTAINER_IMAGE=ubuntu:22.04 python demo.py

    # With entrypoint and env file
    CONTAINER_IMAGE=myrepo/myapp:1.0 CONTAINER_ENTRYPOINT="python main.py" python demo.py
"""

import os
import sys
from pathlib import Path

from .aws_runner import AWSContainerRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> dict:
    """Parse a ``KEY=VALUE`` env file and return a dict of variables.

    Lines that are empty or start with ``#`` are ignored. Inline comments
    (anything after an unquoted ``#``) are *not* stripped — values are taken
    verbatim after the first ``=``.

    Args:
        path: Path to the env file.

    Returns:
        A ``{key: value}`` dict. Returns an empty dict if the file does not
        exist.
    """
    env: dict = {}
    if not path.exists():
        print(f"[demo] No env file found at '{path}', running without extra vars.")
        return env

    with path.open() as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            if not line or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                print(f"[demo] Warning: skipping malformed line {lineno} in '{path}': {line!r}")
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value
    return env


def parse_entrypoint(raw: str | None) -> list[str] | None:
    """Split a shell entrypoint string into a list, or return ``None``.

    Args:
        raw: Entrypoint string (e.g. ``"python -u script.py"``), or ``None``.

    Returns:
        A list of strings suitable for ``spawn_container(entrypoint=...)``, or
        ``None`` if ``raw`` is empty or ``None``.
    """
    if not raw:
        return None
    import shlex
    return shlex.split(raw)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # --- Read configuration --------------------------------------------------
    image = os.environ.get("CONTAINER_IMAGE", "").strip()
    if not image:
        print("Error: CONTAINER_IMAGE environment variable is required.", file=sys.stderr)
        sys.exit(1)

    entrypoint = parse_entrypoint(os.environ.get("CONTAINER_ENTRYPOINT"))
    environment = load_env_file(Path("run.env"))

    # --- Summary -------------------------------------------------------------
    print("[demo] Launch configuration:")
    print(f"  Image      : {image}")
    print(f"  Entrypoint : {entrypoint or '(image default)'}")
    print(f"  Env vars   : {list(environment.keys()) or '(none)'}")
    print()

    # --- Run -----------------------------------------------------------------
    runner = AWSContainerRunner()

    print("[demo] Mirroring image to ECR (skipped if already present)...")
    runner.prepare_image(image)

    print("[demo] Spawning container...")
    container = runner.spawn_container(
        image=image,
        entrypoint=entrypoint,
        environment=environment,
        detach=True,
    )
    print(f"[demo] Task started: {container._task_arn}")

    # --- Stream logs ---------------------------------------------------------
    print("[demo] Streaming container logs (Ctrl-C to detach):\n")
    try:
        for line in container.stream_container_logs():
            print(line)
    except KeyboardInterrupt:
        print("\n[demo] Detached from log stream. Task is still running.")
        return

    # --- Exit code -----------------------------------------------------------
    exit_code = container.get_exit_code()
    print(f"\n[demo] Container finished with exit code: {exit_code}")
    sys.exit(exit_code if exit_code is not None else 0)


if __name__ == "__main__":
    main()
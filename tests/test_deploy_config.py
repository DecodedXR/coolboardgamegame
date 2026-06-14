"""Drift guards for the cloud deploy config (Milestone 3, W4).

These are text-level assertions (no YAML dep) ensuring render.yaml and the
Dockerfile keep pointing at the real entrypoint and server-deps file. The failure
mode they catch: renaming `python -m server` or `requirements-server.txt` without
updating the deploy config, which would break the Render build silently.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_REQS = "requirements-server.txt"
START_CMD = "python -m server"


def test_server_reqs_file_exists():
    assert (REPO_ROOT / SERVER_REQS).is_file()


def test_render_yaml_references_real_entrypoint_and_reqs():
    text = (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")
    assert f"buildCommand: pip install -r {SERVER_REQS}" in text
    assert f"startCommand: {START_CMD}" in text
    # /healthz must match the path the W2 process_request hook answers.
    assert "healthCheckPath: /healthz" in text


def test_dockerfile_runs_server_and_installs_reqs():
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert SERVER_REQS in text
    # CMD is the JSON-array exec form: python -m server.
    assert '["python", "-m", "server"]' in text

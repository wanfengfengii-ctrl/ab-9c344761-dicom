"""One-shot verification service.

Waits for the API to become healthy, then runs, in order:
  1. code tests (pytest unit/API suite)
  2. application build check (byte-compile + app factory instantiation)
  3. HTTP smoke checks against the live API: a valid multi-frame file and a
     file with a broken Basic Offset Table

Exits 0 only if every step passes; exits 1 otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import time

import requests

import synth

API_URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
HEALTH_TIMEOUT_SECONDS = float(os.environ.get("VERIFY_HEALTH_TIMEOUT", "90"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_failures: list[str] = []


def step(name):
    def decorator(fn):
        def wrapper():
            print(f"\n=== {name} ===", flush=True)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"FAIL: {name}: {exc}", flush=True)
                _failures.append(name)
            else:
                print(f"PASS: {name}", flush=True)

        return wrapper

    return decorator


def wait_for_health() -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last_error = "no attempt made"
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{API_URL}/health", timeout=5)
            if response.status_code == 200:
                print(f"API healthy at {API_URL}", flush=True)
                return
            last_error = f"status {response.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(
        f"API at {API_URL} did not become healthy within "
        f"{HEALTH_TIMEOUT_SECONDS:.0f}s ({last_error})"
    )


def run_command(argv: list[str]) -> None:
    result = subprocess.run(argv, cwd=ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"command {' '.join(argv)} exited {result.returncode}")


@step("wait for API health")
def check_health() -> None:
    wait_for_health()


@step("code tests (pytest)")
def check_tests() -> None:
    run_command([sys.executable, "-m", "pytest", "tests", "-q"])


@step("application build")
def check_build() -> None:
    run_command(
        [sys.executable, "-m", "compileall", "-q", "app", "verify", "synth.py"]
    )
    run_command(
        [
            sys.executable,
            "-c",
            "from app.server import create_app; create_app(); "
            "print('app factory OK')",
        ]
    )


@step("smoke: valid multi-frame file")
def check_smoke_valid() -> None:
    groups = synth.sample_fragment_groups()
    data = synth.build_dicom_file(groups)
    requested = [3, 0, 2]
    response = requests.post(
        f"{API_URL}/api/dicom/frames",
        files={"file": ("valid.dcm", data, "application/dicom")},
        data={"frames": ",".join(str(i) for i in requested)},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"expected 200, got {response.status_code}: {response.text}"
        )
    body = response.json()
    frames = body.get("frames")
    if not isinstance(frames, list) or len(frames) != len(requested):
        raise RuntimeError(f"unexpected frames payload: {body!r}")
    for entry, index in zip(frames, requested):
        expected = synth.expected_frame_payload(groups, index)
        assert entry["frame"] == index, f"order broken: {entry['frame']} != {index}"
        assert entry["fragments"] == len(groups[index]), "fragment count mismatch"
        assert entry["bytes"] == len(expected), "byte count mismatch"
        assert entry["sha256"] == hashlib.sha256(expected).hexdigest(), "sha mismatch"
        assert base64.b64decode(entry["data"]) == expected, "payload mismatch"
    # Determinism: an identical request must return byte-identical JSON.
    again = requests.post(
        f"{API_URL}/api/dicom/frames",
        files={"file": ("valid.dcm", data, "application/dicom")},
        data={"frames": ",".join(str(i) for i in requested)},
        timeout=30,
    )
    if again.content != response.content:
        raise RuntimeError("repeated request did not return identical bytes")
    print(f"valid file: frames {requested} verified byte-for-byte", flush=True)


@step("smoke: broken basic offset table")
def check_smoke_bad_offset_table() -> None:
    groups = synth.sample_fragment_groups()
    boundaries = synth.fragment_boundaries([f for g in groups for f in g])
    offsets = [8] + boundaries[1:4]  # first entry not zero
    data = synth.build_dicom_file(groups, bot_offsets=offsets)
    response = requests.post(
        f"{API_URL}/api/dicom/frames",
        files={"file": ("bad-bot.dcm", data, "application/dicom")},
        data={"frames": "0,1"},
        timeout=30,
    )
    if response.status_code != 400:
        raise RuntimeError(
            f"expected 400, got {response.status_code}: {response.text}"
        )
    body = response.json()
    err_type = body.get("error", {}).get("type")
    if err_type != "invalid_offset_table":
        raise RuntimeError(f"expected invalid_offset_table, got {err_type!r}")
    if "frames" in body:
        raise RuntimeError("error response must not contain partial frames")
    print("bad offset table rejected with invalid_offset_table", flush=True)


@step("smoke: frame index out of range")
def check_smoke_out_of_range() -> None:
    groups = synth.sample_fragment_groups()
    data = synth.build_dicom_file(groups)
    response = requests.post(
        f"{API_URL}/api/dicom/frames",
        files={"file": ("valid.dcm", data, "application/dicom")},
        data={"frames": "0,4"},
        timeout=30,
    )
    if response.status_code != 400:
        raise RuntimeError(
            f"expected 400, got {response.status_code}: {response.text}"
        )
    body = response.json()
    if body.get("error", {}).get("type") != "frame_out_of_range":
        raise RuntimeError(f"unexpected error body: {body!r}")
    if "frames" in body:
        raise RuntimeError("error response must not contain partial frames")
    print("out-of-range index rejected with frame_out_of_range", flush=True)


def main() -> int:
    print(f"verify: target API {API_URL}", flush=True)
    for check in (
        check_health,
        check_tests,
        check_build,
        check_smoke_valid,
        check_smoke_bad_offset_table,
        check_smoke_out_of_range,
    ):
        check()
    print("\n=== summary ===", flush=True)
    if _failures:
        for name in _failures:
            print(f"FAIL: {name}", flush=True)
        print("verify: FAILED", flush=True)
        return 1
    print("verify: OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

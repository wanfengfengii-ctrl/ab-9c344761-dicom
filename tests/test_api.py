"""API tests for POST /api/dicom/frames using the Flask test client."""

import base64
import hashlib
import io

import pytest

import synth
from app.server import create_app


@pytest.fixture()
def groups():
    return synth.sample_fragment_groups()


@pytest.fixture()
def valid_file(groups):
    return synth.build_dicom_file(groups)


@pytest.fixture()
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def post(client, file_bytes=None, frames=None, content_type=None, extra=None):
    data = {}
    if file_bytes is not None:
        data["file"] = (io.BytesIO(file_bytes), "sample.dcm")
    if frames is not None:
        data["frames"] = frames
    if extra:
        data.update(extra)
    kwargs = {}
    if content_type is not None:
        kwargs["content_type"] = content_type
    return client.post("/api/dicom/frames", data=data, **kwargs)


# --- happy path ---------------------------------------------------------------


def test_valid_request_returns_frames_in_request_order(client, groups, valid_file):
    response = post(client, valid_file, frames="2,0,3")
    assert response.status_code == 200
    body = response.get_json()
    assert [f["frame"] for f in body["frames"]] == [2, 0, 3]
    for entry, index in zip(body["frames"], [2, 0, 3]):
        expected = synth.expected_frame_payload(groups, index)
        assert entry["bytes"] == len(expected)
        assert entry["fragments"] == len(groups[index])
        assert entry["sha256"] == hashlib.sha256(expected).hexdigest()
        assert base64.b64decode(entry["data"]) == expected


def test_repeated_frames_fields_are_accepted(client, valid_file):
    response = post(client, valid_file, frames=["1", "3"])
    assert response.status_code == 200
    assert [f["frame"] for f in response.get_json()["frames"]] == [1, 3]


def test_single_frame_request(client, groups, valid_file):
    response = post(client, valid_file, frames="1")
    assert response.status_code == 200
    (entry,) = response.get_json()["frames"]
    assert entry["fragments"] == 1
    assert base64.b64decode(entry["data"]) == groups[1][0]


def test_repeated_requests_are_byte_identical(client, valid_file):
    first = post(client, valid_file, frames="0,2").get_json()
    second = post(client, valid_file, frames="0,2").get_json()
    assert first == second


def test_all_32_indices_are_accepted():
    groups = [[synth.sample_fragment(i, 8)] for i in range(32)]
    data = synth.build_dicom_file(groups)
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    response = post(client, data, frames=",".join(str(i) for i in range(32)))
    assert response.status_code == 200
    assert len(response.get_json()["frames"]) == 32


# --- request validation --------------------------------------------------------


def test_missing_file_is_rejected(client):
    # Multipart form that carries some other file field but no 'file' field.
    response = post(
        client, None, frames="0", extra={"other": (io.BytesIO(b"x"), "x.bin")}
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "missing_file"


def test_missing_frames_is_rejected(client, valid_file):
    response = post(client, valid_file, frames=None)
    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "missing_frames"


def test_non_multipart_is_rejected(client, valid_file):
    response = client.post(
        "/api/dicom/frames", data=b"raw", content_type="application/octet-stream"
    )
    assert response.status_code == 415
    assert response.get_json()["error"]["type"] == "invalid_content_type"


def test_non_integer_frame_index_is_rejected(client, valid_file):
    response = post(client, valid_file, frames="0,x")
    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "invalid_frame_index"


def test_negative_frame_index_is_rejected(client, valid_file):
    response = post(client, valid_file, frames="-1")
    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "invalid_frame_index"


def test_duplicate_frame_index_is_rejected(client, valid_file):
    response = post(client, valid_file, frames="1,2,1")
    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "duplicate_frame_index"


def test_more_than_32_indices_is_rejected(client, valid_file):
    frames = ",".join(str(i) for i in range(33))
    response = post(client, valid_file, frames=frames)
    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "too_many_frames_requested"


def test_frame_out_of_range_is_rejected(client, valid_file):
    response = post(client, valid_file, frames="0,4")
    assert response.status_code == 400
    assert response.get_json()["error"]["type"] == "frame_out_of_range"


def test_oversized_file_is_rejected(client, valid_file):
    client.application.config["MAX_DICOM_FILE_SIZE"] = 1024
    response = post(client, valid_file, frames="0")
    assert response.status_code == 413
    assert response.get_json()["error"]["type"] == "file_too_large"


# --- DICOM errors: stable types, no partial frames ------------------------------


def assert_error(response, status, err_type):
    assert response.status_code == status
    body = response.get_json()
    assert body["error"]["type"] == err_type
    assert "frames" not in body


def test_bad_offset_table_returns_stable_error(client, groups):
    boundaries = synth.fragment_boundaries([f for g in groups for f in g])
    offsets = [8] + boundaries[1:4]
    data = synth.build_dicom_file(groups, bot_offsets=offsets)
    response = post(client, data, frames="0,1")
    assert_error(response, 400, "invalid_offset_table")


def test_truncated_file_returns_stable_error(client, valid_file):
    response = post(client, valid_file[:-20], frames="0")
    assert_error(response, 400, "dicom_truncated")


def test_bad_prefix_returns_stable_error(client, valid_file):
    broken = valid_file[:128] + b"XCIM" + valid_file[132:]
    response = post(client, broken, frames="0")
    assert_error(response, 400, "dicom_bad_prefix")


def test_wrong_transfer_syntax_returns_stable_error(client, groups):
    data = synth.build_dicom_file(groups, transfer_syntax=synth.TS_EXPLICIT_VR_LE)
    response = post(client, data, frames="0")
    assert_error(response, 400, "unsupported_transfer_syntax")


def test_error_precedes_any_frame_output(client, valid_file):
    # First index valid, second out of range: no partial frame list.
    response = post(client, valid_file, frames="0,99")
    assert_error(response, 400, "frame_out_of_range")


# --- health ---------------------------------------------------------------------


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}

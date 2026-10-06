"""HTTP API: POST /api/dicom/frames plus a health endpoint."""

from __future__ import annotations

import base64
import hashlib
import re

from flask import Flask, current_app, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from .dicom import parse_dicom
from .errors import ApiError

MAX_FILE_SIZE = 16 * 1024 * 1024  # 16 MiB, applies to the uploaded file
# Allow a little multipart framing overhead above the file limit itself.
MAX_REQUEST_SIZE = MAX_FILE_SIZE + 1024 * 1024
MAX_FRAME_INDICES = 32

_FRAME_INDEX_RE = re.compile(r"[+-]?\d+")


def _parse_frame_indices(values: list[str]) -> list[int]:
    """Validate the 'frames' form fields into a list of distinct indices."""
    tokens: list[str] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if token:
                tokens.append(token)
    if not tokens:
        raise ApiError(
            "missing_frames",
            "at least one frame index is required via the 'frames' field",
        )
    if len(tokens) > MAX_FRAME_INDICES:
        raise ApiError(
            "too_many_frames_requested",
            f"at most {MAX_FRAME_INDICES} frame indices may be requested, "
            f"got {len(tokens)}",
        )
    indices: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        if not token.isascii() or not _FRAME_INDEX_RE.fullmatch(token):
            raise ApiError(
                "invalid_frame_index",
                f"frame index {token!r} is not an integer",
            )
        index = int(token, 10)
        if index < 0:
            raise ApiError(
                "invalid_frame_index",
                "frame indices are zero-based and must be non-negative",
            )
        if index in seen:
            raise ApiError(
                "duplicate_frame_index",
                f"frame index {index} was requested more than once",
            )
        seen.add(index)
        indices.append(index)
    return indices


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_SIZE
    app.config["MAX_DICOM_FILE_SIZE"] = MAX_FILE_SIZE

    @app.errorhandler(ApiError)
    def _handle_api_error(err: ApiError):
        return jsonify(err.to_dict()), err.status

    @app.errorhandler(RequestEntityTooLarge)
    def _handle_too_large(err):
        return (
            jsonify(
                {
                    "error": {
                        "type": "file_too_large",
                        "message": "request exceeds the 16 MiB file limit",
                    }
                }
            ),
            413,
        )

    @app.errorhandler(404)
    def _handle_not_found(err):
        return (
            jsonify({"error": {"type": "not_found", "message": "unknown route"}}),
            404,
        )

    @app.errorhandler(405)
    def _handle_method_not_allowed(err):
        return (
            jsonify(
                {
                    "error": {
                        "type": "method_not_allowed",
                        "message": "method not allowed",
                    }
                }
            ),
            405,
        )

    @app.errorhandler(500)
    def _handle_internal_error(err):
        return (
            jsonify(
                {
                    "error": {
                        "type": "internal_error",
                        "message": "unexpected internal error",
                    }
                }
            ),
            500,
        )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/api/dicom/frames")
    def extract_frames():
        content_type = request.content_type or ""
        if not content_type.startswith("multipart/form-data"):
            raise ApiError(
                "invalid_content_type",
                "Content-Type must be multipart/form-data",
                status=415,
            )
        upload = request.files.get("file")
        if upload is None:
            raise ApiError(
                "missing_file", "multipart field 'file' is required"
            )
        data = upload.read()
        if len(data) > current_app.config["MAX_DICOM_FILE_SIZE"]:
            raise ApiError(
                "file_too_large",
                "file exceeds the 16 MiB limit",
                status=413,
            )
        indices = _parse_frame_indices(request.form.getlist("frames"))

        # Fully parse and validate before extracting anything, so an error
        # can never produce a partial frame response.
        parsed = parse_dicom(data)

        frames = []
        for index in indices:
            if index >= parsed.number_of_frames:
                raise ApiError(
                    "frame_out_of_range",
                    f"frame index {index} is out of range; the file declares "
                    f"{parsed.number_of_frames} frame(s)",
                )
            payload, fragment_count = parsed.frame_payload(index)
            frames.append(
                {
                    "frame": index,
                    "fragments": fragment_count,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "data": base64.b64encode(payload).decode("ascii"),
                }
            )
        return jsonify({"frames": frames})

    return app


app = create_app()

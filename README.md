# DICOM Frame Extraction Service

Extracts user-selected, still-compressed frames from multi-frame DICOM
Part 10 files and returns them as Base64 payloads with integrity digests.
The built-in parser is deliberately strict: any structural anomaly is
rejected with a stable error type instead of being silently repaired, so
offset misalignment and truncated data can never leak through as
"successfully parsed" frames.

## Accepted input profile

* DICOM Part 10 file: 128-byte preamble + `DICM` prefix + file meta group
  with a consistent group length.
* Transfer Syntax UID `1.2.840.10008.1.2.4.50` (JPEG Baseline Process 1 —
  Explicit VR Little Endian dataset with encapsulated pixel data).
* Exactly one Pixel Data element (OB/OW, undefined length) containing:
  * a **complete** Basic Offset Table: exactly `4 x NumberOfFrames` bytes,
    first entry `0`, strictly increasing, every entry landing exactly on a
    fragment item boundary;
  * finite-length, even-sized fragment items;
  * a zero-length sequence delimitation item, after which the file must end.
* All dataset elements before Pixel Data must have finite lengths.
* Declared `NumberOfFrames` (0028,0008) must be 1–256 (defaults to 1 when
  absent).

## API

### `POST /api/dicom/frames`

`multipart/form-data` with:

| field   | content                                                        |
|---------|----------------------------------------------------------------|
| `file`  | one Part 10 file, at most 16 MiB                               |
| `frames`| 1–32 distinct zero-based frame indices; repeat the field and/or use comma-separated values (e.g. `frames=2,0`) |

Success `200` — frames are returned **in request order**, each payload the
byte-exact concatenation of that frame's fragments:

```json
{
  "frames": [
    {"frame": 2, "fragments": 3, "bytes": 280, "sha256": "…", "data": "<base64>"}
  ]
}
```

Errors — always `{"error": {"type": …, "message": …}}`, never a partial
frame list:

| status | type |
|--------|------|
| 400 | `missing_file`, `missing_frames`, `invalid_frame_index`, `duplicate_frame_index`, `too_many_frames_requested`, `frame_out_of_range` |
| 400 | `dicom_too_small`, `dicom_bad_prefix`, `dicom_bad_meta`, `dicom_truncated`, `dicom_bad_element`, `unsupported_transfer_syntax`, `invalid_number_of_frames`, `missing_pixel_data`, `pixel_data_not_encapsulated`, `invalid_offset_table`, `dicom_bad_item`, `dicom_bad_delimitation`, `dicom_trailing_data` |
| 413 | `file_too_large` |
| 415 | `invalid_content_type` |

### `GET /health`

Returns `{"status": "ok"}`; used by the Docker health check and the
verify service.

## Run with Docker Compose

```sh
APP_PORT=8080 docker compose up --build api
# API is now on http://localhost:8080
```

`APP_PORT` selects the host port (default 8000); the container always
listens on 8000. The `api` service has a health check polling `/health`.

## One-shot verification

```sh
docker compose up --build --exit-code-from verify --abort-on-container-exit
```

The `verify` service waits for `api` to report healthy, then runs:

1. the pytest suite (`tests/`),
2. the application build check (byte-compile + app factory instantiation),
3. HTTP smoke checks against the live API: a valid multi-frame file
   (byte-for-byte payload/digest verification, request-order and
   determinism checks), a file with a broken Basic Offset Table
   (`invalid_offset_table`), and an out-of-range frame index
   (`frame_out_of_range`).

It prints a per-step PASS/FAIL report and exits `0` on success, `1`
otherwise; `--exit-code-from verify` propagates that code.

## Local development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests -q
.venv/bin/gunicorn --bind 127.0.0.1:8000 app.server:app
API_URL=http://127.0.0.1:8000 .venv/bin/python -m verify.run
```

## Layout

```
app/dicom.py    strict Part 10 parser (meta, elements, BOT, fragments)
app/server.py   Flask routes, request validation, error mapping
app/errors.py   ApiError with stable error types
synth.py        synthetic DICOM builder shared by tests and smoke checks
tests/          parser unit tests + API tests
verify/run.py   one-shot health-gated verification flow
Dockerfile / docker-compose.yml
```

"""Strict DICOM Part 10 parser for JPEG Baseline encapsulated pixel data.

The parser is deliberately strict: any structural anomaly (bad preamble,
inconsistent meta group length, element boundary overrun, malformed item,
missing sequence delimitation, offset table that does not match the fragment
layout, ...) raises :class:`ApiError` with a stable error type instead of
attempting the kind of lenient recovery that would mask offset misalignment
or truncated data.

Accepted input:
  * Part 10 file: 128-byte preamble + "DICM" prefix + file meta group.
  * Transfer Syntax UID 1.2.840.10008.1.2.4.50 (JPEG Baseline Process 1),
    i.e. an Explicit VR Little Endian dataset with encapsulated pixel data.
  * Exactly one Pixel Data element, OB/OW VR, undefined length, containing a
    complete Basic Offset Table (one uint32 per declared frame, first entry
    zero, strictly increasing, each entry landing on a fragment item
    boundary), finite-length fragment items, and a zero-length sequence
    delimitation item at the very end of the file.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import ApiError

PREAMBLE_LENGTH = 128
DICM_PREFIX = b"DICM"
MIN_FILE_SIZE = PREAMBLE_LENGTH + len(DICM_PREFIX)

TS_JPEG_BASELINE = "1.2.840.10008.1.2.4.50"

MAX_DECLARED_FRAMES = 256

TAG_FILE_META_GROUP_LENGTH = (0x0002, 0x0000)
TAG_TRANSFER_SYNTAX_UID = (0x0002, 0x0010)
TAG_NUMBER_OF_FRAMES = (0x0028, 0x0008)
TAG_PIXEL_DATA = (0x7FE0, 0x0010)
TAG_ITEM = (0xFFFE, 0xE000)
TAG_SEQUENCE_DELIMITATION = (0xFFFE, 0xE0DD)

UNDEFINED_LENGTH = 0xFFFFFFFF

# VRs defined by DICOM PS3.5 (explicit VR little endian encoding).
KNOWN_VRS = frozenset(
    "AE AS AT CS DA DS DT FD FL IS LO LT OB OD OF OL OV OW PN SH SL SQ SS ST "
    "SV TM UC UI UL UN UR US UT UV".split()
)
# VRs that use 2 reserved bytes + a 4-byte length instead of a 2-byte length.
LONG_LENGTH_VRS = frozenset(
    "OB OD OF OL OV OW SQ SV UC UN UR UT UV".split()
)


@dataclass(frozen=True)
class Fragment:
    """One encapsulated fragment item inside the Pixel Data element."""

    item_offset: int  # offset of this fragment's item tag, relative to the
    # first fragment item tag (the Basic Offset Table reference frame)
    start: int  # absolute offset of the fragment payload in the file
    length: int  # payload length in bytes


@dataclass(frozen=True)
class ParsedDicom:
    """Result of a fully validated parse; safe to extract frames from."""

    data: bytes
    number_of_frames: int
    fragments: tuple[Fragment, ...]
    # [start, end) fragment indices for each declared frame
    frame_fragment_ranges: tuple[tuple[int, int], ...]

    def frame_payload(self, index: int) -> tuple[bytes, int]:
        """Return the concatenated raw fragment bytes and fragment count."""
        start, end = self.frame_fragment_ranges[index]
        fragments = self.fragments[start:end]
        payload = b"".join(
            self.data[f.start : f.start + f.length] for f in fragments
        )
        return payload, len(fragments)


def _tag_str(tag: tuple[int, int]) -> str:
    return f"({tag[0]:04X},{tag[1]:04X})"


def _element_header(data: bytes, pos: int, limit: int, err_type: str):
    """Read an Explicit VR Little Endian element header at ``pos``.

    ``limit`` is the exclusive upper bound the header must fit within;
    overruns raise ``err_type``. Returns (tag, vr, value_length, value_pos).
    """
    if pos + 8 > limit:
        raise ApiError(
            err_type, f"element header at offset {pos} extends past its boundary"
        )
    group, elem = struct.unpack_from("<HH", data, pos)
    tag = (group, elem)
    vr_raw = data[pos + 4 : pos + 6]
    try:
        vr = vr_raw.decode("ascii")
    except UnicodeDecodeError:
        raise ApiError(
            "dicom_bad_element", f"non-ASCII VR at offset {pos}"
        ) from None
    if vr not in KNOWN_VRS:
        raise ApiError(
            "dicom_bad_element", f"unknown VR {vr!r} at offset {pos}"
        )
    if vr in LONG_LENGTH_VRS:
        if data[pos + 6 : pos + 8] != b"\x00\x00":
            raise ApiError(
                "dicom_bad_element",
                f"reserved bytes of VR {vr} at offset {pos} are not zero",
            )
        if pos + 12 > limit:
            raise ApiError(
                err_type,
                f"element header at offset {pos} extends past its boundary",
            )
        (length,) = struct.unpack_from("<I", data, pos + 8)
        value_pos = pos + 12
    else:
        (length,) = struct.unpack_from("<H", data, pos + 6)
        value_pos = pos + 8
    return tag, vr, length, value_pos


def _parse_file_meta(data: bytes) -> int:
    """Validate the file meta information; return the dataset start offset."""
    pos = MIN_FILE_SIZE
    tag, vr, length, value_pos = _element_header(
        data, pos, len(data), "dicom_bad_meta"
    )
    if tag != TAG_FILE_META_GROUP_LENGTH:
        raise ApiError(
            "dicom_bad_meta",
            "file meta information must start with (0002,0000) group length, "
            f"got {_tag_str(tag)}",
        )
    if vr != "UL" or length != 4:
        raise ApiError(
            "dicom_bad_meta",
            f"(0002,0000) must be VR UL with length 4, got VR {vr} length {length}",
        )
    if value_pos + 4 > len(data):
        raise ApiError(
            "dicom_bad_meta", "(0002,0000) value extends past end of file"
        )
    (meta_length,) = struct.unpack_from("<I", data, value_pos)
    pos = value_pos + 4
    meta_end = pos + meta_length
    if meta_end > len(data):
        raise ApiError(
            "dicom_bad_meta",
            f"file meta group length {meta_length} extends past end of file",
        )

    transfer_syntax = None
    while pos < meta_end:
        tag, vr, length, value_pos = _element_header(
            data, pos, meta_end, "dicom_bad_meta"
        )
        if tag[0] != 0x0002:
            raise ApiError(
                "dicom_bad_meta",
                f"non-meta element {_tag_str(tag)} inside the file meta group",
            )
        value_end = value_pos + length
        if value_end > meta_end:
            raise ApiError(
                "dicom_bad_meta",
                f"meta element {_tag_str(tag)} value exceeds the group length",
            )
        if tag == TAG_TRANSFER_SYNTAX_UID:
            if transfer_syntax is not None:
                raise ApiError(
                    "dicom_bad_meta", "duplicate Transfer Syntax UID (0002,0010)"
                )
            raw = data[value_pos:value_end].rstrip(b"\x00")
            try:
                transfer_syntax = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                raise ApiError(
                    "dicom_bad_meta", "Transfer Syntax UID is not ASCII"
                ) from None
        pos = value_end

    if transfer_syntax is None:
        raise ApiError(
            "dicom_bad_meta", "missing Transfer Syntax UID (0002,0010)"
        )
    if transfer_syntax != TS_JPEG_BASELINE:
        raise ApiError(
            "unsupported_transfer_syntax",
            f"transfer syntax {transfer_syntax} is not supported; only JPEG "
            f"Baseline ({TS_JPEG_BASELINE}, Explicit VR Little Endian dataset "
            "with encapsulated pixel data) is accepted",
        )
    return meta_end


def _parse_number_of_frames(raw: bytes) -> int:
    try:
        text = raw.decode("ascii").strip()
        number = int(text, 10)
    except (UnicodeDecodeError, ValueError):
        raise ApiError(
            "invalid_number_of_frames",
            "Number of Frames (0028,0008) is not a valid integer",
        ) from None
    if number < 1:
        raise ApiError(
            "invalid_number_of_frames", "Number of Frames must be at least 1"
        )
    if number > MAX_DECLARED_FRAMES:
        raise ApiError(
            "invalid_number_of_frames",
            f"declared Number of Frames {number} exceeds the limit of "
            f"{MAX_DECLARED_FRAMES}",
        )
    return number


def _parse_pixel_data(
    data: bytes, vr: str, length: int, value_pos: int, number_of_frames: int | None
) -> ParsedDicom:
    if vr not in ("OB", "OW"):
        raise ApiError(
            "pixel_data_not_encapsulated",
            f"Pixel Data VR {vr} is not OB/OW; only encapsulated pixel data "
            "is accepted",
        )
    if length != UNDEFINED_LENGTH:
        raise ApiError(
            "pixel_data_not_encapsulated",
            "Pixel Data has a defined length; only encapsulated (undefined "
            "length) pixel data is accepted",
        )
    n_frames = number_of_frames if number_of_frames is not None else 1

    # --- Basic Offset Table item -------------------------------------------
    pos = value_pos
    if pos + 8 > len(data):
        raise ApiError(
            "dicom_truncated",
            "encapsulated Pixel Data ends before the Basic Offset Table item",
        )
    group, elem, bot_length = struct.unpack_from("<HHI", data, pos)
    if (group, elem) != TAG_ITEM:
        raise ApiError(
            "invalid_offset_table",
            f"encapsulated Pixel Data must start with a Basic Offset Table "
            f"item (FFFE,E000), got {_tag_str((group, elem))}",
        )
    expected_bot_length = 4 * n_frames
    if bot_length != expected_bot_length:
        raise ApiError(
            "invalid_offset_table",
            f"Basic Offset Table is {bot_length} bytes; expected exactly "
            f"{expected_bot_length} bytes ({n_frames} frame(s) x 4)",
        )
    bot_pos = pos + 8
    if bot_pos + bot_length > len(data):
        raise ApiError(
            "dicom_truncated", "Basic Offset Table value extends past end of file"
        )
    offsets = list(struct.unpack_from(f"<{n_frames}I", data, bot_pos))
    if offsets[0] != 0:
        raise ApiError(
            "invalid_offset_table",
            f"first Basic Offset Table entry must be 0, got {offsets[0]}",
        )
    for previous, current in zip(offsets, offsets[1:]):
        if current <= previous:
            raise ApiError(
                "invalid_offset_table",
                "Basic Offset Table entries must be strictly increasing",
            )
    pos = bot_pos + bot_length

    # --- Fragment items ------------------------------------------------------
    fragments_region_start = pos
    fragments: list[Fragment] = []
    while True:
        if pos + 8 > len(data):
            raise ApiError(
                "dicom_truncated",
                "encapsulated Pixel Data ends before the sequence "
                "delimitation item",
            )
        group, elem, item_length = struct.unpack_from("<HHI", data, pos)
        tag = (group, elem)
        if tag == TAG_SEQUENCE_DELIMITATION:
            if item_length != 0:
                raise ApiError(
                    "dicom_bad_delimitation",
                    f"sequence delimitation item must have zero length, got "
                    f"{item_length}",
                )
            pos += 8
            break
        if tag != TAG_ITEM:
            raise ApiError(
                "dicom_bad_item",
                f"unexpected tag {_tag_str(tag)} inside encapsulated Pixel "
                "Data; expected a fragment item or the sequence delimitation",
            )
        value_end = pos + 8 + item_length
        if value_end > len(data):
            raise ApiError(
                "dicom_truncated",
                f"fragment item at offset {pos} declares {item_length} bytes "
                "and extends past end of file",
            )
        if item_length % 2 != 0:
            raise ApiError(
                "dicom_bad_item",
                f"fragment item at offset {pos} has odd length {item_length}",
            )
        fragments.append(
            Fragment(
                item_offset=pos - fragments_region_start,
                start=pos + 8,
                length=item_length,
            )
        )
        pos = value_end

    if pos != len(data):
        raise ApiError(
            "dicom_trailing_data",
            f"{len(data) - pos} unexpected byte(s) after the Pixel Data "
            "sequence delimitation",
        )
    if not fragments:
        raise ApiError(
            "invalid_offset_table",
            "encapsulated Pixel Data contains no fragment items",
        )

    # --- Map Basic Offset Table entries onto fragment boundaries ------------
    boundary_index = {f.item_offset: i for i, f in enumerate(fragments)}
    for offset in offsets:
        if offset not in boundary_index:
            raise ApiError(
                "invalid_offset_table",
                f"Basic Offset Table entry {offset} does not land on a "
                "fragment item boundary",
            )
    ranges = []
    for i, offset in enumerate(offsets):
        start = boundary_index[offset]
        if i + 1 < len(offsets):
            end = boundary_index[offsets[i + 1]]
        else:
            end = len(fragments)
        ranges.append((start, end))

    return ParsedDicom(
        data=data,
        number_of_frames=n_frames,
        fragments=tuple(fragments),
        frame_fragment_ranges=tuple(ranges),
    )


def parse_dicom(data: bytes) -> ParsedDicom:
    """Fully parse and validate ``data``; raise ApiError on any anomaly."""
    if len(data) < MIN_FILE_SIZE:
        raise ApiError(
            "dicom_too_small",
            f"file is {len(data)} bytes; a Part 10 file needs at least the "
            f"128-byte preamble and 4-byte 'DICM' prefix",
        )
    if data[PREAMBLE_LENGTH:MIN_FILE_SIZE] != DICM_PREFIX:
        raise ApiError(
            "dicom_bad_prefix", "missing 'DICM' prefix at offset 128"
        )

    pos = _parse_file_meta(data)

    number_of_frames = None
    parsed = None
    while pos < len(data):
        tag, vr, length, value_pos = _element_header(
            data, pos, len(data), "dicom_truncated"
        )
        if tag == TAG_PIXEL_DATA:
            parsed = _parse_pixel_data(data, vr, length, value_pos, number_of_frames)
            break
        if length == UNDEFINED_LENGTH:
            raise ApiError(
                "dicom_bad_element",
                f"element {_tag_str(tag)} before Pixel Data has undefined "
                "length; all preceding elements must have finite length",
            )
        value_end = value_pos + length
        if value_end > len(data):
            raise ApiError(
                "dicom_truncated",
                f"element {_tag_str(tag)} value extends past end of file",
            )
        if tag == TAG_NUMBER_OF_FRAMES:
            number_of_frames = _parse_number_of_frames(data[value_pos:value_end])
        pos = value_end

    if parsed is None:
        raise ApiError(
            "missing_pixel_data", "no Pixel Data (7FE0,0010) element found"
        )
    return parsed

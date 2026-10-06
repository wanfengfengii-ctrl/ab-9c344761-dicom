"""Unit tests for the strict DICOM parser."""

import struct

import pytest

import synth
from app.dicom import parse_dicom
from app.errors import ApiError


@pytest.fixture()
def groups():
    return synth.sample_fragment_groups()


@pytest.fixture()
def valid_file(groups):
    return synth.build_dicom_file(groups)


def parse_ok(data):
    return parse_dicom(data)


def err_type(data):
    with pytest.raises(ApiError) as excinfo:
        parse_dicom(data)
    return excinfo.value.err_type


# --- happy path -------------------------------------------------------------


def test_valid_multiframe_file(groups, valid_file):
    parsed = parse_ok(valid_file)
    assert parsed.number_of_frames == 4
    assert [end - start for start, end in parsed.frame_fragment_ranges] == [
        2,
        1,
        3,
        1,
    ]
    for index in range(4):
        payload, count = parsed.frame_payload(index)
        assert payload == synth.expected_frame_payload(groups, index)
        assert count == len(groups[index])


def test_extraction_is_deterministic(valid_file):
    first = parse_ok(valid_file).frame_payload(2)
    second = parse_ok(valid_file).frame_payload(2)
    assert first == second


def test_single_frame_without_number_of_frames():
    groups = [[synth.sample_fragment(0, 64)]]
    data = synth.build_dicom_file(groups, include_number_of_frames=False)
    parsed = parse_ok(data)
    assert parsed.number_of_frames == 1
    assert parsed.frame_payload(0)[0] == groups[0][0]


# --- preamble / prefix -------------------------------------------------------


def test_file_smaller_than_preamble_is_rejected():
    assert err_type(b"\x00" * 100) == "dicom_too_small"


def test_bad_prefix_is_rejected(valid_file):
    broken = valid_file[:128] + b"XCIM" + valid_file[132:]
    assert err_type(broken) == "dicom_bad_prefix"


# --- file meta ---------------------------------------------------------------


def test_meta_group_length_overrun_is_rejected(valid_file):
    # (0002,0000) value lives at offset 132 + 8 (short-form UL header).
    broken = bytearray(valid_file)
    struct.pack_into("<I", broken, 140, 10_000_000)
    assert err_type(bytes(broken)) == "dicom_bad_meta"


def test_meta_must_start_with_group_length():
    meta = synth.element(0x0002, 0x0010, "UI", synth.ui_value(synth.TS_JPEG_BASELINE))
    data = b"\x00" * 128 + b"DICM" + meta
    assert err_type(data) == "dicom_bad_meta"


def test_non_meta_element_inside_meta_group_is_rejected():
    body = synth.element(0x0002, 0x0010, "UI", synth.ui_value(synth.TS_JPEG_BASELINE))
    body += synth.element(0x0010, 0x0010, "PN", b"X ")
    meta = synth.element(0x0002, 0x0000, "UL", struct.pack("<I", len(body))) + body
    data = b"\x00" * 128 + b"DICM" + meta
    assert err_type(data) == "dicom_bad_meta"


def test_meta_element_value_overrunning_group_length_is_rejected():
    # Declare a group length that cuts a meta element value in half.
    ts = synth.element(0x0002, 0x0010, "UI", synth.ui_value(synth.TS_JPEG_BASELINE))
    meta = synth.element(0x0002, 0x0000, "UL", struct.pack("<I", len(ts) - 4)) + ts
    data = b"\x00" * 128 + b"DICM" + meta + b"\x00" * 64
    assert err_type(data) == "dicom_bad_meta"


def test_missing_transfer_syntax_is_rejected():
    body = synth.element(0x0002, 0x0001, "OB", b"\x00\x01")
    meta = synth.element(0x0002, 0x0000, "UL", struct.pack("<I", len(body))) + body
    data = b"\x00" * 128 + b"DICM" + meta
    assert err_type(data) == "dicom_bad_meta"


def test_wrong_transfer_syntax_is_rejected(groups):
    data = synth.build_dicom_file(groups, transfer_syntax=synth.TS_EXPLICIT_VR_LE)
    assert err_type(data) == "unsupported_transfer_syntax"


# --- dataset elements --------------------------------------------------------


def test_undefined_length_element_before_pixel_data_is_rejected(groups):
    sq = (
        struct.pack("<HH", 0x0008, 0x1111)
        + b"SQ"
        + b"\x00\x00"
        + struct.pack("<I", 0xFFFFFFFF)
    )
    data = synth.build_dicom_file(groups, extra_dataset=sq)
    assert err_type(data) == "dicom_bad_element"


def test_element_value_overrunning_file_is_rejected(groups):
    # Declare a huge PN value that runs past the end of the file.
    bad = struct.pack("<HH", 0x0010, 0x0010) + b"PN" + struct.pack("<H", 60000)
    data = synth.build_dicom_file(groups, extra_dataset=bad)
    assert err_type(data) == "dicom_truncated"


def test_reserved_bytes_must_be_zero(groups):
    bad = (
        struct.pack("<HH", 0x0028, 0x0008)
        + b"OB"
        + b"\x01\x00"
        + struct.pack("<I", 2)
        + b"4 "
    )
    data = synth.build_dicom_file(groups, extra_dataset=bad)
    assert err_type(data) == "dicom_bad_element"


def test_unknown_vr_is_rejected(groups):
    bad = struct.pack("<HH", 0x0028, 0x0008) + b"ZZ" + struct.pack("<H", 2) + b"4 "
    data = synth.build_dicom_file(groups, extra_dataset=bad)
    assert err_type(data) == "dicom_bad_element"


def test_missing_pixel_data_is_rejected():
    meta = synth.file_meta(synth.TS_JPEG_BASELINE)
    dataset = synth.element(0x0010, 0x0010, "PN", b"SMOKE^TEST")
    data = b"\x00" * 128 + b"DICM" + meta + dataset
    assert err_type(data) == "missing_pixel_data"


# --- number of frames --------------------------------------------------------


def test_number_of_frames_above_limit_is_rejected(groups):
    data = synth.build_dicom_file(groups, number_of_frames=257)
    assert err_type(data) == "invalid_number_of_frames"


def test_number_of_frames_at_limit_is_accepted():
    groups = [[synth.sample_fragment(i, 8)] for i in range(256)]
    data = synth.build_dicom_file(groups, number_of_frames=256)
    assert parse_ok(data).number_of_frames == 256


def test_non_integer_number_of_frames_is_rejected(groups):
    bad = synth.element(0x0028, 0x0008, "IS", b"AB")
    data = synth.build_dicom_file(
        groups, include_number_of_frames=False, extra_dataset=bad
    )
    assert err_type(data) == "invalid_number_of_frames"


# --- pixel data encapsulation -------------------------------------------------


def test_native_pixel_data_is_rejected():
    meta = synth.file_meta(synth.TS_JPEG_BASELINE)
    dataset = synth.element(0x0028, 0x0008, "IS", b"1 ")
    dataset += synth.element(0x7FE0, 0x0010, "OW", b"\x00" * 16)
    data = b"\x00" * 128 + b"DICM" + meta + dataset
    assert err_type(data) == "pixel_data_not_encapsulated"


def test_pixel_data_with_wrong_vr_is_rejected():
    meta = synth.file_meta(synth.TS_JPEG_BASELINE)
    dataset = synth.element(0x0028, 0x0008, "IS", b"1 ")
    dataset += (
        struct.pack("<HH", 0x7FE0, 0x0010)
        + b"UN"
        + b"\x00\x00"
        + struct.pack("<I", 0xFFFFFFFF)
    )
    dataset += synth.item(0xE000, struct.pack("<I", 0))
    dataset += synth.item(0xE000, synth.sample_fragment(0, 16))
    dataset += synth.sequence_delimitation()
    data = b"\x00" * 128 + b"DICM" + meta + dataset
    assert err_type(data) == "pixel_data_not_encapsulated"


# --- basic offset table -------------------------------------------------------


def test_empty_offset_table_is_rejected(groups):
    data = synth.build_dicom_file(groups[:1], bot_offsets=[])
    assert err_type(data) == "invalid_offset_table"


def test_offset_table_count_mismatch_is_rejected(groups):
    # 4 frames declared but only 3 table entries.
    boundaries = synth.fragment_boundaries([f for g in groups for f in g])
    data = synth.build_dicom_file(groups, bot_offsets=boundaries[:3])
    assert err_type(data) == "invalid_offset_table"


def test_first_offset_must_be_zero(groups):
    boundaries = synth.fragment_boundaries([f for g in groups for f in g])
    offsets = [8] + boundaries[1:4]
    data = synth.build_dicom_file(groups, bot_offsets=offsets)
    assert err_type(data) == "invalid_offset_table"


def test_offsets_must_be_strictly_increasing(groups):
    boundaries = synth.fragment_boundaries([f for g in groups for f in g])
    offsets = boundaries[:4]
    offsets[1], offsets[2] = offsets[2], offsets[1]
    data = synth.build_dicom_file(groups, bot_offsets=offsets)
    assert err_type(data) == "invalid_offset_table"


def test_offset_not_on_fragment_boundary_is_rejected(groups):
    boundaries = synth.fragment_boundaries([f for g in groups for f in g])
    offsets = boundaries[:4]
    offsets[1] += 2  # still increasing, but inside a fragment item
    data = synth.build_dicom_file(groups, bot_offsets=offsets)
    assert err_type(data) == "invalid_offset_table"


def test_offset_past_all_fragments_is_rejected(groups):
    boundaries = synth.fragment_boundaries([f for g in groups for f in g])
    offsets = boundaries[:4]
    offsets[3] = boundaries[-1] + 8 + len(groups[-1][-1]) + 16
    data = synth.build_dicom_file(groups, bot_offsets=offsets)
    assert err_type(data) == "invalid_offset_table"


# --- fragment items / delimitation --------------------------------------------


def test_truncated_fragment_is_rejected(valid_file):
    assert err_type(valid_file[:-20]) == "dicom_truncated"


def test_missing_sequence_delimitation_is_rejected(valid_file):
    assert err_type(valid_file[:-8]) == "dicom_truncated"


def test_sequence_delimitation_with_nonzero_length_is_rejected(valid_file):
    broken = valid_file[:-4] + struct.pack("<I", 2)
    assert err_type(broken) == "dicom_bad_delimitation"


def test_trailing_data_after_delimitation_is_rejected(valid_file):
    assert err_type(valid_file + b"\x00\x00") == "dicom_trailing_data"


def test_odd_fragment_length_is_rejected():
    groups = [[synth.sample_fragment(0, 32) + b"\x00"]]  # 33 bytes, odd
    data = synth.build_dicom_file(groups)
    assert err_type(data) == "dicom_bad_item"


def test_unexpected_tag_inside_pixel_data_is_rejected():
    meta = synth.file_meta(synth.TS_JPEG_BASELINE)
    dataset = synth.element(0x0028, 0x0008, "IS", b"1 ")
    dataset += (
        struct.pack("<HH", 0x7FE0, 0x0010)
        + b"OB"
        + b"\x00\x00"
        + struct.pack("<I", 0xFFFFFFFF)
    )
    dataset += synth.item(0xE000, struct.pack("<I", 0))
    dataset += synth.item(0xE00D, b"")  # item delimitation is not a fragment
    dataset += synth.sequence_delimitation()
    data = b"\x00" * 128 + b"DICM" + meta + dataset
    assert err_type(data) == "dicom_bad_item"


def test_fragment_item_overrunning_file_is_rejected():
    meta = synth.file_meta(synth.TS_JPEG_BASELINE)
    dataset = synth.element(0x0028, 0x0008, "IS", b"1 ")
    dataset += (
        struct.pack("<HH", 0x7FE0, 0x0010)
        + b"OB"
        + b"\x00\x00"
        + struct.pack("<I", 0xFFFFFFFF)
    )
    dataset += synth.item(0xE000, struct.pack("<I", 0))
    dataset += struct.pack("<HHI", 0xFFFE, 0xE000, 5000) + b"\x00" * 16
    data = b"\x00" * 128 + b"DICM" + meta + dataset
    assert err_type(data) == "dicom_truncated"

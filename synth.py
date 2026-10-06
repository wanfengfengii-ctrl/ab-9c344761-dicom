"""Builds synthetic DICOM Part 10 files for tests and smoke checks.

Only constructs the exact profile the service accepts (JPEG Baseline
transfer syntax, Explicit VR Little Endian dataset, encapsulated pixel
data) plus deliberate mutations for negative tests.
"""

from __future__ import annotations

import hashlib
import struct

TS_EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"
TS_JPEG_BASELINE = "1.2.840.10008.1.2.4.50"

LONG_LENGTH_VRS = frozenset("OB OD OF OL OV OW SQ SV UC UN UR UT UV".split())
UNDEFINED_LENGTH = 0xFFFFFFFF


def element(group: int, elem: int, vr: str, value: bytes) -> bytes:
    """Encode one Explicit VR Little Endian element with a finite length."""
    if len(value) % 2 != 0:
        raise ValueError("element values must be even-length; pad first")
    head = struct.pack("<HH", group, elem) + vr.encode("ascii")
    if vr in LONG_LENGTH_VRS:
        return head + b"\x00\x00" + struct.pack("<I", len(value)) + value
    return head + struct.pack("<H", len(value)) + value


def ui_value(uid: str) -> bytes:
    raw = uid.encode("ascii")
    return raw if len(raw) % 2 == 0 else raw + b"\x00"


def is_value(number: int) -> bytes:
    raw = str(number).encode("ascii")
    return raw if len(raw) % 2 == 0 else raw + b" "


def item(tag_elem: int, value: bytes) -> bytes:
    return struct.pack("<HHI", 0xFFFE, tag_elem, len(value)) + value


def sequence_delimitation() -> bytes:
    return struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)


def fragment_boundaries(fragments: list[bytes]) -> list[int]:
    """Item-tag offset of each fragment relative to the first fragment."""
    offsets = []
    pos = 0
    for frag in fragments:
        offsets.append(pos)
        pos += 8 + len(frag)
    return offsets


def pixel_data_element(
    fragment_groups: list[list[bytes]],
    bot_offsets: list[int] | None = None,
    vr: bytes = b"OB",
) -> bytes:
    """Encapsulated Pixel Data element for one frame per fragment group.

    ``bot_offsets`` may be overridden to build deliberately broken tables.
    """
    fragments = [frag for group in fragment_groups for frag in group]
    if bot_offsets is None:
        boundaries = fragment_boundaries(fragments)
        bot_offsets = []
        index = 0
        for group in fragment_groups:
            bot_offsets.append(boundaries[index])
            index += len(group)
    bot = struct.pack(f"<{len(bot_offsets)}I", *bot_offsets)
    out = [
        struct.pack("<HH", 0x7FE0, 0x0010)
        + vr
        + b"\x00\x00"
        + struct.pack("<I", UNDEFINED_LENGTH),
        item(0xE000, bot),
    ]
    out.extend(item(0xE000, frag) for frag in fragments)
    out.append(sequence_delimitation())
    return b"".join(out)


def file_meta(transfer_syntax: str, extra: bytes = b"") -> bytes:
    body = (
        element(0x0002, 0x0001, "OB", b"\x00\x01")
        + element(0x0002, 0x0010, "UI", ui_value(transfer_syntax))
        + extra
    )
    return element(0x0002, 0x0000, "UL", struct.pack("<I", len(body))) + body


def build_dicom_file(
    fragment_groups: list[list[bytes]],
    *,
    number_of_frames: int | None = None,
    transfer_syntax: str = TS_JPEG_BASELINE,
    include_number_of_frames: bool = True,
    extra_dataset: bytes = b"",
    bot_offsets: list[int] | None = None,
    preamble: bytes = b"\x00" * 128,
    prefix: bytes = b"DICM",
) -> bytes:
    """Assemble a complete Part 10 file from per-frame fragment groups."""
    n_frames = (
        number_of_frames if number_of_frames is not None else len(fragment_groups)
    )
    dataset = element(0x0010, 0x0010, "PN", b"SMOKE^TEST")
    if include_number_of_frames:
        dataset += element(0x0028, 0x0008, "IS", is_value(n_frames))
    dataset += extra_dataset
    dataset += pixel_data_element(fragment_groups, bot_offsets=bot_offsets)
    return preamble + prefix + file_meta(transfer_syntax) + dataset


def sample_fragment(seed: int, length: int) -> bytes:
    """Deterministic JPEG-flavoured fragment payload of even length."""
    if length % 2 != 0:
        raise ValueError("fragment length must be even")
    body = b""
    counter = 0
    while len(body) < length - 4:
        body += hashlib.sha256(f"fragment:{seed}:{counter}".encode()).digest()
        counter += 1
    return (b"\xff\xd8" + body)[: length - 2] + b"\xff\xd9"


def sample_fragment_groups() -> list[list[bytes]]:
    """Four frames with 2/1/3/1 fragments of varying sizes."""
    sizes = [[64, 128], [96], [32, 48, 200], [256]]
    groups = []
    seed = 0
    for frame_sizes in sizes:
        group = []
        for size in frame_sizes:
            group.append(sample_fragment(seed, size))
            seed += 1
        groups.append(group)
    return groups


def expected_frame_payload(
    fragment_groups: list[list[bytes]], frame_index: int
) -> bytes:
    return b"".join(fragment_groups[frame_index])

from __future__ import annotations

from pathlib import Path
import struct


class ImageHeaderError(ValueError):
    pass


_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    },
)


def image_dimensions(path: Path) -> tuple[str, int, int]:
    """Read PNG/JPEG dimensions without importing the legacy image runtime."""

    with path.open("rb") as stream:
        prefix = stream.read(24)
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            if len(prefix) < 24 or prefix[12:16] != b"IHDR":
                raise ImageHeaderError("invalid PNG IHDR")
            width, height = struct.unpack(">II", prefix[16:24])
            if width <= 0 or height <= 0:
                raise ImageHeaderError("invalid PNG dimensions")
            return "png", width, height

        if not prefix.startswith(b"\xff\xd8"):
            raise ImageHeaderError("image is neither PNG nor JPEG")
        stream.seek(2)
        while True:
            marker_prefix = stream.read(1)
            if not marker_prefix:
                raise ImageHeaderError("JPEG has no start-of-frame marker")
            if marker_prefix != b"\xff":
                continue
            marker_byte = stream.read(1)
            while marker_byte == b"\xff":
                marker_byte = stream.read(1)
            if not marker_byte:
                raise ImageHeaderError("truncated JPEG marker")
            marker = marker_byte[0]
            if marker in {0x01, *range(0xD0, 0xD9)}:
                continue
            length_bytes = stream.read(2)
            if len(length_bytes) != 2:
                raise ImageHeaderError("truncated JPEG segment")
            segment_length = struct.unpack(">H", length_bytes)[0]
            if segment_length < 2:
                raise ImageHeaderError("invalid JPEG segment length")
            if marker in _JPEG_SOF_MARKERS:
                payload = stream.read(5)
                if len(payload) != 5:
                    raise ImageHeaderError("truncated JPEG start-of-frame segment")
                height, width = struct.unpack(">HH", payload[1:5])
                if width <= 0 or height <= 0:
                    raise ImageHeaderError("invalid JPEG dimensions")
                return "jpeg", width, height
            stream.seek(segment_length - 2, 1)

"""Minimal streaming PDF writer for bundling scanned JPEG pages.

JPEG data is embedded verbatim as a ``/DCTDecode`` image XObject, so the
scans are not re-encoded and no imaging library is required. Objects are
written in the order they become available and the cross-reference table is
emitted last, which lets callers stream a multi-page document without holding
every page in memory.
"""

from __future__ import annotations

import struct
from typing import Iterator

PAGE_WIDTH_PT = 595.276  # A4 width; height follows the scan's aspect ratio.

# SOF markers that carry frame dimensions (all except DHT C4, JPG C8, DAC CC).
_SOF_MARKERS = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


class InvalidJpegError(ValueError):
    """Raised when data is not a JPEG whose frame header can be read."""


def jpeg_info(data: bytes) -> tuple[int, int, int]:
    """Return (width, height, components) from a JPEG's frame header."""
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        raise InvalidJpegError("missing JPEG SOI marker")
    pos = 2
    length = len(data)
    while pos + 4 <= length:
        if data[pos] != 0xFF:
            raise InvalidJpegError("corrupt marker segment")
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # standalone
            pos += 2
            continue
        if marker == 0xD9 or marker == 0xDA:  # EOI / start of scan: no SOF seen
            break
        seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker in _SOF_MARKERS:
            if pos + 10 > length:
                break
            _precision, height, width, components = struct.unpack(
                ">BHHB", data[pos + 4:pos + 10]
            )
            if width <= 0 or height <= 0 or components not in (1, 3, 4):
                raise InvalidJpegError("unsupported JPEG frame")
            return width, height, components
        pos += 2 + seg_len
    raise InvalidJpegError("no JPEG frame header found")


def _pdf_text_string(value: str) -> bytes:
    """Encode a PDF text string as UTF-16BE hex so CJK titles survive."""
    encoded = "﻿".encode("utf-16-be") + value.encode("utf-16-be")
    return b"<" + encoded.hex().encode("ascii") + b">"


def _escape_literal(value: str) -> bytes:
    out = value.encode("latin-1", "replace")
    return out.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class PdfStreamBuilder:
    """Incrementally emit a PDF; call methods in order and write what they yield."""

    _CATALOG, _PAGES, _INFO = 1, 2, 3

    def __init__(self) -> None:
        self._offset = 0
        self._offsets: dict[int, int] = {}
        self._next_obj = 4
        self._page_ids: list[int] = []

    # -- low level -----------------------------------------------------------

    def _emit(self, chunk: bytes) -> bytes:
        self._offset += len(chunk)
        return chunk

    def _object(self, obj_id: int, body: bytes) -> bytes:
        self._offsets[obj_id] = self._offset
        return self._emit(b"%d 0 obj\n" % obj_id + body + b"\nendobj\n")

    def _alloc(self) -> int:
        obj_id = self._next_obj
        self._next_obj += 1
        return obj_id

    # -- document pieces ------------------------------------------------------

    def begin(self) -> Iterator[bytes]:
        # The binary comment line marks the file as 8-bit per the PDF spec.
        yield self._emit(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def add_jpeg_page(self, jpeg: bytes) -> Iterator[bytes]:
        """Add one full-bleed page showing the JPEG, scaled to A4 width."""
        width, height, components = jpeg_info(jpeg)
        colorspace = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}[components]
        # Adobe-written CMYK JPEGs store inverted values.
        decode = b" /Decode [1 0 1 0 1 0 1 0]" if components == 4 else b""
        page_w = PAGE_WIDTH_PT
        page_h = PAGE_WIDTH_PT * height / width

        image_id, content_id, page_id = self._alloc(), self._alloc(), self._alloc()
        yield self._object(
            image_id,
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode%s /Length %d >>\n"
            b"stream\n" % (width, height, colorspace, decode, len(jpeg))
            + jpeg
            + b"\nendstream",
        )
        content = b"q %.3f 0 0 %.3f 0 0 cm /Im0 Do Q" % (page_w, page_h)
        yield self._object(
            content_id,
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        )
        yield self._object(
            page_id,
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.3f %.3f] "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
            % (self._PAGES, page_w, page_h, image_id, content_id),
        )
        self._page_ids.append(page_id)

    def add_notice_page(self, message: str) -> Iterator[bytes]:
        """Add a plain A4 page carrying a short ASCII notice (e.g. fetch failure)."""
        page_w, page_h = PAGE_WIDTH_PT, PAGE_WIDTH_PT * 1.4142
        font_id, content_id, page_id = self._alloc(), self._alloc(), self._alloc()
        yield self._object(
            font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        )
        content = (
            b"BT /F1 14 Tf 56 %.1f Td (" % (page_h - 80)
            + _escape_literal(message)
            + b") Tj ET"
        )
        yield self._object(
            content_id,
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        )
        yield self._object(
            page_id,
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.3f %.3f] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (self._PAGES, page_w, page_h, font_id, content_id),
        )
        self._page_ids.append(page_id)

    def finish(self, *, title: str, author: str = "", producer: str = "") -> Iterator[bytes]:
        kids = b" ".join(b"%d 0 R" % pid for pid in self._page_ids)
        yield self._object(
            self._PAGES,
            b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(self._page_ids)),
        )
        yield self._object(
            self._CATALOG, b"<< /Type /Catalog /Pages %d 0 R >>" % self._PAGES
        )
        info = b"<< /Title " + _pdf_text_string(title)
        if author:
            info += b" /Author " + _pdf_text_string(author)
        if producer:
            info += b" /Producer " + _pdf_text_string(producer)
        info += b" >>"
        yield self._object(self._INFO, info)

        xref_offset = self._offset
        size = self._next_obj
        lines = [b"xref\n", b"0 %d\n" % size, b"0000000000 65535 f \n"]
        for obj_id in range(1, size):
            lines.append(b"%010d 00000 n \n" % self._offsets[obj_id])
        lines.append(
            b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (size, self._CATALOG, self._INFO, xref_offset)
        )
        yield self._emit(b"".join(lines))

    @property
    def page_count(self) -> int:
        return len(self._page_ids)

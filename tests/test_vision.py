"""Tests for ReadImageTool."""

import asyncio
import base64
import struct
import zlib
import pytest

from coderAI.tools.vision import ReadImageTool


def _make_minimal_png(tmp_path, filename="test.png") -> str:
    """Write a valid 1×1 red PNG and return its path."""
    def _chunk(tag: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return length + tag + data + crc

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1 RGB
    ihdr = _chunk(b"IHDR", ihdr_data)
    raw = b"\x00\xff\x00\x00"  # filter byte + RGB pixel
    idat = _chunk(b"IDAT", zlib.compress(raw))
    iend = _chunk(b"IEND", b"")

    path = tmp_path / filename
    path.write_bytes(signature + ihdr + idat + iend)
    return str(path)


class TestReadImageTool:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.tool = ReadImageTool()

    def test_reads_valid_png(self, tmp_path):
        path = _make_minimal_png(tmp_path)
        result = asyncio.run(self.tool.execute(path=path))
        assert result["success"]
        assert result["mime_type"] == "image/png"
        assert result["_vision"] is True
        # Verify it's valid base64
        decoded = base64.b64decode(result["image_data"])
        assert len(decoded) > 0

    def test_file_not_found(self, tmp_path):
        result = asyncio.run(self.tool.execute(path=str(tmp_path / "nope.png")))
        assert not result["success"]
        assert "not found" in result["error"].lower()

    def test_unsupported_type(self, tmp_path):
        txt_file = tmp_path / "doc.txt"
        txt_file.write_text("not an image")
        result = asyncio.run(self.tool.execute(path=str(txt_file)))
        assert not result["success"]
        assert "Unsupported" in result["error"]

    def test_file_too_large(self, tmp_path, monkeypatch):
        path = _make_minimal_png(tmp_path)
        # Patch the size check to simulate an oversized file
        import coderAI.tools.vision as vision_mod
        monkeypatch.setattr(vision_mod, "MAX_IMAGE_SIZE", 1)
        result = asyncio.run(self.tool.execute(path=path))
        assert not result["success"]
        assert "too large" in result["error"].lower()

    def test_returns_filename(self, tmp_path):
        path = _make_minimal_png(tmp_path, filename="diagram.png")
        result = asyncio.run(self.tool.execute(path=path))
        assert result["success"]
        assert result["file_name"] == "diagram.png"

    def test_returns_file_size(self, tmp_path):
        path = _make_minimal_png(tmp_path)
        result = asyncio.run(self.tool.execute(path=path))
        assert result["success"]
        assert isinstance(result["file_size"], int)
        assert result["file_size"] > 0

"""capture_settled_frame_bytes drops an idle A1 camera's washed-out wake-up frames."""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import camera


def _frame(tag: bytes) -> bytes:
    jpeg = b"\xff\xd8" + tag + b"\xff\xd9"
    return struct.pack("<I", len(jpeg)) + b"\x00" * 12 + jpeg


def _connection(*tags: bytes):
    reader = asyncio.StreamReader()
    for tag in tags:
        reader.feed_data(_frame(tag))
    reader.feed_eof()
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return reader, writer


@pytest.mark.asyncio
async def test_skip_frames_returns_the_frame_after_the_dropped_ones():
    with patch("asyncio.open_connection", AsyncMock(return_value=_connection(b"wake", b"wake2", b"settled"))):
        assert (
            await camera.read_chamber_image_frame("192.0.2.1", "x", timeout=1, skip_frames=2)
            == b"\xff\xd8settled\xff\xd9"
        )


@pytest.mark.asyncio
async def test_default_still_returns_the_first_frame():
    with patch("asyncio.open_connection", AsyncMock(return_value=_connection(b"first", b"second"))):
        assert await camera.read_chamber_image_frame("192.0.2.1", "x", timeout=1) == b"\xff\xd8first\xff\xd9"


@pytest.mark.asyncio
async def test_stream_ending_before_the_settled_frame_is_no_frame():
    with patch("asyncio.open_connection", AsyncMock(return_value=_connection(b"wake"))):
        assert await camera.read_chamber_image_frame("192.0.2.1", "x", timeout=1, skip_frames=2) is None


@pytest.mark.asyncio
async def test_settled_capture_skips_frames_on_a1():
    read = AsyncMock(return_value=b"jpeg")
    with (
        patch.object(camera, "read_chamber_image_frame", read),
        patch.object(camera, "capture_in_flight", return_value=False),
    ):
        assert await camera.capture_settled_frame_bytes("192.0.2.1", "x", "A1", timeout=10) == b"jpeg"
    assert read.await_args.kwargs["skip_frames"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "in_flight"), [("X1C", False), ("A1", True)])
async def test_settled_capture_falls_back_for_rtsp_or_a_capture_in_flight(model, in_flight):
    normal = AsyncMock(return_value=b"normal")
    read = AsyncMock()
    with (
        patch.object(camera, "capture_camera_frame_bytes", normal),
        patch.object(camera, "read_chamber_image_frame", read),
        patch.object(camera, "capture_in_flight", return_value=in_flight),
    ):
        assert await camera.capture_settled_frame_bytes("192.0.2.1", "x", model, timeout=10) == b"normal"
    read.assert_not_awaited()

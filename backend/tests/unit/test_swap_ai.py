import json
from unittest.mock import AsyncMock, MagicMock, patch

import cv2
import httpx
import numpy as np
import pytest
from pydantic import ValidationError

from backend.app.models.settings import Settings
from backend.app.schemas.settings import AppSettings, AppSettingsUpdate
from backend.app.services.swap_ai import PROMPT, ask_plate_empty, crop_plate, load_swap_ai_settings


def _jpeg(w=1280, h=720) -> bytes:
    img = np.zeros((h, w, 3), np.uint8)
    img[:, : w // 2] = (0, 0, 255)  # left half red (BGR)
    return cv2.imencode(".jpg", img)[1].tobytes()


def _client(*, response=None, exc=None):
    c = MagicMock()
    c.post = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=response)
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    return c


def _resp(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://x/api/generate"))


async def _ask(client):
    with patch("backend.app.services.swap_ai.httpx.AsyncClient", return_value=client):
        return await ask_plate_empty("http://ollama:11434/", "m", b"img", 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_valid_json(flag):
    v = await _ask(_client(response=_resp({"response": json.dumps({"object_on_plate": flag, "reason": "r"})})))
    assert v.object_on_plate is flag and v.reason == "r" and v.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _resp({"response": json.dumps({"object_on_plate": "true", "reason": "r"})}),
        _resp({"response": json.dumps({"reason": "r"})}),
        _resp({"response": "not json"}),
        _resp({"error": "x"}, status=500),
    ],
)
async def test_unusable_answers(response):
    v = await _ask(_client(response=response))
    assert v.object_on_plate is None and v.error


@pytest.mark.asyncio
async def test_timeout():
    v = await _ask(_client(exc=httpx.ReadTimeout("slow")))
    assert v.object_on_plate is None and v.error


@pytest.mark.asyncio
async def test_request_body():
    c = _client(response=_resp({"response": '{"object_on_plate": false}'}))
    await _ask(c)
    url = c.post.call_args.args[0]
    body = c.post.call_args.kwargs["json"]
    assert url == "http://ollama:11434/api/generate"
    assert body["prompt"] == PROMPT and body["model"] == "m"
    assert body["format"] == "json" and body["stream"] is False
    assert body["options"]["temperature"] == 0 and body["keep_alive"] == -1
    assert body["images"] == ["aW1n"]


def test_crop_roi_and_scale():
    out = cv2.imdecode(np.frombuffer(crop_plate(_jpeg(), (0.0, 0.0, 0.5, 1.0)), np.uint8), cv2.IMREAD_COLOR)
    assert max(out.shape[:2]) == 768
    assert out[..., 2].mean() > 200 and out[..., 0].mean() < 60  # only the red half


def test_crop_roi_dimensions():
    out = cv2.imdecode(np.frombuffer(crop_plate(_jpeg(), (0.25, 0.25, 0.5, 0.5)), np.uint8), cv2.IMREAD_COLOR)
    assert out.shape[:2] == (432, 768)  # 640x360 scaled to 768 wide


def test_crop_default_lower_half():
    out = cv2.imdecode(np.frombuffer(crop_plate(_jpeg(), None), np.uint8), cv2.IMREAD_COLOR)
    assert out.shape[:2] == (216, 768)  # 1280x360 scaled to 768 wide


@pytest.mark.asyncio
async def test_load_settings_defaults_and_values(db_session):
    s = await load_swap_ai_settings(db_session)
    assert (s.mode, s.url, s.model, s.timeout_s, s.max_wait_s) == ("shadow", "", "qwen2.5vl:7b", 60, 300)
    for k, v in {"swap_ai_mode": "enforce", "swap_ai_url": "http://h:1/", "swap_ai_timeout_s": "30"}.items():
        db_session.add(Settings(key=k, value=v))
    await db_session.commit()
    s = await load_swap_ai_settings(db_session)
    assert (s.mode, s.url, s.timeout_s) == ("enforce", "http://h:1", 30)


def test_schema_defaults_and_validation():
    d = AppSettings()
    assert (d.swap_ai_mode, d.swap_ai_url, d.swap_ai_model, d.swap_ai_timeout_s, d.swap_ai_max_wait_s) == (
        "shadow",
        "",
        "qwen2.5vl:7b",
        60,
        300,
    )
    with pytest.raises(ValidationError):
        AppSettingsUpdate(swap_ai_mode="bogus")
    with pytest.raises(ValidationError):
        AppSettingsUpdate(swap_ai_url="http://169.254.169.254/")
    assert AppSettingsUpdate(swap_ai_mode="off", swap_ai_url="http://192.168.1.5:11434")

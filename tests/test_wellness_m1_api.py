"""M1 健康 API 真正路由验收：离线、临时 HOME、仅使用 mock 估算器。"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest

from nightshift import wellness, wellness_estimator, wellness_store
from nightshift.__main__ import main


def _json_call(monkeypatch, method: str, path: str, payload: dict | None = None):
    raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    return _raw_call(monkeypatch, method, path, raw, "application/json")


def _raw_call(monkeypatch, method: str, path: str, raw: bytes, content_type: str):
    """用裸 Handler 走完整路由和真实持久化，不申请监听端口。"""
    handler = wellness._Handler.__new__(wellness._Handler)
    handler.path = path
    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": content_type}
    handler.rfile = BytesIO(raw)
    captured: dict = {}
    handler._send = lambda status, body, content_type="application/json; charset=utf-8": captured.update(
        status=status, body=body, content_type=content_type
    )
    handler._send_raw = lambda status, body, content_type: captured.update(
        status=status, raw=body, content_type=content_type
    )
    handler._route(method)
    return captured


def _dispatch_error(monkeypatch, method: str, path: str, raw: bytes, content_type: str):
    handler = wellness._Handler.__new__(wellness._Handler)
    handler.path = path
    handler.headers = {"Content-Length": str(len(raw)), "Content-Type": content_type}
    handler.rfile = BytesIO(raw)
    captured: dict = {}
    handler._send = lambda status, body, content_type="application/json; charset=utf-8": captured.update(
        status=status, body=body, content_type=content_type
    )
    handler._dispatch(method)
    return captured


def _item(name: str = "米饭", kcal: int = 260) -> dict:
    return {
        "name": name, "portion": "一碗", "unit": "碗", "kcal_low": kcal - 40,
        "kcal_high": kcal + 40, "kcal_best": kcal, "protein_g": 5,
        "carbs_g": 58, "fat_g": 1, "confidence": 0.8, "note": "常见份量",
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    return tmp_path


def test_settings_route_hides_key_and_roundtrips_retain_photos(home, monkeypatch):
    put = _json_call(monkeypatch, "PUT", "/health/api/settings", {
        "provider": "openai_compatible", "retain_photos": True,
        "providers": {"openai_compatible": {
            "model": "offline-model", "base_url": "http://127.0.0.1:9",
            "api_key": "secret-for-test",
        }},
    })
    assert put["status"] == 200
    get = _json_call(monkeypatch, "GET", "/health/api/settings")
    rendered = json.dumps(get["body"], ensure_ascii=False)
    assert get["body"]["data"]["retain_photos"] is True
    assert get["body"]["data"]["providers"]["openai_compatible"]["has_api_key"] is True
    assert "secret-for-test" not in rendered

    # 脱敏 GET 后只提交 provider 仍须保留磁盘中的 key。
    _json_call(monkeypatch, "PUT", "/health/api/settings", {
        "provider": "openai_compatible", "retain_photos": False,
        "providers": {"openai_compatible": {"model": "offline-model"}},
    })
    saved = wellness_estimator.load_settings()
    assert saved["providers"]["openai_compatible"]["api_key"] == "secret-for-test"
    assert saved["retain_photos"] is False

    invalid = _dispatch_error(
        monkeypatch, "PUT", "/health/api/settings",
        json.dumps({"provider": "mock", "providers": {"mock": {"timeout_s": 0}}}).encode(),
        "application/json",
    )
    assert invalid["status"] == 400 and "timeout_s" in invalid["body"]["error"]["message"]


def test_text_estimate_edit_and_batch_save_changes_summary(home, monkeypatch):
    calls = []

    def fake_estimate(text=None, image_path=None, profile=None, settings=None):
        calls.append({"text": text, "image_path": image_path, "profile": profile, "settings": settings})
        return {"items": [_item()], "questions": [], "provider": "mock", "model": "offline", "elapsed_ms": 1}

    monkeypatch.setattr(wellness_estimator, "estimate", fake_estimate)
    estimated = _json_call(monkeypatch, "POST", "/health/api/estimate", {"text": "午饭一碗米饭"})
    assert estimated["status"] == 200
    estimate = estimated["body"]["data"]
    assert estimate["items"][0]["name"] == "米饭"
    assert calls[0]["text"] == "午饭一碗米饭"
    original = dict(estimate)
    original["items"] = list(estimate["items"])
    entry = dict(estimate["items"][0])
    entry.update({"type": "meal", "name": "米饭（改份量）", "portion": "半碗", "calories": 130,
                  "source": "text", "provider": estimate["provider"], "model": estimate["model"]})
    saved = _json_call(monkeypatch, "POST", "/health/api/days/2026-09-07/entries", {
        "source": "text", "estimate": original, "entries": [entry],
    })
    assert saved["status"] == 201
    record = saved["body"]["data"]["entries"][0]
    assert record["portion"] == "半碗" and record["calories"] == 130
    assert record["source"] == "text" and record["provider"] == "mock" and record["model"] == "offline"
    assert record["estimate"] == original
    assert record["protein_g"] == 5 and record["carbs_g"] == 58 and record["fat_g"] == 1
    summary = _json_call(monkeypatch, "GET", "/health/api/days/2026-09-07/summary")
    assert summary["body"]["data"]["calories_in"] == 130


def _multipart(text: str | None, image: bytes, boundary: str = "m1-boundary") -> tuple[bytes, str]:
    parts = []
    if text is not None:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{text}\r\n").encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"meal.png\"\r\nContent-Type: image/png\r\n\r\n").encode() + image + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _png(size: tuple[int, int] = (2000, 1000)) -> bytes:
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", size, (80, 140, 100)).save(output, format="PNG")
    return output.getvalue()


def test_multipart_image_compresses_and_cleans_temp_by_default(home, monkeypatch):
    seen: dict = {}

    def fake_estimate(text=None, image_path=None, profile=None, settings=None):
        path = Path(image_path)
        seen["path"] = path
        from PIL import Image
        with Image.open(path) as image:
            seen["size"] = image.size
            seen["format"] = image.format
        return {"items": [_item()], "questions": [], "provider": "mock", "model": "m", "elapsed_ms": 1}

    monkeypatch.setattr(wellness_estimator, "estimate", fake_estimate)
    raw, content_type = _multipart("午餐", _png())
    result = _raw_call(monkeypatch, "POST", "/health/api/estimate", raw, content_type)
    assert result["status"] == 200 and seen["size"] == (1280, 640) and seen["format"] == "JPEG"
    assert not list((home / "wellness" / "tmp").glob("*"))
    assert not list((home / "wellness" / "photos").glob("*"))

    _json_call(monkeypatch, "PUT", "/health/api/settings", {"provider": "mock", "retain_photos": True, "providers": {}})
    result = _raw_call(monkeypatch, "POST", "/health/api/estimate", raw, content_type)
    assert result["status"] == 200
    photos = list((home / "wellness" / "photos").glob("*.jpg"))
    assert len(photos) == 1
    from PIL import Image
    with Image.open(photos[0]) as image:
        assert image.size == (1280, 640) and image.format == "JPEG"


def test_image_path_is_not_http_input_and_bad_or_oversize_images_are_readable(home, monkeypatch):
    def fail_if_called(**kwargs):
        raise AssertionError("HTTP 不应把任意 image_path 交给估算器")

    monkeypatch.setattr(wellness_estimator, "estimate", fail_if_called)
    path_payload = {"image_path": str(home / "outside.jpg")}
    error = _dispatch_error(monkeypatch, "POST", "/health/api/estimate", json.dumps(path_payload).encode(), "application/json")
    assert error["status"] == 400 and "图片" in error["body"]["error"]["message"]
    huge = b"x" * (wellness.MAX_IMAGE_BYTES + 1)
    error = _dispatch_error(monkeypatch, "POST", "/health/api/estimate", json.dumps({"image_base64": base64.b64encode(huge).decode()}).encode(), "application/json")
    assert error["status"] == 413 and "过大" in error["body"]["error"]["message"]
    raw, content_type = _multipart(None, b"not-an-image")
    error = _dispatch_error(monkeypatch, "POST", "/health/api/estimate", raw, content_type)
    assert error["status"] == 400 and "图片" in error["body"]["error"]["message"]


def test_batch_validation_is_atomic(home, monkeypatch):
    payload = {"source": "manual", "entries": [
        {"type": "meal", "name": "先写入", "calories": 100},
        {"type": "meal", "name": "坏热量", "calories": -1},
    ]}
    error = _dispatch_error(monkeypatch, "POST", "/health/api/days/2026-09-07/entries", json.dumps(payload).encode(), "application/json")
    assert error["status"] == 400
    assert wellness_store.get_store().list_entries("2026-09-07") == []


def test_range_report_and_cli_share_store_numbers_across_month(home, monkeypatch, capsys):
    store = wellness_store.get_store()
    store.save_profile({"calorie_target": 1800})
    store.create_entry("2026-01-31", {"type": "meal", "name": "米饭", "calories": 500, "protein_g": 10})
    store.create_entry("2026-02-01", {"type": "meal", "name": "面条", "calories": 600})
    store.create_entry("2026-02-01", {"type": "exercise", "name": "步行", "calories_burned": 200})
    ranged = _json_call(monkeypatch, "GET", "/health/api/range?from=2026-01-31&to=2026-02-01")
    expected = store.range_summary("2026-01-31", "2026-02-01", profile={"calorie_target": 1800})
    assert ranged["body"]["data"]["totals"] == expected["totals"]
    assert [day["date"] for day in ranged["body"]["data"]["days"]] == ["2026-01-31", "2026-02-01"]
    report = _json_call(monkeypatch, "GET", "/health/api/report?period=week&end=2026-02-01&format=json")
    assert report["body"]["data"]["totals"] == store.report("week", "2026-02-01", "json", profile={"calorie_target": 1800})["totals"]
    markdown = _raw_call(monkeypatch, "GET", "/health/api/report?period=week&end=2026-02-01&format=md", b"", "application/json")
    assert "2026-01-31" in markdown["raw"].decode() and "500.0" in markdown["raw"].decode()
    assert main(["wellness-report", "--days", "7", "--end", "2026-02-01", "--format", "json"]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["totals"] == report["body"]["data"]["totals"]

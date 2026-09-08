"""食物估算适配器专项测试；所有后端均离线模拟。"""

import json
from pathlib import Path

import pytest

from nightshift import wellness_estimator as estimator


def _item():
    return {
        "name": "米饭", "portion": "一碗", "unit": "碗", "kcal_low": 220,
        "kcal_high": 300, "kcal_best": 260, "protein_g": 5,
        "carbs_g": 58, "fat_g": 0.5, "confidence": 0.8, "note": "常见饭碗",
    }


def _result():
    return {"items": [_item()], "questions": []}


def test_schema_validation_and_bad_fields():
    value = estimator.validate_result(_result(), provider="mock", model="mock", elapsed_ms=4)
    assert value["items"][0]["kcal_best"] == 260.0
    assert value["provider"] == "mock"
    with pytest.raises(estimator.EstimatorError, match="缺少字段"):
        estimator.validate_result({"items": [{"name": "饭"}], "questions": []})
    bad = _result()
    bad["items"][0]["confidence"] = 2
    with pytest.raises(estimator.EstimatorError):
        estimator.validate_result(bad)


def test_settings_are_atomic_and_public_view_hides_key(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    saved = estimator.save_settings({
        "provider": "openai_compatible",
        "providers": {"openai_compatible": {
            "base_url": "http://localhost:9000", "model": "local-model", "api_key": "private-test-key",
        }},
    })
    assert saved["providers"]["openai_compatible"]["api_key"] == "private-test-key"
    public = estimator.public_settings()
    rendered = json.dumps(public, ensure_ascii=False)
    assert "private-test-key" not in rendered
    assert public["providers"]["openai_compatible"]["has_api_key"] is True
    assert not list((tmp_path / "wellness").glob("*.tmp"))
    estimator.update_settings({"provider": "mock"})
    assert estimator.load_settings()["providers"]["openai_compatible"]["api_key"] == "private-test-key"


def test_retain_photos_and_settings_type_timeout_validation(tmp_path):
    estimator.save_settings({"retain_photos": True}, root=tmp_path)
    assert estimator.load_settings(root=tmp_path)["retain_photos"] is True
    for invalid in ("yes", 1, None):
        with pytest.raises(estimator.SettingsError, match="retain_photos"):
            estimator.save_settings({"retain_photos": invalid}, root=tmp_path)
    for timeout in (0, 0.5, 601, "60", float("nan")):
        with pytest.raises(estimator.SettingsError, match="timeout_s"):
            estimator.save_settings({"providers": {"mock": {"timeout_s": timeout}}}, root=tmp_path)
    with pytest.raises(estimator.SettingsError, match="model.*字符串"):
        estimator.save_settings({"providers": {"mock": {"model": 123}}}, root=tmp_path)


def test_prompt_contains_profile_and_absolute_image(tmp_path):
    image = tmp_path / "meal.jpg"
    image.write_bytes(b"jpeg")
    prompt = estimator.build_prompt("午饭一碗米饭", image, {"weight_kg": 70, "goal": "减脂"})
    assert str(image.resolve()) in prompt
    assert "用户档案摘要" in prompt
    assert "区间" in prompt


def test_claude_driver_command_and_fenced_json(monkeypatch, tmp_path):
    image = tmp_path / "meal.jpg"
    image.write_bytes(b"jpeg")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        seen["input"] = kwargs.get("input")
        class Completed:
            returncode = 0
            stdout = "```json\n" + json.dumps(_result(), ensure_ascii=False) + "\n```"
            stderr = ""
        return Completed()

    monkeypatch.setattr(estimator.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDECODE", "nested")
    output = estimator.ClaudeEstimator({"model": "claude-test", "timeout_s": 3}).run("识别", image)
    assert output["items"][0]["name"] == "米饭"
    assert seen["command"][0] == "claude"
    assert "--output-format" in seen["command"]
    assert seen["command"][seen["command"].index("--add-dir") + 1] == str(image.parent)
    assert "--tools" in seen["command"] and seen["command"][seen["command"].index("--tools") + 1] == "Read"
    assert "--safe-mode" in seen["command"] and "--permission-prompts" in seen["command"]
    assert "--restricted" in seen["command"]
    assert seen["input"].startswith("识别") and str(image) in seen["input"]
    assert "CLAUDECODE" not in seen["env"]


def test_claude_non_json_output_is_readable_error(monkeypatch):
    seen = {}
    class Completed:
        returncode = 0
        stdout = "模型说了一段话，但没有 JSON"
        stderr = ""
    def fake_run(command, **kwargs):
        seen["command"] = command
        return Completed()
    monkeypatch.setattr(estimator.subprocess, "run", fake_run)
    with pytest.raises(estimator.EstimatorError, match="JSON"):
        estimator.ClaudeEstimator({"timeout_s": 1}).run("识别")
    tools = seen["command"].index("--tools")
    assert seen["command"][tools + 1] == "" and "--add-dir" not in seen["command"]


def test_cli_nested_json_and_error_envelopes_are_readable():
    nested = json.dumps({"result": json.dumps({"output": json.dumps(_result(), ensure_ascii=False)}, ensure_ascii=False)})
    assert estimator._model_json(nested)["items"][0]["name"] == "米饭"
    failed = json.dumps({"type": "result", "is_error": True, "result": "认证不可用"}, ensure_ascii=False)
    with pytest.raises(estimator.EstimatorError, match="认证不可用"):
        estimator._model_json(failed)


def test_codex_driver_image_and_timeout(monkeypatch, tmp_path):
    image = tmp_path / "meal.jpg"
    image.write_bytes(b"jpeg")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["input"] = kwargs.get("input")
        class Completed:
            returncode = 0
            stdout = json.dumps({"result": json.dumps(_result(), ensure_ascii=False)})
            stderr = ""
        return Completed()

    monkeypatch.setattr(estimator.subprocess, "run", fake_run)
    output = estimator.CodexEstimator({"model": "gpt-test", "timeout_s": 9}).run("识别", image)
    assert output["items"]
    assert seen["command"][:4] == ["codex", "exec", "--sandbox", "read-only"]
    assert "-i" in seen["command"] and str(image) in seen["command"]
    assert seen["command"][seen["command"].index("-m") + 1] == "gpt-test"
    assert seen["command"].index("-") < seen["command"].index("-i")
    assert seen["input"] == "识别"

    def timeout(*args, **kwargs):
        raise estimator.subprocess.TimeoutExpired(kwargs.get("timeout"), "")
    monkeypatch.setattr(estimator.subprocess, "run", timeout)
    with pytest.raises(estimator.EstimatorError, match="超时"):
        estimator.CodexEstimator({"timeout_s": 1}).run("识别")


def test_codex_missing_item_field_is_rejected(monkeypatch):
    class Completed:
        returncode = 0
        stdout = json.dumps({"items": [{"name": "米饭"}], "questions": []})
        stderr = ""
    monkeypatch.setattr(estimator.subprocess, "run", lambda *args, **kwargs: Completed())
    output = estimator.CodexEstimator({"timeout_s": 1}).run("识别")
    with pytest.raises(estimator.EstimatorError, match="缺少字段"):
        estimator.validate_result(output)


def test_openai_compatible_payload_and_bad_json(monkeypatch, tmp_path):
    image = tmp_path / "meal.jpg"
    image.write_bytes(b"jpeg")
    seen = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"choices": [{"message": {"content": json.dumps(_result(), ensure_ascii=False)}}]}).encode()

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(estimator.urllib.request, "urlopen", fake_urlopen)
    output = estimator.OpenAICompatibleEstimator({
        "base_url": "http://localhost:9000", "model": "local", "api_key": "secret", "timeout_s": 2,
    }).run("识别", image)
    assert output["items"]
    payload = json.loads(seen["request"].data)
    assert payload["messages"][0]["content"][1]["type"] == "image_url"
    assert "secret" not in json.dumps(payload)
    assert seen["request"].full_url.endswith("/v1/chat/completions")


@pytest.mark.parametrize("outer", [b"not json", b'{"object":"chat.completion"}'])
def test_openai_compatible_bad_outer_json_or_missing_choices(monkeypatch, outer):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return outer
    monkeypatch.setattr(estimator.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(estimator.EstimatorError):
        estimator.OpenAICompatibleEstimator({"base_url": "http://localhost:9000", "model": "local"}).run("识别")


def test_openai_compatible_timeout_is_not_silently_accepted(monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("offline timeout")
    monkeypatch.setattr(estimator.urllib.request, "urlopen", timeout)
    with pytest.raises(estimator.EstimatorError, match="连接失败"):
        estimator.OpenAICompatibleEstimator({"base_url": "http://localhost:9000", "model": "local", "timeout_s": 1}).run("识别")


def test_estimate_mock_and_failures_are_safe(tmp_path, monkeypatch):
    result = estimator.estimate("午饭", settings={"provider": "mock"})
    assert result["provider"] == "mock" and result["items"]
    missing = estimator.estimate(None, settings={"provider": "mock"})
    assert missing["items"] == [] and "error" in missing
    huge = tmp_path / "huge.jpg"
    huge.write_bytes(b"x" * (estimator.MAX_IMAGE_BYTES + 1))
    failed = estimator.estimate(image_path=huge, settings={"provider": "mock"})
    assert "10 MB" in failed["error"]


def test_mock_bad_output_becomes_unified_readable_error(monkeypatch):
    monkeypatch.setattr(estimator.MockEstimator, "run", lambda self, prompt, image_path=None: {"questions": []})
    failed = estimator.estimate("午饭", settings={"provider": "mock"})
    assert failed["items"] == []
    assert "error" in failed and "items" in failed["error"]

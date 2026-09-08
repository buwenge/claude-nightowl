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
    schema = json.loads(seen["command"][seen["command"].index("--json-schema") + 1])
    assert set(estimator._ITEM_FIELDS) <= set(schema["properties"]["items"]["items"]["required"])
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
        command = args[0]
        schema_path = Path(command[command.index("--output-schema") + 1])
        seen["timeout_schema_path"] = schema_path
        seen["timeout_schema_exists"] = schema_path.is_file()
        raise estimator.subprocess.TimeoutExpired(kwargs.get("timeout"), "")
    monkeypatch.setattr(estimator.subprocess, "run", timeout)
    with pytest.raises(estimator.EstimatorError, match="超时"):
        estimator.CodexEstimator({"timeout_s": 1}).run("识别")
    assert seen["timeout_schema_exists"]
    assert not seen["timeout_schema_path"].exists()


def test_codex_driver_output_schema_covers_text_and_image(monkeypatch, tmp_path):
    """Codex 文字和图片请求都传入公开 schema，并在调用后清理临时文件。"""
    image = tmp_path / "meal.jpg"
    image.write_bytes(b"jpeg")
    calls = []

    def fake_run(command, **kwargs):
        schema_index = command.index("--output-schema")
        schema_path = Path(command[schema_index + 1])
        calls.append({
            "command": command,
            "schema_path": schema_path,
            "schema_exists": schema_path.is_file(),
            "schema": json.loads(schema_path.read_text(encoding="utf-8")),
            "input": kwargs.get("input"),
        })

        class Completed:
            returncode = 0
            stdout = json.dumps({"result": json.dumps(_result(), ensure_ascii=False)})
            stderr = ""

        return Completed()

    monkeypatch.setattr(estimator.subprocess, "run", fake_run)
    driver = estimator.CodexEstimator({"model": "gpt-test", "timeout_s": 9})
    assert driver.run("识别图片", image)["items"]
    assert driver.run("识别文字")["items"]

    assert len(calls) == 2
    assert all(call["schema_exists"] for call in calls)
    assert all(call["schema"] == estimator.CODEX_OUTPUT_SCHEMA for call in calls)
    assert set(estimator.CODEX_OUTPUT_SCHEMA["properties"]) == set(estimator.CODEX_OUTPUT_SCHEMA["required"])
    assert set(estimator.CODEX_OUTPUT_SCHEMA["properties"]["items"]["items"]["properties"]) == set(
        estimator.CODEX_OUTPUT_SCHEMA["properties"]["items"]["items"]["required"]
    )
    assert all(not call["schema_path"].exists() for call in calls)
    assert str(image) in calls[0]["command"]
    assert str(image) not in calls[1]["command"]
    assert calls[0]["input"] == "识别图片"
    assert calls[1]["input"] == "识别文字"


def test_process_failure_stderr_is_bounded_and_redacted(monkeypatch):
    """子进程失败只保留定长尾部，不能泄露提示词、路径或会话标识。"""
    prompt = "这是不应回显的敏感提示词"
    private_path = "/tmp/private-project/meal.jpg"
    stderr = (
        f"{prompt}\n{private_path}\n"
        + "前部噪声 " * 100
        + "fatal: upstream request failed; session_id=deadbeef-1234-5678-90ab-cdef12345678"
    )

    class Completed:
        returncode = 1
        stdout = ""

    completed = Completed()
    completed.stderr = stderr

    monkeypatch.setattr(estimator.subprocess, "run", lambda *args, **kwargs: completed)
    with pytest.raises(estimator.EstimatorError) as caught:
        estimator.ClaudeEstimator({"timeout_s": 1}).run(prompt)
    message = str(caught.value)
    assert prompt not in message
    assert private_path not in message
    assert "fatal: upstream request failed" in message
    assert "deadbeef-1234-5678-90ab-cdef12345678" not in message
    assert len(message) <= estimator._ERROR_DETAIL_LIMIT + 20


def test_timeout_stderr_is_bounded_and_redacted(monkeypatch):
    """超时摘要同样不能泄露输入、路径或会话标识。"""
    prompt = "超时场景中的敏感提示词"
    private_path = "/tmp/private-project/timeout-image.jpg"
    session_id = "11111111-2222-3333-4444-555555555555"
    stderr = (
        f"{prompt}\n{private_path}\n"
        + "前部诊断噪声 " * 100
        + f"fatal: upstream timeout; session_id={session_id}; request_id=req-private"
    )

    def timeout(*args, **kwargs):
        raise estimator.subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr=stderr)

    monkeypatch.setattr(estimator.subprocess, "run", timeout)
    with pytest.raises(estimator.EstimatorError) as caught:
        estimator.ClaudeEstimator({"timeout_s": 1}).run(prompt)
    message = str(caught.value)
    assert prompt not in message
    assert private_path not in message
    assert session_id not in message
    assert "request_id=req-private" not in message
    assert "fatal: upstream timeout" in message
    assert len(message) <= estimator._ERROR_DETAIL_LIMIT + 40


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


def _assert_unified_failure(value, provider, model):
    """四种后端的错误都必须经过统一入口收敛成同一种返回。"""
    assert value["items"] == []
    assert value["provider"] == provider
    assert value["model"] == model
    assert isinstance(value.get("error"), str) and value["error"].strip()


def _completed(stdout):
    class Completed:
        returncode = 0
        stderr = ""
        # subprocess.run(text=True) 时 stdout 是字符串。
        pass
    result = Completed()
    result.stdout = stdout
    return result


@pytest.mark.parametrize("bad_output", ["不是 JSON", "[]"], ids=["非JSON", "非对象"])
def test_claude_bad_outputs_through_unified_entry(monkeypatch, bad_output):
    """Claude 的非 JSON 与非对象输出不能绕过统一错误 envelope。"""
    monkeypatch.setattr(estimator.subprocess, "run", lambda *args, **kwargs: _completed(bad_output))
    settings = {"provider": "claude", "providers": {"claude": {"model": "claude-test"}}}
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "claude", "claude-test")


def test_claude_timeout_through_unified_entry(monkeypatch):
    def timeout(*args, **kwargs):
        raise estimator.subprocess.TimeoutExpired(kwargs["timeout"], "离线超时")
    monkeypatch.setattr(estimator.subprocess, "run", timeout)
    settings = {"provider": "claude", "providers": {"claude": {"model": "claude-test"}}}
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "claude", "claude-test")
    assert "超时" in value["error"]


def test_claude_missing_item_field_through_unified_entry(monkeypatch):
    output = json.dumps({"items": [{"name": "米饭"}], "questions": []})
    monkeypatch.setattr(estimator.subprocess, "run", lambda *args, **kwargs: _completed(output))
    settings = {"provider": "claude", "providers": {"claude": {"model": "claude-test"}}}
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "claude", "claude-test")
    assert "缺少字段" in value["error"]


@pytest.mark.parametrize("bad_output", ["不是 JSON", "[]"], ids=["非JSON", "非对象"])
def test_codex_bad_outputs_through_unified_entry(monkeypatch, bad_output):
    """Codex 的命令输出异常同样由 estimate 统一处理。"""
    monkeypatch.setattr(estimator.subprocess, "run", lambda *args, **kwargs: _completed(bad_output))
    settings = {"provider": "codex", "providers": {"codex": {"model": "codex-test"}}}
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "codex", "codex-test")


def test_codex_timeout_through_unified_entry(monkeypatch):
    def timeout(*args, **kwargs):
        raise estimator.subprocess.TimeoutExpired(kwargs["timeout"], "离线超时")
    monkeypatch.setattr(estimator.subprocess, "run", timeout)
    settings = {"provider": "codex", "providers": {"codex": {"model": "codex-test"}}}
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "codex", "codex-test")
    assert "超时" in value["error"]


def test_codex_missing_item_field_through_unified_entry(monkeypatch):
    output = json.dumps({"items": [{"name": "米饭"}], "questions": []})
    monkeypatch.setattr(estimator.subprocess, "run", lambda *args, **kwargs: _completed(output))
    settings = {"provider": "codex", "providers": {"codex": {"model": "codex-test"}}}
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "codex", "codex-test")
    assert "缺少字段" in value["error"]


class _OfflineResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


@pytest.mark.parametrize("body", ["不是 JSON".encode(), b"[]"], ids=["非JSON", "非对象"])
def test_openai_bad_outer_outputs_through_unified_entry(monkeypatch, body):
    monkeypatch.setattr(estimator.urllib.request, "urlopen", lambda *args, **kwargs: _OfflineResponse(body))
    settings = {
        "provider": "openai_compatible",
        "providers": {"openai_compatible": {"base_url": "http://localhost:9000", "model": "http-test"}},
    }
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "openai_compatible", "http-test")


def test_openai_timeout_through_unified_entry(monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("离线超时")
    monkeypatch.setattr(estimator.urllib.request, "urlopen", timeout)
    settings = {
        "provider": "openai_compatible",
        "providers": {"openai_compatible": {"base_url": "http://localhost:9000", "model": "http-test"}},
    }
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "openai_compatible", "http-test")
    assert "连接失败" in value["error"]


def test_openai_missing_item_field_through_unified_entry(monkeypatch):
    content = json.dumps({"items": [{"name": "米饭"}], "questions": []})
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    monkeypatch.setattr(estimator.urllib.request, "urlopen", lambda *args, **kwargs: _OfflineResponse(body))
    settings = {
        "provider": "openai_compatible",
        "providers": {"openai_compatible": {"base_url": "http://localhost:9000", "model": "http-test"}},
    }
    value = estimator.estimate("午饭", settings=settings)
    _assert_unified_failure(value, "openai_compatible", "http-test")
    assert "缺少字段" in value["error"]


@pytest.mark.parametrize("bad_output", ["不是 JSON", []], ids=["非JSON", "非对象"])
def test_mock_bad_outputs_through_unified_entry(monkeypatch, bad_output):
    monkeypatch.setattr(estimator.MockEstimator, "run", lambda self, prompt, image_path=None: bad_output)
    value = estimator.estimate("午饭", settings={"provider": "mock", "providers": {"mock": {"model": "mock-test"}}})
    _assert_unified_failure(value, "mock", "mock-test")


def test_mock_timeout_through_unified_entry(monkeypatch):
    def timeout(*args, **kwargs):
        raise estimator.EstimatorError("mock 超时")
    monkeypatch.setattr(estimator.MockEstimator, "run", timeout)
    value = estimator.estimate("午饭", settings={"provider": "mock", "providers": {"mock": {"model": "mock-test"}}})
    _assert_unified_failure(value, "mock", "mock-test")
    assert "超时" in value["error"]


def test_mock_missing_item_field_through_unified_entry(monkeypatch):
    monkeypatch.setattr(
        estimator.MockEstimator, "run",
        lambda self, prompt, image_path=None: {"items": [{"name": "米饭"}], "questions": []},
    )
    value = estimator.estimate("午饭", settings={"provider": "mock", "providers": {"mock": {"model": "mock-test"}}})
    _assert_unified_failure(value, "mock", "mock-test")
    assert "缺少字段" in value["error"]

import pytest

from model_switch_mcp import validate
from model_switch_mcp.validate import ValidationError


def test_unknown_runner_lists_configured(runners):
    with pytest.raises(ValidationError) as e:
        validate.runner_of(runners, "mini")
    assert e.value.detail["configured"] == ["murderbot"]


def test_unknown_engine_lists_enabled(runners):
    with pytest.raises(ValidationError) as e:
        validate.engine_of(runners["murderbot"], "vllm")
    assert e.value.detail["enabled"] == ["llamacpp", "ninfer", "ollama"]


@pytest.mark.parametrize("bad", ["", "no-slash", "a/b/c", "a/../b", "a b/c", "-x/y", "a/" + "b" * 200])
def test_bad_repo(bad):
    with pytest.raises(ValidationError):
        validate.repo(bad)


def test_good_repo():
    assert validate.repo("Qwen/Qwen3-0.6B-GGUF") == "Qwen/Qwen3-0.6B-GGUF"


@pytest.mark.parametrize("bad", ["../x", "/abs", "a" * 300, "x;rm"])
def test_bad_file(bad):
    with pytest.raises(ValidationError):
        validate.file_pattern(bad)


def test_lease_clamped_and_default():
    assert validate.minutes(None, 30, 120, "lease") == 30
    assert validate.minutes(500, 30, 120, "lease") == 120
    with pytest.raises(ValidationError):
        validate.minutes(-1, 30, 120, "lease")
    with pytest.raises(ValidationError):
        validate.minutes(True, 30, 120, "lease")


@pytest.mark.parametrize("flag", ["-m", "--model", "-hf", "--hf-repo", "--model-url", "-hf=a/b", "--hf-token"])
def test_blocked_flags(flag):
    with pytest.raises(ValidationError) as e:
        validate.args(["--threads", "3", flag, "x"])
    assert e.value.error == "blocked_flag"


def test_args_must_be_clean_strings():
    assert validate.args(["--threads", "3"]) == ["--threads", "3"]
    for bad in (["a\nb"], [1], ["x" * 2000], ["a"] * 65):
        with pytest.raises(ValidationError):
            validate.args(bad)


def test_env_rules():
    assert validate.env({"OLLAMA_FLASH_ATTENTION": "1"}) == {"OLLAMA_FLASH_ATTENTION": "1"}
    for bad in ({"lower": "x"}, {"NVIDIA_VISIBLE_DEVICES": "none"}, {"HF_TOKEN": "x"}, {"LD_PRELOAD": "/x"}, {"A": 1}):
        with pytest.raises(ValidationError):
            validate.env(bad)


def test_context_bounds():
    assert validate.context(None) == 4096
    for bad in (10, 10**9, "4096"):
        with pytest.raises(ValidationError):
            validate.context(bad)

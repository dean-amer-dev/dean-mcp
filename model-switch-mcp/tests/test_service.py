import pytest

from conftest import STAGED, make
from model_switch_mcp import service, validate


def test_switch_ready(runners):
    sw, kube = make(runners)
    out = sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF", ctx=6144, args=["--threads", "3"], lease_minutes=4)
    assert out["ok"] and out["state"] == "ready" and out["model"] == "bench"
    assert out["lease_minutes"] == 4 and out["lease_remaining_seconds"] <= 240
    assert out["endpoint"] == "http://bench-murderbot.llm-bench.svc.cluster.local:8080/v1/chat/completions"
    serve = [j for j in kube.created if j["metadata"]["labels"]["llm-bench/kind"] == "serve"]
    assert len(serve) == 1 and ("llm-bench", "bench-murderbot") in kube.services
    assert serve[0]["metadata"]["annotations"]["llm-bench/weights"].endswith("#Qwen3-0.6B-Q8_0.gguf")


def test_lease_clamped_to_runner_maximum(runners):
    sw, kube = make(runners)
    out = sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF", lease_minutes=9999)
    assert out["lease_minutes"] == 120


def test_not_staged_is_refused_and_nothing_is_created(runners):
    sw, kube = make(runners, entries=[])
    with pytest.raises(service.OperationFailed) as e:
        sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    assert e.value.result["error"] == "not_staged"
    assert not [j for j in kube.created if j["metadata"]["labels"]["llm-bench/kind"] == "serve"]


def test_staged_for_another_engine_is_not_enough(runners):
    sw, kube = make(runners, entries=[dict(STAGED, engines=["llamacpp"])])
    with pytest.raises(service.OperationFailed):
        sw.switch_model("murderbot", "ollama", "Qwen/Qwen3-0.6B-GGUF")
    sw2, _ = make(runners, entries=[dict(STAGED, ollama_imported=False)])
    with pytest.raises(service.OperationFailed):
        sw2.switch_model("murderbot", "ollama", "Qwen/Qwen3-0.6B-GGUF")


def test_two_quants_are_ambiguous_until_a_file_is_given(runners):
    other = dict(STAGED, primary_file="Qwen3-0.6B-Q4.gguf", path="/weights/x")
    sw, _ = make(runners, entries=[STAGED, other])
    with pytest.raises(service.OperationFailed) as e:
        sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    assert e.value.result["error"] == "ambiguous_staged"
    assert sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF", file="*Q4*")["state"] == "ready"


def test_blocked_flag_never_reaches_the_cluster(runners):
    sw, kube = make(runners)
    with pytest.raises(validate.ValidationError):
        sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF", args=["--hf-repo", "evil/model"])
    assert kube.created == []


def test_unknown_runner_is_rejected_with_configured_list(runners):
    sw, _ = make(runners)
    with pytest.raises(validate.ValidationError) as e:
        sw.switch_model("mini", "llamacpp", "a/b")
    assert e.value.detail["configured"] == ["murderbot"]


def test_engine_failure_deletes_the_job_and_returns_logs(runners):
    sw, kube = make(runners, scenario="badflag")
    out = sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    assert out["ok"] is False and out["error"] == "engine_failed" and "bogus-flag" in out["log_tail"]
    assert kube.list_jobs("llm-bench", "llm-bench/kind=serve") == []


def test_image_pull_failure_is_detected_at_once_and_cleaned_up(runners):
    sw, kube = make(runners, scenario="imagepull")
    out = sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    assert out["error"] == "engine_failed" and out["reason"] == "ErrImagePull"
    assert kube.list_jobs("llm-bench", "llm-bench/kind=serve") == []


def test_startup_timeout_deletes_the_job(runners):
    sw, kube = make(runners, scenario="never")
    out = sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF", startup_minutes=1)
    assert out["error"] == "startup_timeout"
    assert kube.list_jobs("llm-bench", "llm-bench/kind=serve") == []


def test_queued_behind_the_quota_gives_up(runners):
    sw, kube = make(runners, scenario="queued")
    out = sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    assert out["error"] == "not_scheduled"


def test_new_switch_replaces_the_previous_run(runners):
    sw, kube = make(runners)
    first = sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    second = sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    assert second["cancelled_previous"] == [first["job"]]
    assert len(kube.list_jobs("llm-bench", "llm-bench/kind=serve")) == 1


def test_restore_default_deletes_everything_it_started(runners):
    sw, kube = make(runners)
    sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    out = sw.restore_default("murderbot")
    assert len(out["deleted_jobs"]) == 1 and kube.list_jobs("llm-bench", "llm-bench/kind=serve") == []
    assert ("llm-bench", "bench-murderbot") not in kube.services
    assert sw.restore_default("murderbot")["deleted_jobs"] == []


def test_stage_model_runs_eviction_first_and_returns_without_waiting(runners):
    sw, kube = make(runners)
    out = sw.stage_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF", ctx=8192)
    kinds = [j["metadata"]["labels"]["llm-bench/kind"] for j in kube.created]
    assert kinds == ["util", "stage"] and out["state"] == "started"
    assert kube.list_jobs("llm-bench", "llm-bench/kind=util") == [], "util Jobs are removed after use"


def test_stage_model_protects_the_served_entry_from_eviction(runners):
    sw, kube = make(runners)
    sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    sw.stage_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    util = [j for j in kube.created if j["metadata"]["labels"]["llm-bench/kind"] == "util"][-1]
    env = {e["name"]: e.get("value") for e in util["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["MODE"] == "evict" and "Qwen3-0.6B-Q8_0.gguf" in env["IN_USE"]


def test_delete_staged_refuses_the_entry_being_served(runners):
    sw, _ = make(runners)
    sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    with pytest.raises(service.OperationFailed) as e:
        sw.delete_staged("murderbot", STAGED["repo"], STAGED["revision"], STAGED["primary_file"])
    assert e.value.result["error"] == "in_use"


def test_get_status_reports_lease_and_stage_result(runners):
    sw, kube = make(runners)
    sw.stage_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF")
    sw.switch_model("murderbot", "llamacpp", "Qwen/Qwen3-0.6B-GGUF", lease_minutes=10)
    st = sw.get_status("murderbot")
    assert st["serve"][0]["phase"] == "ready" and st["serve"][0]["lease_remaining_seconds"] <= 600
    assert st["stage"][0]["phase"] == "ended" and st["stage"][0]["result"]["ok"] is True
    assert st["gpu"] == {"gpu_requested": "0", "gpu_limit": "1"}


@pytest.mark.parametrize("pod,expected", [
    ({"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}, "ready"),
    ({"status": {"phase": "Running", "conditions": []}}, "starting"),
    ({"status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]}}, "scheduling"),
    ({"status": {"phase": "Pending", "containerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}]}}, "failed_to_start"),
    ({"status": {"phase": "Failed", "containerStatuses": [{"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}, "failed"),
    ({"status": {"phase": "Succeeded"}}, "ended"),
])
def test_classify_pod(pod, expected):
    assert service.classify_pod(pod)[0] == expected

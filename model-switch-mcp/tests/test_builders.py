from model_switch_mcp import builders

PATH = "/weights/Qwen/Qwen3-0.6B-GGUF/aaa/Qwen3-0.6B-Q8_0.gguf"


def serve(runners, engine, **kw):
    kw.setdefault("lease_minutes", 30)
    kw.setdefault("startup_minutes", 10)
    return builders.build_serve("murderbot", runners["murderbot"], engine, PATH, "rid1", **kw)


def container(job):
    return job["spec"]["template"]["spec"]["containers"][0]


def test_serve_common_properties(runners):
    for engine in ("ninfer", "llamacpp", "ollama"):
        job = serve(runners, engine)
        pod = job["spec"]["template"]["spec"]
        assert pod["priorityClassName"] == "llmkube-high"
        assert pod["nodeSelector"] == {"kubernetes.io/hostname": "murderbot"}
        assert pod["restartPolicy"] == "Never" and job["spec"]["backoffLimit"] == 0
        assert job["spec"]["ttlSecondsAfterFinished"] == 300
        assert container(job)["resources"]["limits"]["nvidia.com/gpu"] == "1"
        assert [v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]] == ["llm-bench-weights"]
        assert "HF_TOKEN" not in str(job), "the token must never reach a serve Job"
        lab = job["metadata"]["labels"]
        assert lab["llm-bench/runner"] == "murderbot" and lab["llm-bench/kind"] == "serve" and lab["llm-bench/engine"] == engine
        assert job["metadata"]["name"] == "bench-murderbot-%s-rid1" % engine


def test_lease_is_the_deadline_and_startup_is_the_probe(runners):
    job = serve(runners, "llamacpp", lease_minutes=45, startup_minutes=7)
    assert job["spec"]["activeDeadlineSeconds"] == 45 * 60
    assert container(job)["startupProbe"]["failureThreshold"] == 7 * 6


def test_llamacpp_argv_and_user_args_last(runners):
    c = container(serve(runners, "llamacpp", args=["-c", "777"], ctx=6144))
    assert c["command"] == ["/app/llama-server"]
    assert c["args"][:2] == ["-m", PATH]
    assert c["args"].index("6144") < c["args"].index("777") and c["args"][-2:] == ["-c", "777"]
    assert c["volumeMounts"][0]["readOnly"] is True


def test_ninfer_argv(runners):
    c = container(serve(runners, "ninfer", args=["--spec", "mtp"]))
    assert c["args"][0] == PATH and "--model-id" in c["args"] and c["args"][-2:] == ["--spec", "mtp"]
    assert "command" not in c


def test_ollama_uses_env_and_a_fixed_wrapper(runners):
    job = serve(runners, "ollama", env={"OLLAMA_FLASH_ATTENTION": "1"}, ctx=8192)
    c = container(job)
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["OLLAMA_FLASH_ATTENTION"] == "1" and env["OLLAMA_CONTEXT_LENGTH"] == "8192"
    assert builders.ollama_name(PATH) in c["command"][2]
    assert c["volumeMounts"][0]["readOnly"] is False


def test_serve_annotates_the_weights_entry_for_eviction_and_in_use_checks(runners):
    job = serve(runners, "llamacpp", weights_key="a/b@sha#f.gguf")
    assert job["metadata"]["annotations"]["llm-bench/weights"] == "a/b@sha#f.gguf"


def test_stage_job_is_cpu_only_and_has_the_token(runners):
    job = builders.build_stage("murderbot", runners["murderbot"], "llamacpp", "Qwen/Qwen3-0.6B-GGUF", "", "", 4096, "rid2")
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert "nvidia.com/gpu" not in str(c.get("resources")) and "runtimeClassName" not in pod and "priorityClassName" not in pod
    assert any(e["name"] == "HF_TOKEN" and e["valueFrom"]["secretKeyRef"]["optional"] for e in c["env"])
    assert job["metadata"]["name"].startswith("stage-murderbot-qwen-qwen3-0-6b-gguf-")


def test_ollama_stage_has_fetch_init_then_import(runners):
    job = builders.build_stage("murderbot", runners["murderbot"], "ollama", "Qwen/Qwen3-0.6B-GGUF", "", "", 4096, "rid3")
    pod = job["spec"]["template"]["spec"]
    assert [c["name"] for c in pod["initContainers"]] == ["fetch"] and pod["containers"][0]["name"] == "import"


def test_names_stay_within_kubernetes_limits(runners):
    rid = builders.new_run_id()
    job = builders.build_stage("murderbot", runners["murderbot"], "ninfer", "o" * 90 + "/" + "n" * 90, "", "", 4096, rid)
    assert len(job["metadata"]["name"]) <= 63


def test_service_selects_serve_pods_of_the_runner(runners):
    s = builders.build_service("murderbot", runners["murderbot"])
    assert s["metadata"]["name"] == "bench-murderbot"
    assert s["spec"]["selector"] == {"llm-bench/runner": "murderbot", "llm-bench/kind": "serve"}

"""Build the plain Kubernetes objects for staging, utility and serve Jobs and the serve Service.

All runner specifics come from the runner config (ConfigMap llm-bench-runners in Kubernetes).
Objects carry the labels llm-bench/runner|engine|run-id|kind; the MCP selects by label only.
"""
import hashlib
import re
import secrets
import time

GROUP = "llm-bench"
TTL_SECONDS = 300


def _base36(n):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, d = divmod(n, 36)
        out = digits[d] + out
    return out or "0"


def new_run_id():
    return _base36(int(time.time())) + secrets.token_hex(2)


def labels(runner, engine, run_id, kind):
    return {GROUP + "/runner": runner, GROUP + "/engine": engine, GROUP + "/run-id": run_id, GROUP + "/kind": kind}


def selector(runner, kind=None, run_id=None):
    parts = [GROUP + "/runner=" + runner]
    if kind:
        parts.append(GROUP + "/kind=" + kind)
    if run_id:
        parts.append(GROUP + "/run-id=" + run_id)
    return ",".join(parts)


def tolerations():
    return [
        {"key": "gpu-worker", "operator": "Equal", "value": "true", "effect": "NoSchedule"},
        {"key": "nvidia.com/gpu", "operator": "Equal", "value": "present", "effect": "NoSchedule"},
    ]


def slug(text, limit=24):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "x"


def entry_key(repo, revision, primary_file):
    return "%s@%s#%s" % (repo, revision, primary_file)


def _fetch_container(r, env_pairs, extra_mounts=()):
    st = r["stage"]
    env = [{"name": k, "value": str(v)} for k, v in env_pairs.items()]
    env.append({"name": "WEIGHTS_ROOT", "value": "/weights"})
    env.append({"name": "HF_TOKEN", "valueFrom": {"secretKeyRef": {"name": st["hfTokenSecret"], "key": "token", "optional": True}}})
    return {
        "name": "fetch", "image": st["image"], "imagePullPolicy": "IfNotPresent",
        "command": ["python3", "-I", "/script/stage.py"], "env": env,
        "volumeMounts": [{"name": "weights", "mountPath": "/weights"}, {"name": "script", "mountPath": "/script"}] + list(extra_mounts),
        "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"memory": st["memory"]}},
    }


def _base_pod(r):
    return {
        "restartPolicy": "Never", "nodeSelector": {"kubernetes.io/hostname": r["node"]}, "tolerations": tolerations(),
        "volumes": [
            {"name": "weights", "persistentVolumeClaim": {"claimName": r["weightsPvc"]}},
            {"name": "script", "configMap": {"name": r["stage"]["scriptConfigMap"], "defaultMode": 0o555}},
        ],
    }


def _job(name, namespace, lab, pod, deadline_seconds, annotations=None):
    meta = {"name": name, "namespace": namespace, "labels": lab}
    if annotations:
        meta["annotations"] = annotations
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": deadline_seconds, "ttlSecondsAfterFinished": TTL_SECONDS,
                     "template": {"metadata": {"labels": lab}, "spec": pod}}}


def ollama_name(path):
    return "m-" + hashlib.sha256(path.encode()).hexdigest()[:12]


OLLAMA_IMPORT = (
    "set -eu\n"
    "P=$(grep -o '\"path\": *\"[^\"]*\"' /shared/result.json | head -1 | cut -d'\"' -f4)\n"
    "N=m-$(printf %s \"$P\" | sha256sum | cut -c1-12)\n"
    "export OLLAMA_HOST=127.0.0.1:11999 OLLAMA_MODELS=/weights/ollama\n"
    "ollama serve >/tmp/serve.log 2>&1 &\n"
    "until ollama list >/dev/null 2>&1; do sleep 1; done\n"
    "printf 'FROM %s\\n' \"$P\" > /tmp/Modelfile\n"
    "ollama create \"$N\" -f /tmp/Modelfile\n"
    "touch \"$P.ollama-imported\"\n"
    "ollama list\n"
)


def build_stage(runner_name, r, engine, repo, revision, file_glob, ctx, run_id, fit_check="strict"):
    ninfer_versions = r["engines"].get("ninfer", {}).get("supportedContainerVersions", [2])
    env = {
        "ENGINE": engine, "HF_REPO": repo, "HF_REVISION": revision or "main", "HF_FILE": file_glob or "",
        "CTX": ctx, "VRAM_BYTES": r["vramBytes"], "VRAM_RESERVE_BYTES": r["vramReserveBytes"],
        "NINFER_VERSIONS": ",".join(str(v) for v in ninfer_versions), "FIT_CHECK": fit_check,
    }
    lab = labels(runner_name, engine, run_id, "stage")
    pod = _base_pod(r)
    if engine == "ollama":
        env["RESULT_FILE"] = "/shared/result.json"
        pod["volumes"].append({"name": "shared", "emptyDir": {}})
        pod["initContainers"] = [_fetch_container(r, env, [{"name": "shared", "mountPath": "/shared"}])]
        pod["containers"] = [{
            "name": "import", "image": r["engines"]["ollama"]["image"], "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", OLLAMA_IMPORT],
            "volumeMounts": [{"name": "weights", "mountPath": "/weights"}, {"name": "shared", "mountPath": "/shared"}],
            "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"memory": "4Gi"}},
        }]
    else:
        pod["containers"] = [_fetch_container(r, env)]
    annotations = {GROUP + "/repo": repo, GROUP + "/revision": revision or "main", GROUP + "/file": file_glob or ""}
    return _job("stage-%s-%s-%s" % (runner_name, slug(repo), run_id), r["namespace"], lab, pod,
                r["stage"]["deadlineMinutes"] * 60, annotations)


def build_util(runner_name, r, mode, env, run_id):
    """CPU Job running stage.py in list/delete/evict mode; the result is the last log line."""
    lab = labels(runner_name, "none", run_id, "util")
    pod = _base_pod(r)
    pod["containers"] = [_fetch_container(r, dict(env, MODE=mode))]
    return _job("util-%s-%s-%s" % (runner_name, mode, run_id), r["namespace"], lab, pod, 300)


def build_serve(runner_name, r, engine, path, run_id, lease_minutes, startup_minutes, args=(), env=None, ctx=4096,
                weights_key=""):
    eng = r["engines"][engine]
    lab = labels(runner_name, engine, run_id, "serve")
    e = dict(eng.get("env", {}))
    e.update(env or {})
    hp = {"path": eng["health"]["path"], "port": eng["health"]["port"]}
    startup_probe = {"httpGet": hp, "periodSeconds": 10, "failureThreshold": startup_minutes * 6, "timeoutSeconds": 5}
    ready = {"httpGet": hp, "periodSeconds": 10, "failureThreshold": 3, "timeoutSeconds": 5}
    live = {"httpGet": hp, "periodSeconds": 15, "failureThreshold": 4, "timeoutSeconds": 5}
    container = {
        "name": "engine", "image": eng["image"], "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": 8080, "name": "http"}],
        "volumeMounts": [{"name": "weights", "mountPath": "/weights", "readOnly": engine != "ollama"}],
        "resources": {"requests": {"cpu": r["serve"]["cpu"], "memory": r["serve"]["memory"], r["gpuResource"]: "1"},
                      "limits": {"memory": r["serve"]["memory"], r["gpuResource"]: "1"}},
    }
    if engine == "ninfer":
        container["args"] = [path] + list(eng["fixedArgs"]) + ["--model-id", "bench"] + list(args)
    elif engine == "llamacpp":
        container["command"] = list(eng["command"])
        container["args"] = ["-m", path] + list(eng["fixedArgs"]) + ["--alias", "bench", "-c", str(ctx)] + list(args)
    elif engine == "ollama":
        e.setdefault("OLLAMA_CONTEXT_LENGTH", str(ctx))
        c = "OLLAMA_HOST=127.0.0.1:8080 ollama"
        container["command"] = ["sh", "-c", (
            "set -e\n"
            "ollama serve & pid=$!\n"
            "until %s list >/dev/null 2>&1; do sleep 1; done\n"
            "%s cp %s bench\n"
            "%s run bench '' </dev/null >/dev/null\n"
            "wait $pid\n") % (c, c, ollama_name(path), c)]
        probe = {"exec": {"command": ["sh", "-c", "%s ps | grep -q bench" % c]}, "periodSeconds": 10}
        startup_probe = dict(probe, failureThreshold=startup_minutes * 6, timeoutSeconds=10)
        ready = dict(probe, failureThreshold=3, timeoutSeconds=10)
    else:
        raise ValueError("unsupported engine " + engine)
    container["startupProbe"], container["readinessProbe"], container["livenessProbe"] = startup_probe, ready, live
    container["env"] = [{"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
                        {"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "compute,utility"}] + \
                       [{"name": k, "value": str(v)} for k, v in e.items()]
    pod = {
        "restartPolicy": "Never", "priorityClassName": r["priorityClassName"], "runtimeClassName": r["runtimeClassName"],
        "nodeSelector": {"kubernetes.io/hostname": r["node"]}, "tolerations": tolerations(),
        "terminationGracePeriodSeconds": 30,
        "volumes": [{"name": "weights", "persistentVolumeClaim": {"claimName": r["weightsPvc"]}}],
        "containers": [container],
    }
    return _job("bench-%s-%s-%s" % (runner_name, engine, run_id), r["namespace"], lab, pod, lease_minutes * 60,
                {GROUP + "/weights": weights_key, GROUP + "/lease-minutes": str(lease_minutes),
                 GROUP + "/startup-minutes": str(startup_minutes)})


def build_service(runner_name, r):
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "bench-" + runner_name, "namespace": r["namespace"]},
            "spec": {"selector": {GROUP + "/runner": runner_name, GROUP + "/kind": "serve"},
                     "ports": [{"name": "http", "port": 8080, "targetPort": 8080}]}}

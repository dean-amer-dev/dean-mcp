import json
import os

import pytest
import yaml

from model_switch_mcp.service import Switch


@pytest.fixture
def runners():
    return yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "runners.yaml")))["runners"]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeKube:
    """Minimal in-memory cluster. scenario decides how serve Jobs behave."""

    def __init__(self, clock, scenario="ready", entries=None):
        self.clock = clock
        self.scenario = scenario
        self.jobs, self.pods, self.services, self.logs, self.events = {}, [], {}, {}, []
        self.entries = entries if entries is not None else []
        self.created = []
        self.evicted = []

    def _status_time(self):
        return __import__("datetime").datetime.fromtimestamp(self.clock.now(), __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def create_job(self, ns, body):
        name = body["metadata"]["name"]
        lab = body["metadata"]["labels"]
        job = json.loads(json.dumps(body))
        job["metadata"]["creationTimestamp"] = self._status_time()
        job["status"] = {}
        self.jobs[(ns, name)] = job
        self.created.append(job)
        kind = lab["llm-bench/kind"]
        pod = {"metadata": {"name": name + "-abcde", "labels": dict(lab), "creationTimestamp": self._status_time()},
               "status": {"phase": "Running", "conditions": [], "containerStatuses": []}}
        if kind == "util":
            mode = next(e["value"] for e in body["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "MODE")
            result = {"ok": True, "entries": self.entries, "volume_total": 10, "volume_free": 5} if mode == "list" else \
                {"ok": True, "deleted": [] if mode == "evict" else "x"}
            self.logs[pod["metadata"]["name"]] = "noise\n" + json.dumps(result)
            job["status"]["conditions"] = [{"type": "Complete", "status": "True"}]
            pod["status"]["phase"] = "Succeeded"
            self.pods.append(pod)
        elif kind == "stage":
            result = {"ok": True, "path": "/weights/x"}
            pod["status"]["phase"] = "Succeeded"
            pod["status"]["containerStatuses"] = [{"state": {"terminated": {"message": json.dumps(result), "exitCode": 0}}}]
            job["status"]["conditions"] = [{"type": "Complete", "status": "True"}]
            self.pods.append(pod)
        else:
            job["status"]["startTime"] = self._status_time()
            s = self.scenario
            if s == "queued":
                return job
            if s == "ready":
                pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
            elif s == "badflag":
                pod["status"]["phase"] = "Failed"
                pod["status"]["containerStatuses"] = [{"state": {"terminated": {"reason": "Error", "exitCode": 1}}}]
                job["status"]["conditions"] = [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]
                self.logs[pod["metadata"]["name"]] = "error: invalid argument: --bogus-flag"
            elif s == "imagepull":
                pod["status"]["phase"] = "Pending"
                pod["status"]["containerStatuses"] = [{"state": {"waiting": {"reason": "ErrImagePull", "message": "denied"}}}]
            self.pods.append(pod)
        return job

    def get_job(self, ns, name):
        return self.jobs.get((ns, name))

    def list_jobs(self, ns, selector):
        want = dict(p.split("=") for p in selector.split(","))
        return [j for (n, _), j in self.jobs.items() if n == ns and all(j["metadata"]["labels"].get(k) == v for k, v in want.items())]

    def delete_job(self, ns, name):
        job = self.jobs.pop((ns, name), None)
        if job:
            rid = job["metadata"]["labels"]["llm-bench/run-id"]
            self.pods = [p for p in self.pods if p["metadata"]["labels"]["llm-bench/run-id"] != rid]

    def list_pods(self, ns, selector):
        want = dict(p.split("=") for p in selector.split(","))
        return [p for p in self.pods if all(p["metadata"]["labels"].get(k) == v for k, v in want.items())]

    def pod_log(self, ns, name, container=None, tail=200):
        return self.logs.get(name, "")

    def list_events(self, ns, field_selector):
        return self.events

    def get_service(self, ns, name):
        return self.services.get((ns, name))

    def create_service(self, ns, body):
        self.services[(ns, body["metadata"]["name"])] = body
        return body

    def delete_service(self, ns, name):
        self.services.pop((ns, name), None)

    def list_quotas(self, ns):
        return [{"status": {"hard": {"requests.nvidia.com/gpu": "1"}, "used": {"requests.nvidia.com/gpu": "0"}}}]


STAGED = {"repo": "Qwen/Qwen3-0.6B-GGUF", "revision": "a" * 40, "requested_revision": "main",
          "primary_file": "Qwen3-0.6B-Q8_0.gguf", "path": "/weights/Qwen/Qwen3-0.6B-GGUF/%s/Qwen3-0.6B-Q8_0.gguf" % ("a" * 40),
          "engines": ["llamacpp", "ollama"], "ollama_imported": True, "weights_bytes": 639446688, "last_used": 1, "staged_at": 1}


def make(runners, scenario="ready", entries=None):
    clock = Clock()
    kube = FakeKube(clock, scenario, [STAGED] if entries is None else entries)
    return Switch(kube, lambda: runners, sleep=clock.sleep, clock=clock.now, poll=2.0), kube

"""Switching logic. Stateless: all run state is read from labelled Jobs, pods and events in the runner's namespace."""
import fnmatch
import json
import time
from datetime import datetime, timezone

from . import builders, validate
from .validate import ValidationError

FAILED_WAITING = {"ImagePullBackOff", "ErrImagePull", "CreateContainerError", "CreateContainerConfigError",
                  "InvalidImageName", "CrashLoopBackOff", "RunContainerError"}
CHAT_PATH = "/v1/chat/completions"


class OperationFailed(Exception):
    def __init__(self, error, **detail):
        super().__init__(error)
        self.result = {"ok": False, "error": error, **detail}


def _ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def job_condition(job):
    """('Failed'|'Complete'|None, reason)."""
    for c in (job.get("status") or {}).get("conditions") or []:
        if c.get("type") in ("Failed", "Complete") and c.get("status") == "True":
            return c["type"], c.get("reason") or ""
    return None, ""


def classify_pod(pod):
    st = pod.get("status") or {}
    phase = st.get("phase")
    statuses = st.get("containerStatuses") or []
    inits = st.get("initContainerStatuses") or []
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions") or [])
    for cs in inits + statuses:
        w = (cs.get("state") or {}).get("waiting") or {}
        if w.get("reason") in FAILED_WAITING:
            return "failed_to_start", w["reason"], (w.get("message") or "")[:300]
    if phase == "Failed":
        t = next(((cs.get("state") or {}).get("terminated") for cs in statuses + inits
                  if (cs.get("state") or {}).get("terminated")), {}) or {}
        return "failed", t.get("reason") or st.get("reason") or "Error", "exit code %s %s" % (t.get("exitCode"), (t.get("message") or "")[:200])
    if phase == "Succeeded":
        return "ended", "Completed", ""
    if phase == "Running":
        return ("ready", "", "") if ready else ("starting", "", "")
    for c in st.get("conditions") or []:
        if c.get("type") == "PodScheduled" and c.get("status") == "False":
            return "scheduling", c.get("reason") or "", (c.get("message") or "")[:300]
    return "starting", "", ""


def describe_job(job, pods, now):
    """Common view of a stage or serve Job."""
    meta, spec, st = job["metadata"], job.get("spec") or {}, job.get("status") or {}
    lab = meta.get("labels") or {}
    cond, reason = job_condition(job)
    pod = sorted(pods, key=lambda p: p["metadata"].get("creationTimestamp") or "")[-1] if pods else None
    pod_phase = pod_reason = pod_msg = None
    if pod:
        pod_phase, pod_reason, pod_msg = classify_pod(pod)
    if cond == "Failed":
        phase = "expired" if reason == "DeadlineExceeded" else "failed"
    elif cond == "Complete":
        phase = "ended"
    elif not pod:
        phase = "queued"
    else:
        phase = pod_phase
    out = {"job": meta["name"], "run_id": lab.get("llm-bench/run-id"), "kind": lab.get("llm-bench/kind"),
           "engine": lab.get("llm-bench/engine"), "phase": phase, "job_reason": reason or None,
           "pod": pod["metadata"]["name"] if pod else None, "pod_reason": pod_reason or None,
           "pod_message": pod_msg or None, "created": meta.get("creationTimestamp")}
    start = _ts(st.get("startTime"))
    deadline = spec.get("activeDeadlineSeconds")
    if start and deadline and phase not in ("expired", "failed", "ended"):
        out["lease_expires_at"] = _iso(start + deadline)
        out["lease_remaining_seconds"] = max(0, int(start + deadline - now))
    if lab.get("llm-bench/kind") == "serve":
        out["weights"] = (meta.get("annotations") or {}).get("llm-bench/weights")
    return out


def compact_events(events, names, limit=12):
    rows = [e for e in events if (e.get("involvedObject") or {}).get("name") in names]
    rows.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp") or "")
    return [{"type": e.get("type"), "reason": e.get("reason"), "object": e["involvedObject"]["name"],
             "message": (e.get("message") or "")[:240]} for e in rows[-limit:]]


class Switch:
    def __init__(self, kube, runners_provider, sleep=time.sleep, clock=time.time, poll=2.0):
        self.kube = kube
        self.runners_provider = runners_provider
        self.sleep = sleep
        self.clock = clock
        self.poll = poll

    # ---- lookups
    def runner(self, name):
        return validate.runner_of(self.runners_provider(), name or "murderbot")

    def _jobs(self, runner_name, r, kind=None):
        return self.kube.list_jobs(r["namespace"], builders.selector(runner_name, kind))

    def _pods(self, runner_name, r, kind=None, run_id=None):
        return self.kube.list_pods(r["namespace"], builders.selector(runner_name, kind, run_id))

    def _active_serve(self, runner_name, r):
        return [j for j in self._jobs(runner_name, r, "serve") if job_condition(j)[0] is None]

    def _wait_pods_gone(self, runner_name, r, kind, timeout=90):
        end = self.clock() + timeout
        while self.clock() < end:
            if not self._pods(runner_name, r, kind):
                return True
            self.sleep(self.poll)
        return False

    # ---- util jobs (list / delete / evict run in a CPU Job next to the weights)
    def util(self, runner_name, r, mode, env, timeout=150):
        run_id = builders.new_run_id()
        job = builders.build_util(runner_name, r, mode, env, run_id)
        name, ns = job["metadata"]["name"], r["namespace"]
        self.kube.create_job(ns, job)
        try:
            end = self.clock() + timeout
            while self.clock() < end:
                j = self.kube.get_job(ns, name)
                cond = job_condition(j)[0] if j else "Failed"
                if cond:
                    break
                self.sleep(self.poll)
            else:
                raise OperationFailed("util_timeout", mode=mode)
            line = None
            for pod in self.kube.list_pods(ns, builders.selector(runner_name, "util", run_id)):
                text = self.kube.pod_log(ns, pod["metadata"]["name"], "fetch", 20)
                for ln in reversed(text.strip().splitlines()):
                    if ln.startswith("{"):
                        line = ln
                        break
            if line is None:
                raise OperationFailed("util_no_result", mode=mode)
            result = json.loads(line)
            if not result.get("ok"):
                raise OperationFailed("util_failed", mode=mode, detail=result)
            return result
        finally:
            self.kube.delete_job(ns, name)

    # ---- tools
    def list_engines(self, runner_name):
        r = self.runner(runner_name)
        return {"ok": True, "runner": runner_name, "node": r["node"], "default_model": r.get("defaultModel"),
                "lease_minutes": r["lease"], "startup_minutes": r["startup"],
                "engines": {n: {"image": e["image"], "mode": e["mode"], "health": e["health"],
                                "supported_container_versions": e.get("supportedContainerVersions")}
                            for n, e in r["engines"].items()}}

    def get_status(self, runner_name):
        r = self.runner(runner_name)
        ns, now = r["namespace"], self.clock()
        jobs = [j for j in self._jobs(runner_name, r) if (j["metadata"]["labels"].get("llm-bench/kind") in ("serve", "stage"))]
        pods = self._pods(runner_name, r)
        by_run = {}
        for p in pods:
            by_run.setdefault(p["metadata"]["labels"].get("llm-bench/run-id"), []).append(p)
        serve, stage = [], []
        for j in sorted(jobs, key=lambda j: j["metadata"].get("creationTimestamp") or ""):
            run = j["metadata"]["labels"].get("llm-bench/run-id")
            d = describe_job(j, by_run.get(run, []), now)
            if d["kind"] == "serve":
                serve.append(d)
            else:
                ann = j["metadata"].get("annotations") or {}
                d.update({"repo": ann.get("llm-bench/repo"), "revision": ann.get("llm-bench/revision"),
                          "file": ann.get("llm-bench/file")})
                if d["phase"] in ("ended", "expired", "failed") or d["pod_reason"] == "Completed" or d["phase"] == "ended":
                    d["result"] = self._stage_result(ns, by_run.get(run, []))
                stage.append(d)
        quota = {}
        for q in self.kube.list_quotas(ns):
            hard, used = (q.get("status") or {}).get("hard") or {}, (q.get("status") or {}).get("used") or {}
            key = r["gpuResource"]
            if "requests." + key in hard:
                quota = {"gpu_requested": used.get("requests." + key), "gpu_limit": hard["requests." + key]}
        return {"ok": True, "runner": runner_name, "default_model": r.get("defaultModel"),
                "default_model_note": "restored automatically when no serve Job holds the GPU; its state is not visible to this tool",
                "gpu": quota, "serve": serve, "stage": stage}

    def _stage_result(self, ns, pods):
        for p in pods:
            term = None
            st = p.get("status") or {}
            for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
                term = ((cs.get("state") or {}).get("terminated") or {}).get("message") or term
            if term:
                try:
                    return json.loads(term)
                except ValueError:
                    return {"raw": term[:500]}
        return None

    def stage_model(self, runner_name, engine, repo, revision="", file="", ctx=None, wait_minutes=0, fit_check="strict"):
        r = self.runner(runner_name)
        validate.engine_of(r, engine)
        repo, revision, file = validate.repo(repo), validate.revision(revision), validate.file_pattern(file)
        ctx = validate.context(ctx)
        if fit_check not in ("strict", "warn"):
            raise ValidationError("bad_fit_check", allowed=["strict", "warn"])
        warnings = []
        in_use = ",".join(j["metadata"].get("annotations", {}).get("llm-bench/weights", "") for j in self._active_serve(runner_name, r))
        try:
            ev = self.util(runner_name, r, "evict", {"LIMIT_GIB": r["evictAboveGiB"], "IN_USE": in_use}, timeout=120)
            if ev.get("deleted"):
                warnings.append("evicted least recently used weights: %s" % ev["deleted"])
        except OperationFailed as e:
            warnings.append("eviction check failed: %s" % e.result.get("error"))
        run_id = builders.new_run_id()
        job = builders.build_stage(runner_name, r, engine, repo, revision, file, ctx, run_id, fit_check)
        self.kube.create_job(r["namespace"], job)
        out = {"ok": True, "runner": runner_name, "run_id": run_id, "job": job["metadata"]["name"], "state": "started",
               "warnings": warnings, "next": "poll get_status (stage section) until phase is ended, then switch_model"}
        if wait_minutes:
            out.update(self._wait_stage(runner_name, r, run_id, min(int(wait_minutes), 30) * 60))
        return out

    def _wait_stage(self, runner_name, r, run_id, timeout):
        end = self.clock() + timeout
        while self.clock() < end:
            st = self.get_status(runner_name)
            for s in st["stage"]:
                if s["run_id"] == run_id and s["phase"] in ("ended", "failed", "expired", "failed_to_start"):
                    ok = s["phase"] == "ended" and (s.get("result") or {}).get("ok")
                    return {"state": "staged" if ok else "failed", "result": s.get("result"), "phase": s["phase"]}
            self.sleep(max(self.poll, 5))
        return {"state": "still_running", "note": "stage not finished within wait; poll get_status"}

    def list_staged(self, runner_name):
        r = self.runner(runner_name)
        res = self.util(runner_name, r, "list", {})
        return {"ok": True, "runner": runner_name, "entries": res["entries"], "volume_total": res["volume_total"],
                "volume_free": res["volume_free"]}

    def delete_staged(self, runner_name, repo, revision, file):
        r = self.runner(runner_name)
        repo, revision = validate.repo(repo), validate.revision(revision)
        if not revision or not file:
            raise ValidationError("need_revision_and_file", hint="use the exact revision and primary_file from list_staged")
        key = builders.entry_key(repo, revision, file)
        for j in self._active_serve(runner_name, r):
            if (j["metadata"].get("annotations") or {}).get("llm-bench/weights") == key:
                raise OperationFailed("in_use", hint="restore_default first", entry=key)
        res = self.util(runner_name, r, "delete", {"HF_REPO": repo, "HF_REVISION": revision, "PRIMARY_FILE": file})
        return {"ok": True, "deleted": res["deleted"]}

    def _find_staged(self, runner_name, r, engine, repo, revision, file):
        entries = self.util(runner_name, r, "list", {})["entries"]
        hits = []
        for e in entries:
            if e["repo"] != repo or engine not in (e.get("engines") or []):
                continue
            if revision and revision not in (e["revision"], e.get("requested_revision")):
                continue
            if file and not (fnmatch.fnmatch(e["primary_file"], file) or e["primary_file"] == file):
                continue
            if engine == "ollama" and not e.get("ollama_imported"):
                continue
            hits.append(e)
        if not hits:
            raise OperationFailed("not_staged", repo=repo, revision=revision or None, file=file or None, engine=engine,
                                  hint="call stage_model for this engine first and wait for it to end")
        if len(hits) > 1:
            raise OperationFailed("ambiguous_staged", candidates=[
                {"revision": e["revision"], "primary_file": e["primary_file"]} for e in hits][:12],
                hint="pass revision (commit sha) and file")
        return hits[0]

    def _cancel_serve(self, runner_name, r):
        names = []
        for j in self._jobs(runner_name, r, "serve"):
            self.kube.delete_job(r["namespace"], j["metadata"]["name"])
            names.append(j["metadata"]["name"])
        if names and not self._wait_pods_gone(runner_name, r, "serve"):
            raise OperationFailed("previous_pods_still_terminating", jobs=names)
        return names

    def switch_model(self, runner_name, engine, repo, revision="", file="", ctx=None, args=None, env=None,
                     lease_minutes=None, startup_minutes=None, wait=True):
        r = self.runner(runner_name)
        validate.engine_of(r, engine)
        repo, revision, file = validate.repo(repo), validate.revision(revision), validate.file_pattern(file)
        ctx, args, env = validate.context(ctx), validate.args(args), validate.env(env)
        lease = validate.minutes(lease_minutes, r["lease"]["defaultMinutes"], r["lease"]["maxMinutes"], "lease")
        startup = validate.minutes(startup_minutes, r["startup"]["defaultMinutes"], r["startup"]["maxMinutes"], "startup")
        entry = self._find_staged(runner_name, r, engine, repo, revision, file)
        cancelled = self._cancel_serve(runner_name, r)
        ns = r["namespace"]
        self.kube.create_service(ns, builders.build_service(runner_name, r))
        key = builders.entry_key(entry["repo"], entry["revision"], entry["primary_file"])
        run_id = builders.new_run_id()
        job = builders.build_serve(runner_name, r, engine, entry["path"], run_id, lease, startup, args, env, ctx, key)
        self.kube.create_job(ns, job)
        out = {"ok": True, "runner": runner_name, "engine": engine, "run_id": run_id, "job": job["metadata"]["name"],
               "weights": key, "lease_minutes": lease, "startup_minutes": startup, "cancelled_previous": cancelled,
               "model": "bench",
               "endpoint": "http://bench-%s.%s.svc.cluster.local:8080%s" % (runner_name, ns, CHAT_PATH)}
        if not wait:
            out["state"] = "started"
            return out
        return self._wait_ready(runner_name, r, run_id, startup, out)

    def _failure(self, runner_name, r, run_id, d, out, error):
        ns = r["namespace"]
        pods = self._pods(runner_name, r, "serve", run_id)
        names = {d["job"]} | {p["metadata"]["name"] for p in pods}
        log = ""
        if pods:
            log = self.kube.pod_log(ns, pods[-1]["metadata"]["name"], "engine", 60)
        events = compact_events(self.kube.list_events(ns, ""), names)
        self.kube.delete_job(ns, d["job"])
        return {**out, "ok": False, "state": "failed", "error": error, "phase": d["phase"], "reason": d.get("pod_reason") or d.get("job_reason"),
                "message": d.get("pod_message"), "log_tail": log[-4000:], "events": events,
                "note": "serve Job deleted; the default model returns automatically"}

    def _wait_ready(self, runner_name, r, run_id, startup_minutes, out):
        ns = r["namespace"]
        started = self.clock()
        budget = startup_minutes * 60 + 120
        name = out["job"]
        while True:
            job = self.kube.get_job(ns, name)
            if job is None:
                return {**out, "ok": False, "state": "failed", "error": "job_disappeared"}
            d = describe_job(job, self._pods(runner_name, r, "serve", run_id), self.clock())
            if d["phase"] == "ready":
                return {**out, "state": "ready", "lease_expires_at": d.get("lease_expires_at"),
                        "lease_remaining_seconds": d.get("lease_remaining_seconds"), "pod": d["pod"],
                        "startup_seconds": int(self.clock() - started)}
            if d["phase"] in ("failed", "failed_to_start", "ended", "expired"):
                return self._failure(runner_name, r, run_id, d, out, "engine_failed" if d["phase"] != "expired" else "lease_expired_before_ready")
            if d["phase"] == "queued" and self.clock() - started > 120:
                return self._failure(runner_name, r, run_id, d, out, "not_scheduled")
            if self.clock() - started > budget:
                return self._failure(runner_name, r, run_id, d, out, "startup_timeout")
            self.sleep(self.poll)

    def restore_default(self, runner_name):
        r = self.runner(runner_name)
        cancelled = self._cancel_serve(runner_name, r)
        self.kube.delete_service(r["namespace"], "bench-" + runner_name)
        return {"ok": True, "runner": runner_name, "deleted_jobs": cancelled,
                "note": "the GPU is free; the default model reschedules by itself and takes about a minute to load"}

    def get_logs(self, runner_name, run_id="", kind="serve", tail=200, container=""):
        r = self.runner(runner_name)
        if kind not in ("serve", "stage"):
            raise ValidationError("bad_kind", allowed=["serve", "stage"])
        tail = max(1, min(int(tail or 200), 2000))
        jobs = self._jobs(runner_name, r, kind)
        if run_id:
            jobs = [j for j in jobs if j["metadata"]["labels"].get("llm-bench/run-id") == run_id]
        if not jobs:
            return {"ok": False, "error": "no_such_run", "kind": kind, "run_id": run_id or None}
        job = sorted(jobs, key=lambda j: j["metadata"].get("creationTimestamp") or "")[-1]
        rid = job["metadata"]["labels"]["llm-bench/run-id"]
        pods = self._pods(runner_name, r, kind, rid)
        names = {job["metadata"]["name"]} | {p["metadata"]["name"] for p in pods}
        out = {"ok": True, "job": job["metadata"]["name"], "run_id": rid, "events": compact_events(self.kube.list_events(r["namespace"], ""), names)}
        if not pods:
            return {**out, "log": "", "note": "pod no longer exists (finished Jobs are removed after about 5 minutes)"}
        pod = pods[-1]
        default = "engine" if kind == "serve" else ("import" if job["metadata"]["labels"].get("llm-bench/engine") == "ollama" else "fetch")
        c = container or default
        return {**out, "pod": pod["metadata"]["name"], "container": c, "log": self.kube.pod_log(r["namespace"], pod["metadata"]["name"], c, tail)}

"""Input validation. Everything a caller can send is checked here before any object is built."""
import re

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
FILE_RE = re.compile(r"^[A-Za-z0-9._*?/\[\]-]{0,200}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
ENV_BLOCKED_PREFIXES = ("NVIDIA_", "HF_", "KUBERNETES_")
ENV_BLOCKED = {"PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "HOME"}
# Flags that would make an engine fetch weights itself or read a path other than the staged one.
BLOCKED_FLAGS = {
    "-m", "--model", "-mu", "--model-url", "-hf", "-hfr", "--hf-repo", "-hff", "--hf-file", "-hft", "--hf-token",
    "-dr", "--docker-repo", "--model-draft", "-md", "--model-vocoder", "-mv", "--hf-repo-draft", "-hfd", "-hfrd",
}


class ValidationError(Exception):
    def __init__(self, error, **detail):
        super().__init__(error)
        self.error = error
        self.detail = detail

    def as_dict(self):
        return {"ok": False, "error": self.error, **self.detail}


def runner_of(runners, name):
    if name not in runners:
        raise ValidationError("unknown_runner", runner=name, configured=sorted(runners))
    return runners[name]


def engine_of(runner, name):
    engines = runner["engines"]
    if name not in engines:
        raise ValidationError("unknown_engine", engine=name, enabled=sorted(engines))
    return engines[name]


def repo(value):
    if not isinstance(value, str) or not REPO_RE.match(value):
        raise ValidationError("bad_repo", hint="expected a Hugging Face repo id like owner/name")
    return value


def revision(value):
    value = value or ""
    if value and not REVISION_RE.match(value):
        raise ValidationError("bad_revision", hint="a commit sha, tag or branch name")
    return value


def file_pattern(value):
    value = value or ""
    if not FILE_RE.match(value) or ".." in value or value.startswith("/"):
        raise ValidationError("bad_file_pattern", hint="a file name or glob inside the repo")
    return value


def context(value, default=4096):
    if value in (None, 0):
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not 256 <= value <= 4_000_000:
        raise ValidationError("bad_context", hint="integer between 256 and 4000000")
    return value


def minutes(value, default, cap, what):
    if value in (None, 0):
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError("bad_" + what, hint="positive integer minutes")
    return min(value, cap)


def args(values):
    values = list(values or [])
    if len(values) > 64:
        raise ValidationError("too_many_args", max=64)
    for v in values:
        if not isinstance(v, str) or len(v) > 1024 or "\x00" in v or "\n" in v:
            raise ValidationError("bad_arg", hint="each arg is a string up to 1024 chars, no newline or NUL")
        flag = v.split("=", 1)[0]
        if flag in BLOCKED_FLAGS:
            raise ValidationError("blocked_flag", flag=flag,
                                  hint="weights come only from the staged Hugging Face download")
    return values


def env(values):
    values = dict(values or {})
    if len(values) > 32:
        raise ValidationError("too_many_env", max=32)
    for k, v in values.items():
        if not isinstance(k, str) or not ENV_KEY_RE.match(k) or k in ENV_BLOCKED or k.startswith(ENV_BLOCKED_PREFIXES):
            raise ValidationError("bad_env_key", key=str(k))
        if not isinstance(v, str) or len(v) > 512 or "\x00" in v:
            raise ValidationError("bad_env_value", key=k)
    return values

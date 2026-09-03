"""Coexistence contract tests: tool-output-compactor + a progress-guard.

Emulates Hermes' model_tools.py hook flow on the RAW result:

    post_tool_call observers first (guard detection on raw result)
    -> transform_tool_result callbacks in load order
       ('progress-guard' < 'tool-output-compactor' alphabetically)
    -> first string return wins; it is what persists / reaches the model.

Proves (REDIRECTION_PLAN.md §5, §11-12):
   1. compaction never hides raw-result change from the guard;
   2. on recovery events the guard's injected message wins and the
      compactor's return is dropped (documented, observable);
   3. identical-result / changed-result / polling / repeated-failure / noisy-
      with-semantic-change scenarios coexist without breaking either plugin.

The suite is SELF-CONTAINED by default: it runs against a minimal local stub
(_StubGuard) that implements only the observable contract tool-output-compactor
depends on (raw-result fingerprinting, poll completion, recovery injection on
repeat), NOT hermes-progress-guard internals. hermes-progress-guard evolves
independently; tool-output-compactor's tests must not break when it does.

Real-plugin validation is opt-in: set COEXISTENCE_REAL=1 (and, if needed,
PROGRESS_GUARD_PLUGIN_DIR to point at its plugins/progress-guard) to also run
the same scenarios against the actual hermes-progress-guard source.

Usage:
    python3 coexistence_test.py                       # stub suite (always)
    COEXISTENCE_REAL=1 python3 coexistence_test.py    # + real progress-guard
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
PG_DIR = Path(
    os.environ.get("PROGRESS_GUARD_PLUGIN_DIR")
    or (REPO.parent / "hermes-progress-guard" / "plugins" / "progress-guard")
)

COMPACTED_MARKER = "[tool-output-compactor compacted tool result]"
_POLL_RE = re.compile(r'\{"state": "(\w+)", "progress": (\d+)\}')


# --------------------------------------------------------------------------
# Stub progress-guard: the stable contract surface only.
# --------------------------------------------------------------------------

def _fingerprint(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()


class _Event:
    def __init__(self, result_fingerprint: str) -> None:
        self.result_fingerprint = result_fingerprint


class _State:
    """Minimal TurnState surface used by the scenarios below."""

    def __init__(self) -> None:
        self.events = []
        self.last_poll_done = False
        self.hard_stop = False
        self.recovery_count = 0
        self.pending_recovery = None
        # exact-repeat bookkeeping (consecutive identical runs, non-poll tools)
        self._key = None
        self._run = 0

    def note(self, fp: str) -> None:
        self.events.append(_Event(fp))


class _StubRegistry:
    """Mirrors the real guard's StateRegistry.get(session, turn) surface."""

    def __init__(self) -> None:
        self._turns = {}

    def get(self, session, turn) -> _State:
        return self._turns.setdefault((session, turn), _State())


class _StubGuard:
    """Guards: record raw fingerprints + inject a recovery message on repeat.

    Only the behavior tool-output-compactor's guarantees depend on, and only
    when configured (exact_repeat.threshold in ctx settings): a non-poll tool
    returning the same (action, raw) more than `threshold` consecutive times
    triggers one recovery injection that preempts compaction.
    """

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.registry = _StubRegistry()

    def install(self, ctx) -> None:
        ctx.register_hook("post_tool_call", self.on_post_tool_call)
        ctx.register_hook("transform_tool_result", self.on_transform_tool_result)

    def _state(self, session, turn):
        return self.registry.get(session, turn)

    @staticmethod
    def _is_poll(tool_name, args) -> bool:
        return tool_name == "process_manage" and isinstance(args, dict) \
            and args.get("op") == "poll"

    def on_post_tool_call(self, tool_name="", args=None, result=None, session_id="",
                          turn_id="", tool_call_id="", status="ok", **_) -> None:
        st = self._state(session_id, turn_id)
        fp = _fingerprint(result) if isinstance(result, str) else _fingerprint(str(result))
        st.note(fp)

        poll_state = self._poll_progress(result)
        if poll_state:
            st.last_poll_done = poll_state[1] >= 100

        if self._is_poll(tool_name, args):
            return
        threshold = self.ctx.get_config("exact_repeat.threshold")
        if not threshold:
            return
        key = (tool_name, _fingerprint(str(args)), fp)
        if key == st._key:
            st._run += 1
        else:
            st._key, st._run = key, 1
        if st._run > int(threshold):  # repeat beyond threshold -> recover
            st.pending_recovery = tool_call_id
            st.recovery_count += 1

    @staticmethod
    def _poll_progress(result):
        if isinstance(result, str):
            m = _POLL_RE.search(result)
            if m:
                return m.group(1), int(m.group(2))
        return None

    def on_transform_tool_result(self, tool_name="", result=None, session_id="",
                                 turn_id="", tool_call_id="", **_) -> str | None:
        if not isinstance(result, str):
            return None
        st = self._state(session_id, turn_id)
        if st.pending_recovery == tool_call_id:
            st.pending_recovery = None
            return (result + "\n\n[stub guard] identical output repeated; "
                    "verify this is still what you need to run.")
        return None


# --------------------------------------------------------------------------

def _load(alias: str, init_py: Path):
    spec = importlib.util.spec_from_file_location(
        alias, str(init_py), submodule_search_locations=[str(init_py.parent)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {init_py}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    """Hermes plugin context stand-in: register_hook + scoped get_config."""

    def __init__(self, settings=None) -> None:
        self._settings = settings or {}
        self.hooks = {}

    def register_hook(self, name, cb) -> None:
        self.hooks.setdefault(name, []).append(cb)

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


class Harness:
    """One wiring per scenario: guard registered before compactor."""
    def __init__(self, guard_cls, toc_mod, settings=None) -> None:
        self.ctx = FakeCtx(settings)
        self.guard = guard_cls(self.ctx)
        self.guard.install(self.ctx)
        self.compactor = toc_mod.ToolOutputCompactorPlugin()
        self.transform_cbs = [
            self.guard.on_transform_tool_result,   # 'progress-guard' loads first
            self.compactor.transform_tool_result,
        ]
        self.session = "sx"
        self._call_no = 0

    def run(self, tool_name, args, result, status="ok", error_type=None, error_message=None):
        self._call_no += 1
        kw = dict(
            tool_name=tool_name, args=args, result=result,
            session_id=self.session, turn_id="t1",
            tool_call_id=f"call_{self._call_no}",
            status=status, error_type=error_type, error_message=error_message,
            duration_ms=1,
        )
        self.guard.on_post_tool_call(**kw)          # observers first (raw result)
        returned = None
        for cb in self.transform_cbs:               # all run; first str wins
            r = cb(**kw)
            if isinstance(r, str) and returned is None:
                returned = r
        return returned if returned is not None else result

    def state(self):
        return self.guard.registry.get(self.session, "t1")

    def fingerprints(self):
        return [e.result_fingerprint for e in self.state().events]


# --------------------------------------------------------------------------

def big_log(n=1200, marker=None):
    lines = [f"{i:04d}: worker={i % 7} step={i % 3} INFO processing chunk {i}" for i in range(n)]
    if marker:
        lines.append(marker)
    out = "\n".join(lines)
    return out * max(1, (4100 // len(out)) + 1)  # ensure > DEDUP_MIN_CHARS


def test_identical_large(h):
    """Same 50KB result twice: compactor dedups, guard still sees both."""
    raw = big_log()
    a = h.run("terminal", {"command": "check"}, raw)
    b = h.run("terminal", {"command": "check"}, raw)
    assert COMPACTED_MARKER in a
    assert "times this session" in b, "second identical result became a dedup stub"
    assert len(b) < len(raw) // 10, "stub must be tiny, 50KB not re-entered"
    fp = h.fingerprints()
    assert len(fp) == 2 and fp[0] == fp[1], "guard saw both identical raws"


def test_changed_result(h):
    """Build #1 -> 12 errors, build #2 -> 7 errors: difference must survive."""
    r1 = big_log(marker="Build FAILED with 12 errors")
    r2 = big_log(marker="Build FAILED with 7 errors")
    a = h.run("terminal", {"command": "make"}, r1)
    b = h.run("terminal", {"command": "make"}, r2)
    assert "12 errors" in a and "7 errors" in b, "compactor preserved meaningful diff"
    assert "times this session" not in b, "changed output must not be deduped"
    fp = h.fingerprints()
    assert fp[0] != fp[1], "guard sees the change"


def test_polling(h):
    """pending -> pending -> running -> completed must not stall or dedup away."""
    def poll(pct, word):
        return big_log(n=60, marker=f'{{"state": "{word}", "progress": {pct}}}')
    out = [h.run("process_manage", {"op": "poll"}, poll(40, "pending"))]
    out.append(h.run("process_manage", {"op": "poll"}, poll(40, "pending")))
    out.append(h.run("process_manage", {"op": "poll"}, poll(60, "running")))
    out.append(h.run("process_manage", {"op": "poll"}, poll(100, "completed")))
    st = h.state()
    assert st.last_poll_done is True, "guard recorded poll completion"
    assert not st.hard_stop, "legitimate polling must not hard-stop"
    # identical pending results (2nd, same bytes) became a reference, others compacted
    assert "times this session" in out[1] or len(out[1]) < len(out[0]), "repeat pending deduped/compacted"
    assert "completed" in out[3]


def test_repeated_failure(h):
    """3x identical pytest failure: first has evidence, later ones become refs."""
    raw = "===== test session starts =====\ncollected 1042 items\n" + \
        "\n".join(f"test_worker_{i} step {i % 7}" for i in range(300)) + \
        "\n____ test_refresh ____\ntests/test_auth.py:42: in test_refresh\n" + \
        "E       AssertionError: expected 200, got 401\n" + \
        "short test summary info\n" + \
        "FAILED tests/test_auth.py::test_refresh - AssertionError: expected 200, got 401\n" + \
        "=== 1 failed, 1041 passed in 12.45s ===\n" * 4
    args = {"command": "pytest -v tests/test_auth.py"}
    e = {"error_type": "tool_error", "error_message": "Script exited with code 1"}
    a = h.run("terminal", args, raw, status="error", **e)
    b = h.run("terminal", args, raw, status="error", **e)
    c = h.run("terminal", args, raw, status="error", **e)
    assert "FAILED tests/test_auth.py::test_refresh" in a, "first failure keeps node evidence"
    assert "AssertionError: expected 200, got 401" in a, "first failure keeps error evidence"
    assert "times this session" in b, "2nd identical failure became a reference"
    fp = h.fingerprints()
    assert len(set(fp)) == 1, "guard sees 3 identical failure results"


def test_noisy_small_change(h):
    """10k noisy lines, only the ERROR marker differs between runs."""
    body = big_log(n=3000)
    r1 = body + "\nERROR foo"
    r2 = body + "\nERROR bar"
    a = h.run("terminal", {"command": "run.sh"}, r1)
    b = h.run("terminal", {"command": "run.sh"}, r2)
    assert "ERROR foo" in a and "ERROR bar" in b, "critical lines survive compaction"
    assert "times this session" not in b, "small semantic change must not dedup"
    fp = h.fingerprints()
    assert fp[0] != fp[1], "fingerprint must not hide the semantic difference"


def test_recovery_injection_wins(h):
    """When the guard RECOVERs, its injected message preempts compaction.

    Every large outcome must be coherent and annotated: either the compactor's
    (COMPACTED_MARKER: compacted body or dedup reference) or the guard's
    (raw preserved + guidance appended). Never both, never un-annotated raw.

    The real guard may keep recovering until material progress occurs (its own
    policy, evolving independently), so the follow-up distinct call only has to
    stay coherent and consume the recovery state.
    """
    raw = big_log()
    args = {"command": "probe"}
    comp = guard = 0
    for _ in range(3):                       # 1st compacted, 2nd dedup ref, 3rd -> recovery
        out = h.run("terminal", args, raw)
        if COMPACTED_MARKER in out:
            comp += 1
        else:
            guard += 1
            assert out.startswith(raw) and len(out) > len(raw), \
                "guard recovery message preserves the raw result and appends guidance"
    st = h.state()
    assert st.recovery_count >= 1, "repeated probe triggered recovery"
    assert comp >= 1 and guard >= 1, "compactor AND guard both acted across the runs"
    dr = big_log(marker="ok")
    d = h.run("terminal", {"command": "probe -v"}, dr)   # distinct call afterwards
    if COMPACTED_MARKER not in d:
        assert d.startswith(dr), "guard still recovering -> coherent annotated message"
    assert st.pending_recovery is None, "recovery consumed by the matching call"


# --------------------------------------------------------------------------

def build_cases(harness):
    return [
        ("identical large result", lambda: test_identical_large(harness())),
        ("similar but changed", lambda: test_changed_result(harness())),
        ("polling", lambda: test_polling(harness())),
        ("repeated failure", lambda: test_repeated_failure(harness())),
        ("noisy with semantic change", lambda: test_noisy_small_change(harness())),
        ("recovery injection wins", lambda: test_recovery_injection_wins(
            harness({"exact_repeat.threshold": 2, "exact_repeat.window": 8}))),
    ]


def _run(tag, cases) -> bool:
    failed = []
    for name, fn in cases:
        try:
            fn()
            print(f"PASS {tag}: {name}")
        except AssertionError as exc:
            failed.append(name)
            print(f"FAIL {tag}: {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed.append(name)
            print(f"ERROR {tag}: {name}: {type(exc).__name__}: {exc}")
    if failed:
        print(f"coexistence: {tag} failed={len(failed)}/{len(cases)} {failed}")
        return False
    print(f"coexistence: {tag} passed={len(cases)}/{len(cases)}")
    return True


def main() -> int:
    for env in ("TOOL_SLIM_ENABLED", "TOOL_SLIM_LLM_ENABLED"):
        os.environ.pop(env, None)
    os.environ.setdefault("TOOL_SLIM_MAX_CHARS", "4000")
    os.environ.setdefault("TOOL_SLIM_DEDUP_MIN_CHARS", "4000")
    os.environ.setdefault("TOOL_SLIM_MIN_SAVING_CHARS", "500")
    os.environ["TOOL_SLIM_ENABLED"] = "true"
    os.environ["TOOL_SLIM_LLM_ENABLED"] = "false"

    toc = _load("toc_coexist", REPO / "__init__.py")

    ok = _run("stub", build_cases(
        lambda settings=None: Harness(_StubGuard, toc, settings)))

    if os.environ.get("COEXISTENCE_REAL") == "1":
        if not PG_DIR.is_dir():
            print(f"ERROR: COEXISTENCE_REAL=1 but progress-guard not found at {PG_DIR} "
                  f"(set PROGRESS_GUARD_PLUGIN_DIR)")
            return 1
        pg = _load("pg_coexist", PG_DIR / "__init__.py")
        from pg_coexist.hooks import ProgressGuard  # noqa: PLC0415  (loaded above)
        real = _run("real", build_cases(
            lambda settings=None: Harness(ProgressGuard, toc, settings)))
        ok = ok and real

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

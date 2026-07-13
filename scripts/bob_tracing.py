#!/usr/bin/env python3
"""Bob tracing — OpenTelemetry-style spans for a run / tool / sub-agent tree.

Seam discipline (like the MCP server/client): the span model + nesting + no-op-when-off logic is import-light and
unit-tested WITHOUT the `opentelemetry` package (tests inject a fake sink). The real OTLP export bridge
(`_otlp_sink`) is the ONLY part that touches the package and a live collector; it's smoke-validated
against Langfuse, not unit-tested.

Gated by `agent.tracing` (default false): a disabled Tracer returns a shared no-op span for every
`span()` call — zero allocation, zero I/O, no dependency at rest — so the file log is identical
whether tracing is on or off. When on, each finished span is handed to a `sink` (the OTLP exporter, or a
test recorder). Parent/child nesting is explicit (a child span inherits its parent's `trace_id`), so it
works across the generator/thread boundaries the agent loop crosses without contextvar hazards.
"""
import sys
import uuid


def _new_id(n: int = 16) -> str:
    return uuid.uuid4().hex[:n]


def _monotonic() -> float:
    import time
    return time.monotonic()


class Span:
    """One finished-or-in-flight span. `end()` stamps duration and emits to the tracer's sink. Also a
    context manager: on a normal exit it ends 'ok'; on an exception it ends 'error' with the message."""
    __slots__ = ("name", "span_id", "trace_id", "parent_id", "attributes", "status",
                 "_tracer", "_t0", "ended")

    def __init__(self, tracer, name, trace_id, parent_id, attributes):
        self.name = name
        self.span_id = _new_id()
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.attributes = dict(attributes or {})
        self.status = "unset"
        self._tracer = tracer
        self._t0 = tracer._clock()
        self.ended = False

    def set(self, key, value) -> "Span":
        self.attributes[key] = value
        return self

    def set_status(self, status: str) -> "Span":
        self.status = status
        return self

    def end(self, status: str = None, attributes: dict = None) -> None:
        if self.ended:
            return
        self.ended = True
        if status:
            self.status = status
        if attributes:
            self.attributes.update(attributes)
        self.attributes["duration_ms"] = round((self._tracer._clock() - self._t0) * 1000, 3)
        self._tracer._emit(self)

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.status = "error"
            self.attributes.setdefault("exception", str(exc))
        self.end()
        return False  # never swallow


class _NoopSpan:
    """The disabled-tracer span: satisfies the Span interface, does nothing, allocates nothing."""
    span_id = None
    trace_id = None
    parent_id = None
    attributes = {}
    status = "unset"

    def set(self, *a, **k):
        return self

    def set_status(self, *a, **k):
        return self

    def end(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_NOOP_SPAN = _NoopSpan()


class Tracer:
    """Creates spans and funnels finished ones to a sink. Disabled -> every span() is the shared no-op."""
    __slots__ = ("enabled", "_sink", "service", "_clock", "spans")

    def __init__(self, enabled: bool = False, sink=None, service: str = "bob-agent", clock=None):
        self.enabled = enabled
        self._sink = sink
        self.service = service
        self._clock = clock or _monotonic
        self.spans = []   # finished spans, for introspection / tests

    def span(self, name: str, attributes: dict = None, parent=None):
        """Start a span. `parent` is a Span (or None for a root). A child inherits the parent's trace_id
        so the whole run/sub-agent tree shares one trace. Disabled -> the shared no-op span."""
        if not self.enabled:
            return _NOOP_SPAN
        trace_id = parent.trace_id if getattr(parent, "trace_id", None) else _new_id(32)
        parent_id = getattr(parent, "span_id", None)
        return Span(self, name, trace_id, parent_id, attributes)

    # Alias — a span the caller ends explicitly (the run span, which outlives many generator yields).
    start = span

    def _emit(self, span: Span) -> None:
        self.spans.append(span)
        if self._sink:
            try:
                self._sink(span)
            except Exception as e:   # an exporter hiccup must never crash the run (loud-fail)
                print(f"[warn] tracing sink failed: {e}", file=sys.stderr)


def make_tracer(config: dict, sink=None) -> Tracer:
    """Build the run's tracer from config. `agent.tracing` off -> a disabled (no-op) Tracer. On -> a
    Tracer whose sink is chosen by `agent.tracingSink`: 'file' (default) writes spans to
    logs/traces/<trace_id>.jsonl (Docker-free, offline, viewable via `bob traces`); 'otlp' exports to an
    OTLP collector (e.g. Langfuse). A sink injected by the caller (tests) wins over both."""
    agent = (config or {}).get("agent", {}) or {}
    if not agent.get("tracing", False):
        return Tracer(enabled=False)
    if sink is None:
        sink = _otlp_sink(agent) if (agent.get("tracingSink", "file") == "otlp") else _file_sink(agent)
    return Tracer(enabled=True, sink=sink)


def traces_dir():
    """The directory the file sink writes to (logs/traces under the repo/data dir)."""
    from pathlib import Path
    try:
        import osenv
        return osenv.cache_dir() / "traces"
    except Exception:   # osenv unavailable (import-light contexts) -> repo-relative default
        return Path("logs") / "traces"


def _file_sink(agent: dict):
    """Return a callable(Span) that appends each finished span as one JSONL line under
    logs/traces/<trace_id>.jsonl. Zero dependencies, no server, Docker-free; the whole run/tool/sub-agent
    tree lands in one file per trace for `bob traces` to reconstruct."""
    import json
    base = traces_dir()
    base.mkdir(parents=True, exist_ok=True)

    def _write(span: "Span") -> None:
        rec = {"trace_id": span.trace_id, "span_id": span.span_id, "parent_id": span.parent_id,
               "name": span.name, "status": span.status, "attributes": span.attributes}
        with open(base / f"{span.trace_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    return _write


# --- reader side: `bob traces [list|show <id>]` (viewer for the file sink) ------------------------

def _load_trace(path) -> list:
    """Read one <trace_id>.jsonl into a list of span records (skips malformed lines)."""
    import json
    spans = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            spans.append(json.loads(line))
        except ValueError:
            continue
    return spans


def list_traces(limit: int = 20) -> list:
    """Most-recent-first summary of stored traces: (trace_id, root_name, span_count, mtime)."""
    base = traces_dir()
    if not base.exists():
        return []
    files = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        spans = _load_trace(f)
        root = next((s for s in spans if not s.get("parent_id")), spans[0] if spans else None)
        out.append((f.stem, (root or {}).get("name", "?"), len(spans), f.stat().st_mtime))
    return out


def format_trace(trace_id: str) -> str:
    """Render one trace as an indented run/tool/sub-agent tree (parent_id linkage), with durations."""
    base = traces_dir()
    path = base / f"{trace_id}.jsonl"
    if not path.exists():
        # allow a unique prefix
        matches = list(base.glob(f"{trace_id}*.jsonl")) if base.exists() else []
        if len(matches) != 1:
            return f"No trace '{trace_id}' in {base} (see: bob traces)."
        path = matches[0]
    spans = _load_trace(path)
    if not spans:
        return f"Trace {path.stem} is empty."
    children = {}
    for s in spans:
        children.setdefault(s.get("parent_id"), []).append(s)
    lines = [f"trace {path.stem}"]

    def _walk(parent_id, depth):
        for s in children.get(parent_id, []):
            dur = s.get("attributes", {}).get("duration_ms")
            dur_s = f"  {dur} ms" if dur is not None else ""
            status = "" if s.get("status") in ("ok", "unset", None) else f"  [{s.get('status')}]"
            lines.append(f"{'  ' * depth}- {s.get('name', '?')}{dur_s}{status}")
            _walk(s.get("span_id"), depth + 1)

    _walk(None, 1)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OTLP export bridge — the ONLY part that touches the opentelemetry package + a live collector.
# Not unit-tested (the seam above is, via a fake sink); smoke-validated against Langfuse (:3001).
# ---------------------------------------------------------------------------

def _otlp_sink(agent: dict):
    """Return a callable(Span) that exports to an OTLP collector, or a no-op that warns once if the
    opentelemetry SDK is missing (loud-fail: tracing degrades to off, the run never crashes)."""
    endpoint = agent.get("otlpEndpoint", "") or "http://127.0.0.1:4318/v1/traces"
    try:
        from opentelemetry.sdk.trace import TracerProvider, ReadableSpan  # type: ignore  # noqa: F401
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
            OTLPSpanExporter)
        from opentelemetry import trace as _ot  # type: ignore
        from opentelemetry.trace import SpanContext, TraceFlags, NonRecordingSpan, set_span_in_context  # type: ignore
    except Exception as e:
        print(f"[warn] tracing enabled but opentelemetry SDK unavailable ({e}); "
              f"install it or set agent.tracing=false — tracing disabled for this run.", file=sys.stderr)
        return None

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    ot_tracer = provider.get_tracer("bob-agent")

    def _export(span: Span) -> None:
        # Best-effort reconstruction of our span as an OTel span. Parent linkage uses the parent_id we
        # recorded (mapped into a NonRecordingSpan context). Timing approximate (post-hoc). Smoke-only.
        ctx = None
        if span.parent_id:
            try:
                pctx = SpanContext(trace_id=int(span.trace_id, 16), span_id=int(span.parent_id, 16),
                                   is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED))
                ctx = set_span_in_context(NonRecordingSpan(pctx))
            except Exception:
                ctx = None
        s = ot_tracer.start_span(span.name, context=ctx)
        for k, v in span.attributes.items():
            try:
                s.set_attribute(k, v)
            except Exception:
                pass
        s.end()

    return _export

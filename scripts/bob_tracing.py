#!/usr/bin/env python3
"""Bob tracing (O9) — OpenTelemetry-style spans for a run / tool / sub-agent tree.

Seam discipline (like N10/O7): the span model + nesting + no-op-when-off logic is import-light and
unit-tested WITHOUT the `opentelemetry` package (tests inject a fake sink). The real OTLP export bridge
(`_otlp_sink`) is the ONLY part that touches the package and a live collector; it's smoke-validated
against Langfuse, not unit-tested.

Gated by `agent.tracing` (default false): a disabled Tracer returns a shared no-op span for every
`span()` call — zero allocation, zero I/O, no dependency at rest — so the N5 file log is byte-identical
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
    Tracer whose sink is the OTLP exporter bridge (unless a sink is injected, e.g. by tests)."""
    agent = (config or {}).get("agent", {}) or {}
    if not agent.get("tracing", False):
        return Tracer(enabled=False)
    if sink is None:
        sink = _otlp_sink(agent)
    return Tracer(enabled=True, sink=sink)


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

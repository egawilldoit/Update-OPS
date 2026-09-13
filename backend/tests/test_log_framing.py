"""Lossless phase-stream framing regression tests (D3).

The coordinator tails a sanitized NDJSON stream file. The previous
`_tail` advanced its offset before a record was known complete, so a
record split across reads (or a multibyte character split across
reads) was silently dropped. These tests pin the pure framing primitive
plus one end-to-end `_supervise_launched` regression.
"""
import json

import pytest


def _record(line, stream="stdout"):
    return (json.dumps({"ts": "t", "stream": stream, "line": line},
                       ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Pure framing primitive
# ---------------------------------------------------------------------------

def test_stream_framer_split_json_record_across_reads():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    data = _record("hello world")
    assert framer.feed(data[:7]) == []
    assert framer.feed(data[7:]) == [("stdout", "hello world")]


def test_stream_framer_split_utf8_multibyte_across_reads():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    data = _record("caf\u00e9 \u2192 done")
    idx = data.index(b"\xc3") + 1
    assert idx < len(data)
    assert framer.feed(data[:idx]) == []
    got = framer.feed(data[idx:])
    assert got == [("stdout", "caf\u00e9 \u2192 done")]
    assert "\ufffd" not in got[0][1]


def test_stream_framer_multiple_records_in_one_read():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    out = framer.feed(_record("a") + _record("b", "stderr")
                      + _record("c", "event"))
    assert out == [("stdout", "a"), ("stderr", "b"), ("event", "c")]


def test_stream_framer_incomplete_final_record_drained():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    data = _record("tail").rstrip(b"\n")
    assert framer.feed(data) == []
    assert framer.drain() == [("stdout", "tail")]
    assert framer.drain() == []


def test_stream_framer_malformed_record_surfaced():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    out = framer.feed(b"not-json\n")
    assert len(out) == 1
    stream, line = out[0]
    assert stream == "event"
    assert line.startswith(pr.STREAM_RECORD_MALFORMED_MARKER)


def test_stream_framer_keeps_valid_records_before_malformed():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    out = framer.feed(_record("ok") + b"{broken\n" + _record("after"))
    assert out[0] == ("stdout", "ok")
    assert out[1][0] == "event"
    assert out[1][1].startswith(pr.STREAM_RECORD_MALFORMED_MARKER)
    assert out[2] == ("stdout", "after")


def test_stream_framer_large_backlog_preserved_in_order():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    expected = ["record-%05d" % i for i in range(5000)]
    blob = b"".join(_record(line) for line in expected)
    out = []
    step = 256 * 1024
    for i in range(0, len(blob), step):
        out.extend(framer.feed(blob[i:i + step]))
    out.extend(framer.drain())
    assert [line for _s, line in out] == expected


def test_stream_framer_record_ends_exactly_at_read_boundary():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    first = _record("edge")
    second = _record("next")
    blob = first + second
    assert framer.feed(blob[:len(first)]) == [("stdout", "edge")]
    assert framer.feed(blob[len(first):]) == [("stdout", "next")]
    assert framer.drain() == []


def test_stream_framer_delimiter_split_between_reads():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer()
    data = _record("split-delim")
    body, nl = data[:-1], data[-1:]
    assert nl == b"\n"
    assert framer.feed(body) == []
    assert framer.feed(nl) == [("stdout", "split-delim")]


def test_stream_framer_oversized_record_fails_loud():
    from backend.app.worker import phase_run as pr

    framer = pr.StreamFramer(max_record=16)
    with pytest.raises(pr.StreamFramingError):
        framer.feed(_record("x" * 100))
    framer2 = pr.StreamFramer(max_record=16)
    with pytest.raises(pr.StreamFramingError):
        framer2.feed(b"x" * 100)


# ---------------------------------------------------------------------------
# End-to-end supervisor regression
# ---------------------------------------------------------------------------

class _StagedPopen(object):
    """Worker whose stream file grows between polls."""

    stream_path = ""
    result_path = ""

    def __init__(self, stages):
        self.stages = list(stages)
        self.index = 0

    def poll(self):
        if self.index < len(self.stages):
            payload, rc = self.stages[self.index]
            self.index += 1
            if payload:
                with open(type(self).stream_path, "ab") as fh:
                    fh.write(payload)
            return rc
        return 0

    def wait(self, timeout=None):
        return 0

    def communicate(self, timeout=None):
        return b"", b""

    def kill(self):
        pass


def test_supervise_launched_preserves_split_and_final_records(
        tmp_path, monkeypatch):
    from backend.app import units as units_lib
    from backend.app.worker import phase_run as pr

    monkeypatch.setattr(pr.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        units_lib, "query_unit",
        lambda unit, timeout_s=5: {"state": "confirmed_stopped",
                                   "unit": unit, "detail": ""})
    stream_path = str(tmp_path / "s.stream")
    result_path = str(tmp_path / "r.json")
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump({"ok": True, "data": {"done": 1}}, fh)

    r1 = _record("first record")
    r2 = _record("second record")
    r3 = _record("final record").rstrip(b"\n")
    cut = len(r1) // 2

    _StagedPopen.stream_path = stream_path
    _StagedPopen.result_path = result_path
    proc = _StagedPopen([(r1[:cut], None), (r1[cut:] + r2 + r3, 0)])

    emitted = []
    ok, data, error, timed_out = pr._supervise_launched(
        proc, "unit", "phase scope", result_path, stream_path, 60.0,
        lambda stream, line: emitted.append((stream, line)), None)
    assert ok is True and timed_out is False, error
    assert data.get("done") == 1
    assert [line for _s, line in emitted] == [
        "first record", "second record", "final record"]

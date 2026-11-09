"""CPU oracles for fixed-text KV reuse and worker integration.

The model below implements the installed prefill/cache interfaces with causal
integer arithmetic. It is not an ASR model and never imports MLX. Full uncached
prefill is the oracle; no expected logits come from the cache implementation.
Run with: uvx --offline --with numpy pytest -q Tests/runtime/test_prefix_cache.py
"""
from __future__ import annotations

import builtins
import contextlib
import importlib.util
import sys
import types
import json
from pathlib import Path

import numpy as np
import pytest

from conftest import PYSTUBS, ZH_VOICE_DAEMON


class Cache:
    def __init__(self, num_layers=2, max_seq_len=None):
        self.keys = [None] * num_layers
        self.values = [None] * num_layers
        self.offset = 0
        self.max_seq_len = max_seq_len

    @property
    def seq_len(self):
        return self.offset

    def update(self, key, value, layer_idx):
        end = self.offset + key.shape[2]
        if self.max_seq_len is None:
            if self.keys[layer_idx] is None:
                self.keys[layer_idx] = key.copy()
                self.values[layer_idx] = value.copy()
            else:
                self.keys[layer_idx] = np.concatenate((self.keys[layer_idx], key), axis=2)
                self.values[layer_idx] = np.concatenate((self.values[layer_idx], value), axis=2)
        else:
            if end > self.max_seq_len:
                raise ValueError("KV cache overflow")
            if self.keys[layer_idx] is None:
                shape = (*key.shape[:2], self.max_seq_len, key.shape[3])
                self.keys[layer_idx] = np.zeros(shape, dtype=key.dtype)
                self.values[layer_idx] = np.zeros(shape, dtype=value.dtype)
            self.keys[layer_idx][..., self.offset:end, :] = key
            self.values[layer_idx][..., self.offset:end, :] = value
        if layer_idx == len(self.keys) - 1:
            self.offset = end
        return (self.keys[layer_idx][..., :end, :],
                self.values[layer_idx][..., :end, :])


class Model:
    """A causal decoder whose values depend on all three position axes."""

    audio_token_id = 151676

    def __init__(self):
        self.events = []
        self.fail_next = False
        self.config = types.SimpleNamespace(text_config=types.SimpleNamespace(num_hidden_layers=2))

    def create_cache(self, max_seq_len=None):
        return Cache(max_seq_len=max_seq_len)

    def _embed_tokens(self, input_ids, *, validate_input_ids=True):
        ids = np.asarray(input_ids, dtype=np.int64)
        return np.stack((ids, ids * 2 + 1), axis=-1)

    def _inject_audio_features(self, embeds, audio_features, audio_mask):
        result = embeds.copy()
        for batch in range(result.shape[0]):
            result[batch, audio_mask[batch]] = audio_features[batch]
        return result

    def model(self, input_ids=None, inputs_embeds=None, position_ids=None,
              attention_mask=None, cache=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("decoder failed")
        embeds = inputs_embeds if inputs_embeds is not None else self._embed_tokens(input_ids)
        self.events.append({"length": embeds.shape[1], "offset": cache.offset,
                            "positions": position_ids.copy(), "embeds": embeds.copy(),
                            "cache": cache, "mask": attention_mask})
        position_value = (position_ids[:, 0] + 3 * position_ids[:, 1]
                          + 7 * position_ids[:, 2])
        value = (embeds + position_value[..., None])[:, None, :, :]
        start = cache.offset
        for layer in range(len(cache.keys)):
            _, values = cache.update(value + layer, value * (layer + 1), layer)
        # Each output uses history up to that token, never future suffix tokens.
        return np.cumsum(values[:, 0], axis=1)[:, start:, :]

    def lm_head(self, hidden):
        return hidden

    def prefill(self, input_ids, audio_features, position_ids, cache):
        embeds = self._embed_tokens(input_ids, validate_input_ids=True)
        embeds = self._inject_audio_features(embeds, audio_features,
                                              input_ids == self.audio_token_id)
        hidden = self.model(inputs_embeds=embeds, position_ids=position_ids,
                            attention_mask=None, cache=cache)
        return self.lm_head(hidden[:, -1:, :])

    def step(self, input_ids, position_ids, cache):
        return self.lm_head(self.model(input_ids=input_ids, position_ids=position_ids,
                                       cache=cache))


class Tokenizer:
    AUDIO_TOKEN_ID = Model.audio_token_id
    AUDIO_START_TOKEN_ID = 151669

    def __init__(self, prefix_length=None, missing_audio=False):
        self.prefix_length = prefix_length
        self.missing_audio = missing_audio

    def build_prompt_tokens(self, n_audio_tokens, language=None, context=""):
        if self.prefix_length is None:
            prefix = [151644, 10] + [1000 + ord(c) for c in context]
            prefix += [151645, 11, 151644, 12, self.AUDIO_START_TOKEN_ID]
        else:
            prefix = [42] * self.prefix_length
        audio = [] if self.missing_audio else [self.AUDIO_TOKEN_ID] * n_audio_tokens
        return prefix + audio + [151670, 151645, 151644, 13]


def positions(length):
    return np.broadcast_to(np.arange(length, dtype=np.int32), (1, 3, length)).copy()


def request(tokenizer, context="terms", n_audio=3):
    ids = np.array([tokenizer.build_prompt_tokens(n_audio, language="Chinese", context=context)],
                   dtype=np.int32)
    audio = np.arange(n_audio * 2, dtype=np.int64).reshape(1, n_audio, 2) + 71
    return ids, audio, positions(ids.shape[1])


def invoke(model, args, capacity=80, style="keyword"):
    cache = model.create_cache(max_seq_len=capacity)
    if style == "positional":
        result = model.prefill(*args, cache)
    elif style == "mixed":
        result = model.prefill(args[0], args[1], position_ids=args[2], cache=cache)
    else:
        result = model.prefill(input_ids=args[0], audio_features=args[1],
                               position_ids=args[2], cache=cache)
    return result, cache


@pytest.fixture
def daemon(monkeypatch):
    """Import the parent module without allowing any eager MLX imports."""
    module = types.ModuleType("prefix_cache_daemon_spec")
    module.__file__ = str(ZH_VOICE_DAEMON)
    original_import = builtins.__import__

    def cpu_import(name, *args, **kwargs):
        if name == "mlx" or name.startswith("mlx.") or name.startswith("mlx_qwen3_asr"):
            raise AssertionError("socket parent imported MLX: " + name)
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as guarded:
        guarded.setattr(builtins, "__import__", cpu_import)
        exec(compile(ZH_VOICE_DAEMON.read_bytes(), str(ZH_VOICE_DAEMON), "exec"), module.__dict__)

    # A small NumPy adapter supplies only CPU tensor operations used by the API.
    mx = types.ModuleType("mlx.core")
    for name in ("array", "arange", "stack", "zeros", "zeros_like", "concatenate",
                 "broadcast_to", "array_equal", "all", "int32", "float32", "float16"):
        setattr(mx, name, getattr(np, name))
    mx.arange = lambda *args, **kwargs: np.arange(*args, **({"dtype": np.int32} | kwargs))
    mx.eval = lambda *args: None
    mx.synchronize = lambda: None
    mx.set_wired_limit = lambda value: 0
    mx.metal = types.SimpleNamespace(device_info=lambda: {"max_recommended_working_set_size": 1})
    mlx = types.ModuleType("mlx")
    mlx.core = mx
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mx)

    # Keep the package's function/module re-export shape for tokenizer pinning.
    for name in ("mlx_qwen3_asr", "mlx_qwen3_asr.transcribe", "mlx_qwen3_asr.tokenizer"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location("mlx_qwen3_asr", PYSTUBS / "mlx_qwen3_asr.py")
    q = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "mlx_qwen3_asr", q)
    spec.loader.exec_module(q)
    # Register the dynamically created modules with monkeypatch for cleanup.
    for name in ("mlx_qwen3_asr.transcribe", "mlx_qwen3_asr.tokenizer"):
        child = sys.modules.pop(name)
        monkeypatch.setitem(sys.modules, name, child)
    module.q_stub = q
    return module


def test_socket_parent_imports_no_mlx_and_budget_is_bounded(daemon):
    assert daemon.MAX_PREFIX_TOKENS == 1024
    assert callable(daemon.FixedPrefixPrefill)


@pytest.mark.parametrize("style", ["keyword", "positional", "mixed"])
def test_reuse_matches_full_prefill_and_decode_with_original_api(daemon, style):
    tokenizer = Tokenizer()
    args = request(tokenizer)
    expected, baseline_cache = invoke(Model(), args)
    expected_step = Model().step(np.array([[37]]), positions(args[0].shape[1] + 1)[:, :, -1:],
                                  baseline_cache)
    model = Model()
    reusable = daemon.FixedPrefixPrefill(model, tokenizer, "terms")
    with reusable.enabled():
        actual, cache = invoke(model, args, style=style)
        actual_step = model.step(np.array([[37]]), positions(args[0].shape[1] + 1)[:, :, -1:], cache)
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(actual_step, expected_step)
    prefix_len = tokenizer.build_prompt_tokens(1, context="terms").index(tokenizer.AUDIO_TOKEN_ID)
    suffix = [event for event in model.events if event["offset"] == prefix_len]
    assert len(suffix) == 1, "matching request did not use a saved fixed prefix"
    assert suffix[0]["length"] == args[0].shape[1] - prefix_len
    np.testing.assert_array_equal(suffix[0]["positions"], args[2][:, :, prefix_len:])


def test_each_request_has_independent_full_capacity_kv_and_prefix_is_built_once(daemon):
    tokenizer = Tokenizer()
    model = Model()
    reusable = daemon.FixedPrefixPrefill(model, tokenizer, "terms")
    args1 = request(tokenizer, n_audio=2)
    args2 = request(tokenizer, n_audio=5)
    with reusable.enabled():
        first, cache1 = invoke(model, args1, capacity=47)
        # Mutating one request must not poison the retained prefix or next request.
        cache1.keys[0][..., 0, :] = -999
        cache1.values[1][..., 0, :] = -777
        second, cache2 = invoke(model, args2, capacity=61)
    np.testing.assert_array_equal(first, invoke(Model(), args1, capacity=47)[0])
    reference, reference_cache = invoke(Model(), args2, capacity=61)
    np.testing.assert_array_equal(second, reference)
    assert cache1 is not cache2
    assert (cache1.max_seq_len, cache2.max_seq_len) == (47, 61)
    assert (cache1.offset, cache2.offset) == (args1[0].shape[1], args2[0].shape[1])
    for left, right in zip(cache1.keys + cache1.values, cache2.keys + cache2.values):
        assert left.shape[2] == 47 and right.shape[2] == 61
        assert not np.shares_memory(left, right)
    for got, wanted in zip(cache2.keys + cache2.values,
                           reference_cache.keys + reference_cache.values):
        np.testing.assert_array_equal(got, wanted)
    prefix_len = tokenizer.build_prompt_tokens(1, context="terms").index(tokenizer.AUDIO_TOKEN_ID)
    prefix_events = [event for event in model.events
                     if event["offset"] == 0 and event["length"] == prefix_len]
    assert len(prefix_events) == 1, "fixed text must be computed once, not once per request"
    assert sum(event["length"] for event in model.events) == (
        prefix_len + args1[0].shape[1] - prefix_len + args2[0].shape[1] - prefix_len)


@pytest.mark.parametrize("mismatch", ["model", "ids", "position_axis0", "position_axis1",
                                      "position_axis2"])
def test_mismatching_model_ids_or_positions_use_original_prefill(daemon, mismatch):
    tokenizer = Tokenizer()
    owner = Model()
    selected = Model() if mismatch == "model" else owner
    args = list(request(tokenizer))
    if mismatch == "ids":
        args[0][0, 2] += 1
    elif mismatch.startswith("position_axis"):
        axis = int(mismatch[-1])
        args[2][0, axis, 1] += 1
    expected, _ = invoke(Model(), args)
    reusable = daemon.FixedPrefixPrefill(owner, tokenizer, "terms")
    before = len(selected.events)
    with reusable.enabled():
        actual, _ = invoke(selected, args)
    np.testing.assert_array_equal(actual, expected)
    calls = selected.events[before:]
    assert calls[-1]["length"] == args[0].shape[1]
    assert calls[-1]["offset"] == 0


@pytest.mark.parametrize("capacity", [None, 64])
def test_create_cache_keeps_original_positional_and_keyword_capacity(daemon, capacity):
    model = Model()
    reusable = daemon.FixedPrefixPrefill(model, Tokenizer(), "terms")
    with reusable.enabled():
        positional = model.create_cache(capacity)
        keyword = model.create_cache(max_seq_len=capacity)
    assert positional.max_seq_len == keyword.max_seq_len == capacity
    assert positional is not keyword


@pytest.mark.parametrize("prefix_length,missing_audio", [(1025, False), (8, True), (0, False)])
def test_oversized_missing_or_empty_prefix_stays_uncached(daemon, prefix_length, missing_audio):
    tokenizer = Tokenizer(prefix_length=prefix_length, missing_audio=missing_audio)
    model = Model()
    args = request(tokenizer, context="terms", n_audio=0 if missing_audio else 2)
    expected, _ = invoke(Model(), args, capacity=1100)
    reusable = daemon.FixedPrefixPrefill(model, tokenizer, "terms")
    assert model.events == [], "unsupported prefix must not perform an extra decoder pass"
    with reusable.enabled():
        actual, _ = invoke(model, args, capacity=1100)
    np.testing.assert_array_equal(actual, expected)
    assert len(model.events) == 1
    assert model.events[0]["length"] == args[0].shape[1]
    assert model.events[0]["offset"] == 0


def test_prefix_budget_accepts_exactly_1024_tokens(daemon):
    tokenizer = Tokenizer(prefix_length=1024)
    model = Model()
    args = request(tokenizer, n_audio=2)
    reusable = daemon.FixedPrefixPrefill(model, tokenizer, "terms")
    with reusable.enabled():
        actual, _ = invoke(model, args, capacity=1100)
    np.testing.assert_array_equal(actual, invoke(Model(), args, capacity=1100)[0])
    assert model.events[-1]["offset"] == 1024


def test_unsupported_model_is_unchanged_and_context_manager_is_a_noop(daemon):
    model = types.SimpleNamespace(prefill=lambda *a, **kw: "unchanged",
                                  create_cache=lambda *a, **kw: "fresh")
    original = dict(vars(model))
    reusable = daemon.FixedPrefixPrefill(model, Tokenizer(), "terms")
    with reusable.enabled():
        assert model.prefill("ids", "audio", "positions", "cache") == "unchanged"
        assert model.create_cache(20) == "fresh"
    assert vars(model) == original


@pytest.mark.parametrize("failure", ["body", "decoder"])
def test_bindings_are_restored_on_exception_and_next_request_still_matches(daemon, failure):
    model = Model()
    tokenizer = Tokenizer()
    args = request(tokenizer)
    original_instance = dict(vars(model))
    original_methods = (model.prefill, model.create_cache, type(model).prefill, type(model).create_cache)
    reusable = daemon.FixedPrefixPrefill(model, tokenizer, "terms")
    with pytest.raises(RuntimeError, match="failed"):
        with reusable.enabled():
            if failure == "body":
                raise RuntimeError("body failed")
            model.fail_next = True
            invoke(model, args)
    assert (model.prefill, model.create_cache, type(model).prefill,
            type(model).create_cache) == original_methods
    assert ("prefill" in vars(model)) == ("prefill" in original_instance)
    assert ("create_cache" in vars(model)) == ("create_cache" in original_instance)
    with reusable.enabled():
        actual, _ = invoke(model, args)
    np.testing.assert_array_equal(actual, invoke(Model(), args)[0])


class Pipe:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []

    def recv(self):
        return next(self.messages)

    def send(self, message):
        self.sent.append(message)


@pytest.mark.parametrize("empty_first,fail_first", [(False, False), (True, False), (False, True)])
def test_worker_reuses_context_replaces_changed_context_and_never_biases_retry(
        daemon, monkeypatch, empty_first, fail_first):
    caches = []
    active = []
    calls = []

    class PrefixSpy:
        def __init__(self, model, tokenizer, context):
            self.context = context
            self.prefix_tokens = 7
            self.cached = object()
            caches.append(self)

        @contextlib.contextmanager
        def enabled(self):
            assert not active
            active.append(self)
            try:
                yield
            finally:
                active.pop()

    def transcribe(wav, **kwargs):
        calls.append((wav, kwargs, active[0] if active else None))
        if len(calls) == 1 and fail_first:
            raise RuntimeError("request failed")
        if len(calls) == 1 and empty_first:
            return types.SimpleNamespace(text=" \n ")
        return types.SimpleNamespace(text="one\ntwo")

    model = types.SimpleNamespace(_resolved_model_path="stub/model")
    monkeypatch.setattr(daemon.q_stub, "load_model", lambda target: (model, {}))
    monkeypatch.setattr(daemon.q_stub, "transcribe", transcribe)
    monkeypatch.setattr(daemon, "warm_up", lambda *args: None)
    monkeypatch.setattr(daemon, "FixedPrefixPrefill", PrefixSpy)
    pipe = Pipe([("TRANSCRIBE", "a.wav", "A"), ("TRANSCRIBE", "b.wav", "A"),
                 ("TRANSCRIBE", "c.wav", "B"), ("TRANSCRIBE", "d.wav", ""), ("STOP",)])
    daemon.worker_main(pipe)
    assert pipe.sent[0][0] == "READY", pipe.sent
    assert [cache.context for cache in caches] == ["A", "B"]
    biased = [call for call in calls if call[1]["context"]]
    assert [call[2] for call in biased] == [caches[0], caches[0], caches[1]]
    assert all(call[2] is None for call in calls if not call[1]["context"])
    assert all(call[1]["model"] is model and call[1]["language"] == "Chinese" for call in calls)
    assert not active
    if empty_first:
        assert [(call[0], call[1]["context"]) for call in calls[:2]] == [
            ("a.wav", "A"), ("a.wav", "")]
        assert len(calls) == 5
    else:
        assert len(calls) == 4
    results = pipe.sent[1:]
    assert len(results) == 4
    if fail_first:
        assert results[0][0] == "ERROR"
        assert "request failed" in results[0][1]
        assert results[1:] == [("RESULT", "one two")] * 3
    else:
        assert results == [("RESULT", "one two")] * 4


@pytest.fixture
def probe():
    path = Path(__file__).with_name("accept_rollout.py")
    module = types.ModuleType("asr_rollout_acceptance_spec")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("raw", [b"ERROR: failure\n", b"META asr_ms=1\n\n",
                                  b"META asr_ms=1\nwrong\n", b"expected\n",
                                  b"META asr_ms=nan\nexpected\n",
                                  b"META asr_ms=1\nexpected\nextra\n",
                                  b"META asr_ms=1\nexpected", b"\xff\n"])
def test_acceptance_rejects_errors_empty_wrong_text_or_invalid_protocol(probe, raw):
    result = probe.compare_reply(raw, "expected")
    assert result["status"] == "failed"
    assert result["exact_match"] is False
    assert result["raw_reply_base64"]
    assert "error" in result


def test_acceptance_preserves_internal_spaces_and_normalizes_only_lines(probe):
    expected = probe.one_line("  first  word\nsecond\tword \n")
    assert expected == "first  word second\tword"
    raw = ("META asr_ms=123\n" + expected + "\n").encode()
    result = probe.compare_reply(raw, expected)
    assert result["status"] == "ok"
    assert result["asr_ms"] == 123
    assert result["exact_match"] is True
    assert result["raw_reply_utf8"] == raw.decode()
    assert probe.compare_reply(b"META asr_ms=1\nfirst word second word\n", expected)["status"] == "failed"


@pytest.mark.parametrize("live_text", ["gold", "wrong", "", "ERROR: failed"])
def test_acceptance_main_records_each_original_comparison_without_live_io(
        probe, monkeypatch, tmp_path, live_text):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    clips, calls = [], []
    for index in range(3):
        wav = corpus / f"clip-{index}.wav"
        wav.write_bytes(b"synthetic WAV input" + bytes([index]))
        ctx = wav.with_suffix(".ctx")
        ctx.write_bytes(b"terms\n")
        clip = {"path": str(wav), "sha256": probe.sha256(wav.read_bytes()),
                "context_path": str(ctx), "context_sha256": probe.sha256(ctx.read_bytes())}
        clips.append(clip)
        common = {"clip_path": str(wav), "clip_sha256": clip["sha256"],
                  "context_sha256": clip["context_sha256"], "status": "ok"}
        calls += [common | {"id": index * 2, "mode": "baseline", "text": "gold\nline",
                            "text_sha256": probe.sha256(b"gold\nline")},
                  common | {"id": index * 2 + 1, "mode": "prefix", "text": "wrong",
                            "text_sha256": probe.sha256(b"wrong")}]
    baseline = tmp_path / "prefix-result.json"
    baseline.write_text(json.dumps({"status": "ok", "completed": True,
                                    "clips": clips, "calls": calls}))
    source = tmp_path / "daemon-source"
    source.write_bytes(b"source under acceptance")
    output = tmp_path / "evidence.json"
    requests = []

    def fake_exchange(socket_path, payload, timeout, received):
        assert socket_path == "/explicit/test-only.sock"
        assert timeout == 2
        requests.append(payload)
        value = "gold line" if live_text == "gold" else live_text
        received.extend(("META asr_ms=7\n" + value + "\n").encode())

    monkeypatch.setattr(probe, "exchange", fake_exchange)
    result = probe.main(["--socket", "/explicit/test-only.sock", "--source", str(source),
                         "--reference", str(baseline), "--output", str(output), "--timeout", "2"])
    evidence = json.loads(output.read_text())
    assert result == (0 if live_text == "gold" else 1)
    assert evidence["status"] == ("ok" if live_text == "gold" else "failed")
    assert len(evidence["comparisons"]) == len(requests) == 3
    assert evidence["source_sha256"] == probe.sha256(source.read_bytes())
    assert evidence["baseline_sha256"] == probe.sha256(baseline.read_bytes())
    assert evidence["baseline_path"] == str(baseline.resolve())
    for index, row in enumerate(evidence["comparisons"]):
        assert row["original_calls"][0]["id"] == index * 2
        assert row["original_calls"][0]["text"] == "gold\nline"
        assert row["expected_one_line"] == "gold line"
        assert row["exact_match"] is (live_text == "gold")
        assert row["raw_reply_utf8"]
        assert row["asr_ms"] == 7
        assert requests[index] == (clips[index]["path"] + "\nterms\n").encode()
        assert Path(clips[index]["context_path"]).read_bytes() == b"terms\n"


@pytest.mark.parametrize("missing", ["--reference", "--source"])
def test_acceptance_requires_explicit_reference_and_source(probe, tmp_path, capsys, missing):
    output = tmp_path / "evidence.json"
    options = {"--socket": "/explicit/test-only.sock", "--output": output,
               "--reference": tmp_path / "reference.json", "--source": tmp_path / "daemon"}
    argv = [part for option, value in options.items() if option != missing
            for part in (option, str(value))]
    with pytest.raises(SystemExit) as caught:
        probe.main(argv)
    assert caught.value.code == 2
    assert missing in capsys.readouterr().err
    assert not output.exists()


def test_acceptance_absolute_deadline_does_not_reset_on_partial_replies(probe, monkeypatch):
    ticks = iter([0, 0.1, 0.2, 0.6, 1.1])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    operations = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            operations.append("closed")

        def settimeout(self, value):
            operations.append(("timeout", value))

        def connect(self, path):
            operations.append(("connect", path))

        def sendall(self, data):
            operations.append(("send", data))

        def shutdown(self, how):
            operations.append(("shutdown", how))

        def recv(self, size):
            return b"META "

    monkeypatch.setattr(probe.socket, "socket", lambda *args: Socket())
    raw = bytearray()
    with pytest.raises(TimeoutError, match="absolute deadline"):
        probe.exchange("/fake.sock", b"clip\nctx\n", 1, raw)
    assert raw == b"META "
    assert ("connect", "/fake.sock") in operations
    assert operations[-1] == "closed"

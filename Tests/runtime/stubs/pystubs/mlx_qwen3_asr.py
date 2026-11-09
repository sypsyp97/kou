"""Stand-in for the real `mlx_qwen3_asr` package.

Put on PYTHONPATH so `zh-voice-daemon` can start in milliseconds without the
1.7B model. Every transcribe() is appended to $STUB_LOG_DIR/asr-transcribe.log
so tests can see which files the daemon fed it - the first one is the warm-up
scratch wav.

  STUB_ASR_RAISE_ON  substring of the wav path that makes transcribe() raise
  STUB_ASR_HANG_ON   substring of the wav path that makes transcribe() never
                     return, standing in for a wedged Metal call
  STUB_ASR_EMPTY_WITH_CONTEXT=1  return nothing when a context was supplied,
                     standing in for term biasing that suppresses the output
"""
import json
import os
import sys
import time
import types

# Copy the structure of the real package: `transcribe` is both a submodule and a
# function of the same name that the package __init__ re-exports. The daemon
# pin_tokenizer_dir must take the module from sys.modules (`import pkg.transcribe
# as T` returns the function, and setting an attribute on it silently has no
# effect). The stub needs the same shape, or the tests do not exercise the real
# structure.
_transcribe_module = types.ModuleType("mlx_qwen3_asr.transcribe")
_transcribe_module.DEFAULT_MODEL_ID = "Qwen/Qwen3-ASR-0.6B"
sys.modules["mlx_qwen3_asr.transcribe"] = _transcribe_module


class Tokenizer:
    """CPU prompt template with the installed tokenizer's public interface."""

    AUDIO_TOKEN_ID = 151676
    AUDIO_START_TOKEN_ID = 151669
    AUDIO_END_TOKEN_ID = 151670

    def encode(self, text, add_special_tokens=False):
        return [1000 + ord(char) for char in text]

    def build_prompt_tokens(self, n_audio_tokens, language=None, context=""):
        prefix = [151644, 20] + self.encode(context) + [151645, 21, 151644, 22,
                                                       self.AUDIO_START_TOKEN_ID]
        return (prefix + [self.AUDIO_TOKEN_ID] * n_audio_tokens
                + [self.AUDIO_END_TOKEN_ID, 151645, 21, 151644, 23]
                + self.encode(language or ""))


class _TokenizerHolder:
    _cache = {}

    @classmethod
    def get(cls, model_path):
        if model_path not in cls._cache:
            cls._cache[model_path] = Tokenizer()
        return cls._cache[model_path]


_transcribe_module._TokenizerHolder = _TokenizerHolder
_tokenizer_module = types.ModuleType("mlx_qwen3_asr.tokenizer")
_tokenizer_module.Tokenizer = Tokenizer
_tokenizer_module._TokenizerHolder = _TokenizerHolder
sys.modules["mlx_qwen3_asr.tokenizer"] = _tokenizer_module


class Result(object):
    def __init__(self, text):
        self.text = text


def _record(path, context):
    log = os.environ.get("STUB_LOG_DIR")
    if not log:
        return
    with open(os.path.join(log, "asr-transcribe.log"), "a") as fh:
        fh.write(json.dumps({"path": os.path.realpath(str(path)),
                             "raw": str(path),
                             "context": context or "",
                             # Where this transcription actually looks for the tokenizer.
                             # The real package reads this module attribute at this point
                             # (_resolve_model_components in transcribe.py).
                             "tokenizer_from": _transcribe_module.DEFAULT_MODEL_ID}) + "\n")


class StubCache:
    def __init__(self, max_seq_len=None):
        self.keys = [None]
        self.values = [None]
        self.offset = 0
        self.max_seq_len = max_seq_len


class StubModel:
    """Public prefill/cache surface without model weights or Metal."""

    audio_token_id = Tokenizer.AUDIO_TOKEN_ID

    def __init__(self, name):
        self._resolved_model_path = str(name)

    def create_cache(self, max_seq_len=None):
        return StubCache(max_seq_len)

    def model(self, input_ids=None, inputs_embeds=None, position_ids=None,
              attention_mask=None, cache=None):
        import mlx.core as mx
        data = mx.array(input_ids)[..., None] if inputs_embeds is None else inputs_embeds
        key = (data + position_ids[:, 0, :, None])[:, None]
        value = data[:, None]
        end = cache.offset + data.shape[1]
        if cache.max_seq_len is None:
            cache.keys[0] = key if cache.keys[0] is None else mx.concatenate(
                [cache.keys[0], key], axis=2)
            cache.values[0] = value if cache.values[0] is None else mx.concatenate(
                [cache.values[0], value], axis=2)
        else:
            if end > cache.max_seq_len:
                raise ValueError("stub KV cache overflow")
            if cache.keys[0] is None:
                shape = (data.shape[0], 1, cache.max_seq_len, data.shape[2])
                cache.keys[0] = mx.zeros(shape, dtype=key.dtype)
                cache.values[0] = mx.zeros(shape, dtype=value.dtype)
            cache.keys[0][..., cache.offset:end, :] = key
            cache.values[0][..., cache.offset:end, :] = value
        cache.offset = end
        return data

    def prefill(self, input_ids, audio_features, position_ids, cache):
        embeds = input_ids[..., None].copy()
        for batch in range(input_ids.shape[0]):
            embeds[batch, input_ids[batch] == self.audio_token_id] = audio_features[batch]
        return self.model(inputs_embeds=embeds, position_ids=position_ids,
                          cache=cache)[:, -1:, :]


def load_model(name, *a, **kw):
    return (StubModel(name), {"stub": True})


def transcribe(path, model=None, context=None, language=None, **kw):
    _record(path, context)
    raise_on = os.environ.get("STUB_ASR_RAISE_ON")
    if raise_on and raise_on in str(path):
        raise RuntimeError("stub ASR blew up on " + str(path))
    hang_on = os.environ.get("STUB_ASR_HANG_ON")
    if hang_on and hang_on in str(path):
        # Stand-in for a hung Metal call. The real call hangs in C, and no Python
        # signal or exception can reach it. The sleep length does not matter,
        # because the only way out is os._exit from another thread.
        while True:
            time.sleep(3600)
    if not path or not os.path.exists(path):
        raise FileNotFoundError("no such wav: %r" % (path,))
    if isinstance(model, StubModel):
        import mlx.core as mx
        tokenizer = _TokenizerHolder.get(model._resolved_model_path)
        prompt = tokenizer.build_prompt_tokens(2, language=language, context=context or "")
        ids = mx.array([prompt])
        pos = mx.arange(len(prompt))[None, :]
        model.prefill(input_ids=ids, audio_features=mx.zeros((1, 2, 1), dtype=ids.dtype),
                      position_ids=mx.stack([pos, pos, pos], axis=1),
                      cache=model.create_cache(max_seq_len=len(prompt) + 8))
    if context and os.environ.get("STUB_ASR_EMPTY_WITH_CONTEXT") == "1":
        return Result("")
    text = "转写:" + os.path.basename(str(path))
    if context:
        text = text + "|ctx=" + str(context)
    return Result(text)

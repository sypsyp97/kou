#!/usr/bin/env python3
"""Compare an explicit daemon socket with the three original ASR baseline calls.

This probe reads an explicit baseline JSON and paired WAV/.ctx files.
It writes only a new evidence JSON file. It does not change the corpus,
packages, daemon, or model. No MLX import is needed.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import socket
import time

MAX_REPLY_BYTES = 1024 * 1024


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def one_line(text):
    """Use the daemon's transcript normalization, not whitespace folding."""
    return " ".join(text.splitlines()).strip()


def load_gold(path):
    raw = path.read_bytes()
    data = json.loads(raw)
    if data["status"] != "ok" or data["completed"] is not True:
        raise ValueError("Original benchmark did not complete successfully")
    clips = data["clips"]
    if len(clips) != 3 or len({clip["path"] for clip in clips}) != 3:
        raise ValueError("Original benchmark must contain three distinct clips")
    gold = []
    for clip in clips:
        # Never derive gold from prefix/candidate rows or stored corpus .txt.
        originals = [call for call in data["calls"]
                     if call["mode"] == "baseline" and call["clip_path"] == clip["path"]]
        if not originals:
            raise ValueError("Missing original q.transcribe call: " + clip["path"])
        texts = []
        for call in originals:
            if (call["status"] != "ok" or call["clip_sha256"] != clip["sha256"]
                    or call["context_sha256"] != clip["context_sha256"]
                    or sha256(call["text"].encode("utf-8")) != call["text_sha256"]):
                raise ValueError("Original call failed validation: " + str(call["id"]))
            text = one_line(call["text"])
            if not text or text.startswith("ERROR:"):
                raise ValueError("Original transcript is empty or an error")
            texts.append(text)
        if len(set(texts)) != 1:
            raise ValueError("Original calls disagree: " + clip["path"])
        wav = Path(clip["path"])
        context_path = Path(clip["context_path"])
        if not wav.is_absolute() or context_path != wav.with_suffix(".ctx"):
            raise ValueError("Original clip must have its paired absolute .ctx path")
        gold.append({"wav": str(wav), "wav_sha256": clip["sha256"],
                     "context_path": str(context_path),
                     "context_sha256": clip["context_sha256"],
                     "original_calls": [{"id": call["id"], "text": call["text"],
                                          "text_sha256": call["text_sha256"]}
                                         for call in originals],
                     "expected_one_line": texts[0]})
    return gold, sha256(raw)


def compare_reply(raw, expected):
    result = {"raw_reply_base64": base64.b64encode(raw).decode("ascii"),
              "raw_reply_utf8": raw.decode("utf-8", "replace"),
              "asr_ms": None, "live_one_line": None, "exact_match": False}
    try:
        text = raw.decode("utf-8")
        if text.startswith("ERROR:"):
            raise ValueError("Daemon returned ERROR")
        lines = text.split("\n")
        if len(lines) != 3 or lines[-1] != "":
            raise ValueError("Expected META and exactly one transcript line with final newline")
        match = re.fullmatch(r"META asr_ms=(\d+)", lines[0])
        if match is None:
            raise ValueError("Missing or invalid META asr_ms")
        result["asr_ms"] = int(match[1])
        live = one_line(lines[1])
        result["live_one_line"] = live
        if not live or live.startswith("ERROR:"):
            raise ValueError("Daemon transcript is empty or an error")
        if live != expected:
            raise ValueError("Transcript differs from original q.transcribe text")
        result["exact_match"] = True
        result["status"] = "ok"
    except (UnicodeError, ValueError) as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
    return result


def exchange(socket_path, payload, timeout, received):
    """One absolute deadline covers connect, send, and all receive calls."""
    deadline = time.monotonic() + timeout

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Daemon request exceeded its absolute deadline")
        return budget

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(remaining())
        conn.connect(socket_path)
        conn.settimeout(remaining())
        conn.sendall(payload)
        conn.shutdown(socket.SHUT_WR)
        while True:
            conn.settimeout(remaining())
            chunk = conn.recv(min(65536, MAX_REPLY_BYTES + 1 - len(received)))
            if not chunk:
                return
            received.extend(chunk)
            if len(received) > MAX_REPLY_BYTES:
                raise ValueError("Daemon reply exceeded 1 MiB")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, help="Explicit absolute AF_UNIX socket path")
    parser.add_argument("--output", required=True, type=Path, help="New evidence JSON path")
    parser.add_argument("--reference", required=True, type=Path, help="Original baseline JSON path")
    parser.add_argument("--source", required=True, type=Path,
                        help="Daemon source to hash; does not attest the running socket process")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="Absolute seconds per request, in (0, 180]; default: 20")
    args = parser.parse_args(argv)
    if not Path(args.socket).is_absolute():
        parser.error("--socket must be an explicit absolute path")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 180:
        parser.error("--timeout must be finite and in (0, 180]")
    output = args.output.expanduser().resolve()
    reference = args.reference.expanduser().resolve()
    source = args.source.expanduser().resolve()
    if output.exists():
        parser.error("--output must not already exist")
    if "site-packages" in output.parts or "dist-packages" in output.parts:
        parser.error("--output must not be inside an installed package directory")
    try:
        gold, baseline_hash = load_gold(reference)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error("Cannot validate original baseline: " + str(exc))
    for item in gold:
        if output.is_relative_to(Path(item["wav"]).parent.resolve()):
            parser.error("--output must not write to the corpus")
    if output in (source, reference, Path(__file__).resolve()):
        parser.error("--output must not overwrite a source file")

    evidence = {"schema_version": 1, "status": "failed", "socket": args.socket,
                "baseline_path": str(reference), "baseline_sha256": baseline_hash,
                "gold_source": "original mode=baseline q.transcribe calls only",
                "timeout_sec_per_request": args.timeout, "comparisons": [],
                "source_path": str(source), "source_sha256": None,
                "probe_sha256": sha256(Path(__file__).read_bytes()),
                "source_attestation": "Supplied file hash only; socket process not attested",
                "errors": []}
    started = time.monotonic()
    try:
        evidence["source_sha256"] = sha256(source.read_bytes())
        for original in gold:
            row = dict(original)
            received = bytearray()
            row_started = time.monotonic()
            try:
                wav = Path(original["wav"])
                context_path = Path(original["context_path"])
                actual_wav_hash = sha256(wav.read_bytes())
                context_bytes = context_path.read_bytes()
                actual_context_hash = sha256(context_bytes)
                row["observed_wav_sha256"] = actual_wav_hash
                row["observed_context_sha256"] = actual_context_hash
                if (actual_wav_hash != original["wav_sha256"]
                        or actual_context_hash != original["context_sha256"]):
                    raise ValueError("WAV or paired .ctx differs from original benchmark")
                context = context_bytes.decode("utf-8")
                wire_context = context.rstrip("\r\n")
                if "\n" in wire_context or "\r" in wire_context or "\n" in str(wav):
                    raise ValueError("Request cannot fit the daemon's two-line protocol")
                row["context_utf8"] = context
                row["wire_context"] = wire_context
                row["worker_context"] = wire_context.strip()
                row["context_note"] = "Protocol removes terminal newlines; daemon strips its context line"
                payload = (str(wav) + "\n" + wire_context + "\n").encode("utf-8")
                row["request_sha256"] = sha256(payload)
                exchange(args.socket, payload, args.timeout, received)
                row.update(compare_reply(bytes(received), original["expected_one_line"]))
                # Read-only checks catch source/corpus drift during an exchange.
                if (sha256(wav.read_bytes()) != actual_wav_hash
                        or sha256(context_path.read_bytes()) != actual_context_hash):
                    raise ValueError("Corpus changed during the request")
            except (OSError, UnicodeError, ValueError) as exc:
                row.update(compare_reply(bytes(received), original["expected_one_line"]))
                row.update(status="failed", exact_match=False,
                           error=f"{type(exc).__name__}: {exc}")
            row["elapsed_sec"] = time.monotonic() - row_started
            evidence["comparisons"].append(row)
        if sha256(source.read_bytes()) != evidence["source_sha256"]:
            raise ValueError("Daemon source changed during acceptance")
        if sha256(reference.read_bytes()) != baseline_hash:
            raise ValueError("Original baseline changed during acceptance")
        if len(evidence["comparisons"]) == 3 and all(
                row["status"] == "ok" and row["exact_match"]
                for row in evidence["comparisons"]):
            evidence["status"] = "ok"
    except (OSError, ValueError) as exc:
        evidence["errors"].append(f"{type(exc).__name__}: {exc}")
    evidence["elapsed_sec"] = time.monotonic() - started
    with output.open("x", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"status": evidence["status"], "output": str(output),
                      "comparisons": len(evidence["comparisons"])}, ensure_ascii=False))
    return 0 if evidence["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

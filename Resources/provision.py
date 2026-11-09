"""Provision pinned model snapshots. Runtime inference remains offline."""
import importlib.metadata
import json
import os
from pathlib import Path
import sys

from huggingface_hub import snapshot_download


def main():
    if importlib.metadata.version("mlx-qwen3-asr") != "0.4.4":
        raise RuntimeError("Kou requires mlx-qwen3-asr 0.4.4")
    manifest = json.loads(Path(sys.argv[1]).read_text())
    profile = sys.argv[2]
    if profile not in ("large", "small"):
        raise ValueError("Unknown model profile")
    paths = {}
    for key in (profile, "vad"):
        spec = manifest[key]
        print(f"Preparing {spec['repo']} @ {spec['revision']}", flush=True)
        options = {"repo_id": spec["repo"], "revision": spec["revision"]}
        if key == "vad":
            options["allow_patterns"] = ["silero_vad.mlmodelc/**", "config.json"]
        try:
            path = snapshot_download(**options, local_files_only=True)
        except Exception:
            path = snapshot_download(**options)
        root = Path(path)
        required = (["silero_vad.mlmodelc/model.mil", "silero_vad.mlmodelc/weights/weight.bin"]
                    if key == "vad" else
                    ["config.json", "model.safetensors", "vocab.json", "merges.txt", "tokenizer_config.json"])
        for name in required:
            if not (root / name).is_file() or (root / name).stat().st_size == 0:
                raise RuntimeError(f"Missing model file: {root / name}")
        paths[key] = str(root)
    result = {"profile": profile, "model": manifest[profile]["repo"],
              "revision": manifest[profile]["revision"], "modelPath": paths[profile],
              "vadPath": str(Path(paths["vad"]) / "silero_vad.mlmodelc")}
    output = Path(sys.argv[3])
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.chmod(0o600)
    os.replace(temporary, output)
    print("Models ready", flush=True)


if __name__ == "__main__":
    main()

"""Build or explicitly run the isolated native paste acceptance probe."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "Tests" / "ClipboardPlatformProbe.swift"
PASTER = ROOT / "Sources" / "Kou" / "ClipboardPaster.swift"


def compile_probe(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "swiftc", "-swift-version", "5", "-module-name", "KouPastePlatformProbe",
        str(PASTER), str(PROBE), "-o", str(output),
    ], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compile_command = commands.add_parser("compile", help="Compile only; do not sign or launch")
    compile_command.add_argument("--output", type=Path, required=True)
    build = commands.add_parser("build", help="Build and sign a new staging app; do not launch")
    build.add_argument("--app", type=Path, required=True)
    build.add_argument("--identity", required=True, help="Explicit stable certificate; no discovery")
    run = commands.add_parser("run", help="Explicitly launch the previously staged probe")
    run.add_argument("--app", type=Path, required=True)
    run.add_argument("--result", type=Path, required=True)
    run.add_argument("--append-history", action="store_true", help="Keep previous result in the same JSON history")
    args = parser.parse_args()
    if args.command == "compile":
        output = args.output.resolve()
        if output.exists():
            parser.error("--output must be a new staging file")
        compile_probe(output)
        print(json.dumps({"status": "compiled", "executable": str(output), "launched": False}))
        return 0

    app = args.app.resolve()
    if app.suffix != ".app":
        parser.error("--app must name a staging .app bundle")
    executable = app / "Contents" / "MacOS" / "Kou"
    marker = app / "Contents" / "Resources" / "KouPastePlatformProbe.marker"
    if args.command == "build":
        if app.exists():
            parser.error("Refusing to replace an existing app; choose a new staging path")
        if not args.identity.strip() or args.identity == "-":
            parser.error("A stable explicit signing identity is required")
        compile_probe(executable)
        with (ROOT / "Resources" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        info.update(CFBundleExecutable="Kou", CFBundleName="Kou Paste Platform Probe",
                    CFBundleDisplayName="Kou Paste Platform Probe", LSUIElement=False)
        info.pop("CFBundleIconFile", None)
        with (app / "Contents" / "Info.plist").open("wb") as stream:
            plistlib.dump(info, stream)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("Isolated named pasteboard acceptance probe\n")
        subprocess.run(["codesign", "--force", "--options", "runtime", "--timestamp",
                        "--sign", args.identity, str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        print(json.dumps({"status": "staged", "app": str(app), "bundleIdentifier": info["CFBundleIdentifier"], "launched": False}))
        return 0

    if not marker.is_file() or not executable.is_file():
        parser.error("--app is not a staged paste probe")
    result = args.result.resolve()
    if result.exists() and not args.append_history:
        parser.error("Refusing to replace an existing result")
    result.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    previous = None
    if result.exists():
        previous = json.loads(result.read_text())
        if not isinstance(previous, dict):
            parser.error("Existing result must be a JSON object")
    with tempfile.TemporaryDirectory(prefix="kou-paste-result-", dir=result.parent) as scratch:
        current = Path(scratch) / "result.json"
        try:
            process = subprocess.run([str(executable), "--result", str(current)], timeout=12)
        except subprocess.TimeoutExpired:
            print(json.dumps({"status": "failed", "reason": "launcher-timeout"}))
            return 1
        if current.exists():
            observed = json.loads(current.read_text())
            if previous is not None:
                history = previous.pop("history", [])
                observed["history"] = [*history, previous]
            current.write_text(json.dumps(observed, ensure_ascii=False, sort_keys=True) + "\n")
            os.replace(current, result)
        return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())

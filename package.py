#!/usr/bin/env python3
"""Create a release archive and a cask with the archive's actual checksum."""
import argparse
import hashlib
from pathlib import Path
import plistlib
import re
import subprocess
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=Path(__file__).parent / "dist/Kou.app")
    parser.add_argument("--url", required=True, help="Final download URL for this archive")
    args = parser.parse_args()
    app = args.app.resolve()
    parsed = urlparse(args.url)
    if parsed.scheme not in ("https", "file") or any(char in args.url for char in ('"', "'", "\\", "\n", "\r", "#{")):
        parser.error("Use an HTTPS or local file URL without Ruby interpolation")
    with (app / "Contents/Info.plist").open("rb") as stream:
        version = plistlib.load(stream)["CFBundleShortVersionString"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        parser.error("App version must use major.minor.patch")
    archive = app.parent / f"Kou-{version}-arm64.zip"
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    # Homebrew audit expects the version in the URL to come from the version stanza.
    cask_url = args.url.replace(version, "#{version}")
    cask = f'''cask "kou" do
  version "{version}"
  sha256 "{digest}"

  url "{cask_url}"
  name "Kou"
  desc "Local Mandarin dictation with a modifier-key shortcut"
  homepage "https://github.com/sypsyp97/kou"

  depends_on arch: :arm64
  depends_on formula: "opencc"
  depends_on formula: "uv"
  depends_on macos: :sonoma

  app "Kou.app"
  binary "#{{appdir}}/Kou.app/Contents/MacOS/Kou", target: "kou"

  uninstall quit: "io.github.sypsyp97.Kou"

  zap trash: [
    "~/Library/Application Support/Kou",
    "~/Library/Caches/Kou",
    "~/Library/Preferences/io.github.sypsyp97.Kou.plist",
  ]
end
'''
    target = app.parent / "kou.rb"
    target.write_text(cask)
    print(f"Archive: {archive}\nSHA256: {digest}\nCask: {target}")


if __name__ == "__main__":
    main()

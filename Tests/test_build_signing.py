"""Run the build script with isolated storage and fake signing/build tools."""

import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys

import pytest


IDENTITY_A = "A" * 40
IDENTITY_B = "B" * 40
BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "build.sh"

TOOL = r'''
import json
import os
from pathlib import Path
import plistlib
import sys

root = Path(os.environ["SIGN_TEST_ROOT"]).resolve()
command = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps([command, args]) + "\n")

def writable(path):
    p = Path(path).resolve()
    if not p.is_relative_to(root):
        raise SystemExit("Mock refused write outside test sandbox: " + str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

if command == "security":
    choices = json.loads(os.environ.get("SIGN_TEST_IDENTITIES", "[]"))
    for number, identity in enumerate(choices, 1):
        print(f'  {number}) {identity} "Certificate {number}"')
    print(f"     {len(choices)} valid identities found")
elif command == "swift":
    binary = writable(root / "bin" / "Kou")
    binary.write_text(os.environ.get("SIGN_TEST_GENERATION", "first"))
    if "--show-bin-path" in args:
        print(binary.parent)
elif command == "xcrun":
    if args == ["--show-sdk-version"]:
        print("26.0")
    elif args[0] == "vtool":
        writable(args[args.index("-output") + 1]).write_bytes(Path(args[-1]).read_bytes())
    elif args[0] == "swift":
        writable(args[-1]).mkdir(exist_ok=True)
    elif "-o" in args:
        writable(args[args.index("-o") + 1]).write_text("fake native resource")
    else:
        raise SystemExit("Unexpected xcrun invocation")
elif command == "codesign":
    target = Path(args[-1])
    if "--sign" in args:
        identity = args[args.index("--sign") + 1]
        signable = json.loads(os.environ.get("SIGN_TEST_SIGNABLE", "[]"))
        if identity != "-" and identity not in signable:
            raise SystemExit("Requested identity is unavailable")
        if os.environ.get("SIGN_TEST_FAIL_SIGN") == "1":
            raise SystemExit("Mock signing failure")
        if target.suffix == ".app":
            with (target / "Contents" / "Info.plist").open("rb") as stream:
                bundle = plistlib.load(stream)["CFBundleIdentifier"]
            requirement = f'identifier "{bundle}" and certificate leaf = H"{identity}"'
            writable(target / "Contents" / ".test-requirement").write_text(requirement)
    elif "--verify" in args:
        if os.environ.get("SIGN_TEST_FAIL_VERIFY") == "1":
            raise SystemExit("Mock verification failure")
    elif "-d" in args:
        marker = target / "Contents" / ".test-requirement"
        if not marker.exists():
            raise SystemExit("Cannot read designated requirement")
        is_previous = target.resolve() == (root / "output" / "Kou.app").resolve()
        selected = os.environ.get("SIGN_TEST_REQUIREMENT_TARGET")
        if selected == ("previous" if is_previous else "candidate"):
            print(os.environ["SIGN_TEST_REQUIREMENT_OUTPUT"], end="", file=sys.stderr)
            raise SystemExit(0)
        print("designated => " + marker.read_text(), file=sys.stderr)
    else:
        raise SystemExit("Unexpected codesign invocation")
elif command == "plutil":
    with Path(args[-1]).open("rb") as stream:
        plistlib.load(stream)
else:
    raise SystemExit("Unexpected mock tool: " + command)
'''


class BuildSandbox:
    def __init__(self, root):
        self.root = root
        self.home = root / "home"
        self.output = root / "output"
        self.tools = root / "tools"
        self.home.mkdir()
        self.tools.mkdir()
        temporary = root / "tmp"
        temporary.mkdir()
        for name in ("security", "swift", "xcrun", "codesign", "plutil"):
            tool = self.tools / name
            tool.write_text("#!" + sys.executable + "\n" + TOOL)
            tool.chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "TMPDIR": str(temporary),
            "PATH": str(self.tools) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "KOU_BUILD_DIR": str(self.output),
            "SIGN_TEST_ROOT": str(root),
            "SIGN_TEST_IDENTITIES": json.dumps([IDENTITY_A]),
            "SIGN_TEST_SIGNABLE": json.dumps([IDENTITY_A, IDENTITY_B]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.identity_file = self.home / "Library" / "Application Support" / "Kou" / "build-signing-identity"
        self.app = self.output / "Kou.app"

    def run(self, identity=None, identities=None, generation="first", **extra):
        env = dict(self.env, SIGN_TEST_GENERATION=generation)
        if identity is not None:
            env["KOU_SIGN_IDENTITY"] = identity
        if identities is not None:
            env["SIGN_TEST_IDENTITIES"] = json.dumps(identities)
        env.update(extra)
        return subprocess.run(
            ["/bin/bash", str(BUILD_SCRIPT)], env=env,
            cwd=self.root, capture_output=True, text=True, timeout=20,
        )

    def calls(self, command):
        log = self.root / "calls.jsonl"
        if not log.exists():
            return []
        return [args for name, args in map(json.loads, log.read_text().splitlines()) if name == command]

    def signatures(self):
        return [args[args.index("--sign") + 1] for args in self.calls("codesign") if "--sign" in args]

    def bundle_id(self):
        with (self.app / "Contents" / "Info.plist").open("rb") as stream:
            return plistlib.load(stream)["CFBundleIdentifier"]

    def snapshot(self):
        return {str(path.relative_to(self.app)): path.read_bytes() for path in self.app.rglob("*") if path.is_file()}


@pytest.fixture
def build(tmp_path):
    return BuildSandbox(tmp_path)


@pytest.mark.parametrize("identities", [[], [IDENTITY_A, IDENTITY_B]], ids=["no-certificate", "ambiguous-certificates"])
def test_first_build_requires_unambiguous_stable_identity(build, identities):
    result = build.run(identities=identities)
    assert result.returncode != 0, "Build must request an explicit signing choice"
    assert not build.app.exists()
    assert not build.identity_file.exists()
    assert build.signatures() == [], "Ambiguous discovery must not start signing"


def test_single_identity_is_selected_and_persisted_privately(build):
    result = build.run()
    assert result.returncode == 0, result.stderr
    assert build.app.is_dir()
    assert build.identity_file.read_text().strip() == IDENTITY_A
    assert stat.S_IMODE(build.identity_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(build.identity_file.parent.stat().st_mode) == 0o700
    assert build.signatures() and set(build.signatures()) == {IDENTITY_A}
    assert build.bundle_id()
    assert str(build.app) in result.stdout


@pytest.mark.parametrize("identities", [[], [IDENTITY_A, IDENTITY_B]], ids=["discovery-empty", "discovery-ambiguous"])
def test_rebuild_reuses_persisted_identity_and_bundle_id(build, identities):
    initial = build.run()
    assert initial.returncode == 0, initial.stderr
    bundle_id = build.bundle_id()
    before = len(build.signatures())
    result = build.run(identities=identities, generation="rebuilt")
    assert result.returncode == 0, result.stderr
    assert build.bundle_id() == bundle_id
    assert (build.app / "Contents" / "MacOS" / "Kou").read_text() == "rebuilt"
    assert build.signatures()[before:]
    assert set(build.signatures()[before:]) == {IDENTITY_A}
    assert build.identity_file.read_text().strip() == IDENTITY_A


def test_explicit_identity_resolves_ambiguous_discovery(build):
    result = build.run(identity=IDENTITY_B, identities=[IDENTITY_A, IDENTITY_B])
    assert result.returncode == 0, result.stderr
    assert build.signatures() and set(build.signatures()) == {IDENTITY_B}
    assert build.identity_file.read_text().strip() == IDENTITY_B


@pytest.mark.parametrize("persisted", [False, True], ids=["explicit-ad-hoc", "persisted-ad-hoc"])
def test_ad_hoc_identity_is_rejected_without_signing(build, persisted):
    if persisted:
        build.identity_file.parent.mkdir(parents=True)
        build.identity_file.write_text("-\n")
        result = build.run()
    else:
        result = build.run(identity="-")
    assert result.returncode != 0, "Ad-hoc signing cannot preserve permissions across builds"
    assert not build.app.exists()
    assert "-" not in build.signatures(), "Build attempted ad-hoc signing"


@pytest.mark.parametrize("failure", ["unavailable", "signing", "verification"])
def test_failed_first_build_does_not_persist_identity_or_publish_bundle(build, failure):
    extra = {}
    if failure == "unavailable":
        extra["SIGN_TEST_SIGNABLE"] = "[]"
    elif failure == "signing":
        extra["SIGN_TEST_FAIL_SIGN"] = "1"
    else:
        extra["SIGN_TEST_FAIL_VERIFY"] = "1"
    result = build.run(identity=IDENTITY_A, **extra)
    assert result.returncode != 0
    assert not build.identity_file.exists(), "Failed signing must not select a future identity"
    assert not build.app.exists(), "Failed signing must not publish an app"
    assert "-" not in build.signatures(), "Signing failure must not fall back to ad-hoc"


def test_changed_designated_requirement_preserves_previous_bundle(build):
    initial = build.run()
    assert initial.returncode == 0, initial.stderr
    previous = build.snapshot()
    result = build.run(identity=IDENTITY_B, generation="must-not-replace")
    assert result.returncode != 0, "Changed designated requirement must block replacement"
    assert build.snapshot() == previous, "Rejected build replaced or damaged the prior app"
    assert build.identity_file.read_text().strip() == IDENTITY_A
    reads = [args[-1] for args in build.calls("codesign") if "-d" in args]
    assert str(build.app) in reads, "Build did not inspect the previous designated requirement"
    assert any(path != str(build.app) for path in reads), "Build did not inspect the candidate requirement"


def test_failed_rebuild_preserves_previous_bundle_and_identity(build):
    initial = build.run()
    assert initial.returncode == 0, initial.stderr
    previous = build.snapshot()
    result = build.run(generation="broken", SIGN_TEST_FAIL_VERIFY="1")
    assert result.returncode != 0
    assert build.snapshot() == previous
    assert build.identity_file.read_text().strip() == IDENTITY_A
    assert "-" not in build.signatures()


UNEXTRACTABLE_REQUIREMENTS = [
    pytest.param("", id="empty-output"),
    pytest.param("Executable=Kou.app\nFormat=app bundle\n", id="unrecognized-output"),
    pytest.param("designated => \n", id="empty-designated-requirement"),
]


@pytest.mark.parametrize("output", UNEXTRACTABLE_REQUIREMENTS)
def test_unextractable_previous_requirement_refuses_replacement(build, output):
    initial = build.run()
    assert initial.returncode == 0, initial.stderr
    previous = build.snapshot()
    identity = build.identity_file.read_bytes()
    result = build.run(
        generation="must-not-replace",
        SIGN_TEST_REQUIREMENT_TARGET="previous",
        SIGN_TEST_REQUIREMENT_OUTPUT=output,
    )
    assert result.returncode != 0, "Success from codesign does not establish an extracted requirement"
    assert build.snapshot() == previous, "Unknown previous requirement allowed replacement"
    assert build.identity_file.read_bytes() == identity


@pytest.mark.parametrize("output", UNEXTRACTABLE_REQUIREMENTS)
@pytest.mark.parametrize("existing_app", [False, True], ids=["fresh-build", "rebuild"])
def test_unextractable_candidate_requirement_refuses_publication(build, output, existing_app):
    previous = None
    if existing_app:
        initial = build.run()
        assert initial.returncode == 0, initial.stderr
        previous = build.snapshot()
    result = build.run(
        generation="must-not-publish",
        SIGN_TEST_REQUIREMENT_TARGET="candidate",
        SIGN_TEST_REQUIREMENT_OUTPUT=output,
    )
    assert result.returncode != 0, "Candidate app lacks a usable designated requirement"
    if existing_app:
        assert build.snapshot() == previous, "Unknown candidate requirement replaced the prior app"
        assert build.identity_file.read_text().strip() == IDENTITY_A
    else:
        assert not build.app.exists(), "Fresh build published an app with an unknown requirement"
        assert not build.identity_file.exists(), "Failed first build persisted an identity"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))

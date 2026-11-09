"""Process-level tests for kou-run; all subprocesses belong to this test run."""

from pathlib import Path
import os
import signal
import subprocess
import sys
import time

import pytest


TREE = r'''
from pathlib import Path
import os
import signal
import subprocess
import sys
import time

role, folder, mode = sys.argv[1:]
root = Path(folder)

def save(signum, frame):
    time.sleep(0.25 if role == "leader" else 0.05)
    (root / (role + ".saved")).write_text(str(signum))
    raise SystemExit(0)

def prearm(signum, frame):
    (root / (role + ".prearmed")).write_text("ready")

for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, save if mode == "graceful" else signal.SIG_IGN)
signal.signal(signal.SIGUSR1, prearm)

if role != "grandchild":
    next_role = "child" if role == "leader" else "grandchild"
    subprocess.Popen([sys.executable, __file__, next_role, folder, mode])
(root / (role + ".pid")).write_text(str(os.getpid()))
(root / (role + ".pgid")).write_text(str(os.getpgrp()))
while True:
    if role == "leader" and mode == "normal" and (root / "exit").exists():
        raise SystemExit(23)
    time.sleep(0.01)
'''


def eventually(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux containers can retain orphan zombies after they have stopped.
    status = Path(f"/proc/{pid}/status")
    try:
        if status.exists() and "\nState:\tZ" in status.read_text():
            return False
    except FileNotFoundError:
        return False
    return True


@pytest.fixture(scope="session")
def supervisor(tmp_path_factory):
    source = Path(__file__).resolve().parents[1] / "Support" / "kou-run.c"
    executable = tmp_path_factory.mktemp("supervisor-build") / "kou-run"
    result = subprocess.run(
        ["clang", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


@pytest.fixture
def process_tree(tmp_path, supervisor):
    script = tmp_path / "tree.py"
    script.write_text(TREE)
    owned = []
    extra_pids = set()
    logs = []

    def start(mode="graceful", through_owner=False):
        command = [str(supervisor), sys.executable, str(script), "leader", str(tmp_path), mode]
        log = (tmp_path / "output.log").open("wb")
        logs.append(log)
        if through_owner:
            owner_source = (
                "from pathlib import Path; import subprocess,sys,time; "
                "p=subprocess.Popen(sys.argv[2:]); "
                "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
            )
            owner = subprocess.Popen(
                [sys.executable, "-c", owner_source, str(tmp_path / "supervisor.pid"), *command],
                stdout=log, stderr=log,
            )
            owned.append(owner)
            assert eventually(lambda: (tmp_path / "supervisor.pid").exists()), "Owner did not start supervisor"
            supervisor_pid = int((tmp_path / "supervisor.pid").read_text())
            extra_pids.add(supervisor_pid)
        else:
            owner = None
            process = subprocess.Popen(command, stdout=log, stderr=log)
            owned.append(process)
            supervisor_pid = process.pid

        ready = lambda: all((tmp_path / (role + ".pid")).exists() for role in ("leader", "child", "grandchild"))
        assert eventually(ready), "The command tree did not become ready"
        pids = {role: int((tmp_path / (role + ".pid")).read_text()) for role in ("leader", "child", "grandchild")}
        extra_pids.update(pids.values())
        return supervisor_pid, pids, owner

    yield start, tmp_path, owned

    # Read only this test's files, including PIDs written during partial startup.
    for pid_file in tmp_path.glob("*.pid"):
        try:
            extra_pids.add(int(pid_file.read_text()))
        except (ValueError, OSError):
            pass
    for process in owned:
        if process.poll() is None:
            process.kill()
    for pid in extra_pids:
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for process in owned:
        process.wait(timeout=5)
    for log in logs:
        log.close()


@pytest.mark.parametrize("exit_code", [0, 7, 127])
def test_streams_arguments_and_exit_status(supervisor, exit_code):
    code = "import sys; print(sys.argv[1]); print('diagnostic', file=sys.stderr); sys.exit(int(sys.argv[2]))"
    process = subprocess.Popen(
        [str(supervisor), sys.executable, "-c", code, "argument with spaces", str(exit_code)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == exit_code
        assert stdout == "argument with spaces\n"
        assert stderr == "diagnostic\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_command_has_own_process_group(process_tree):
    start, root, _ = process_tree
    supervisor_pid, pids, _ = start()
    pgids = {role: int((root / (role + ".pgid")).read_text()) for role in pids}
    assert pgids["leader"] == pids["leader"]
    assert pgids["leader"] != os.getpgid(supervisor_pid)
    assert all(pgid == pgids["leader"] for pgid in pgids.values())


def test_usr1_prearms_command_without_stopping(process_tree):
    start, root, owned = process_tree
    supervisor_pid, pids, _ = start()
    os.kill(supervisor_pid, signal.SIGUSR1)
    assert eventually(lambda: (root / "leader.prearmed").exists()), "SIGUSR1 did not reach command"
    time.sleep(0.15)
    assert owned[0].poll() is None
    assert all(alive(pid) for pid in pids.values())
    os.kill(supervisor_pid, signal.SIGTERM)
    assert eventually(lambda: not any(alive(pid) for pid in pids.values()))


@pytest.mark.parametrize("cancel_signal", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_cancellation_reaches_descendants_and_allows_save(process_tree, cancel_signal):
    start, root, owned = process_tree
    supervisor_pid, pids, _ = start()
    os.kill(supervisor_pid, cancel_signal)
    assert eventually(lambda: all((root / (role + ".saved")).exists() for role in pids)), "Cancellation prevented graceful save"
    assert eventually(lambda: not any(alive(pid) for pid in pids.values()))
    owned[0].wait(timeout=4)


@pytest.mark.parametrize("cancel_signal", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_cancellation_escalates_for_unresponsive_descendants(process_tree, cancel_signal):
    start, _, owned = process_tree
    supervisor_pid, pids, _ = start(mode="stubborn")
    began = time.monotonic()
    os.kill(supervisor_pid, cancel_signal)
    assert eventually(lambda: not any(alive(pid) for pid in pids.values()), timeout=4.0), "Unresponsive descendants survived cancellation"
    assert time.monotonic() - began <= 4.0
    owned[0].wait(timeout=1)


def test_owner_death_stops_descendants(process_tree):
    start, _, _ = process_tree
    supervisor_pid, pids, owner = start(mode="stubborn", through_owner=True)
    owner.kill()
    owner.wait(timeout=2)
    assert eventually(lambda: not any(alive(pid) for pid in pids.values()), timeout=4.0), "Descendants survived owner death"
    assert eventually(lambda: not alive(supervisor_pid)), "Supervisor survived its owner"


def test_normal_leader_exit_reaps_remaining_descendants(process_tree):
    start, root, owned = process_tree
    _, pids, _ = start(mode="normal")
    (root / "exit").write_text("exit")
    assert eventually(lambda: not any(alive(pid) for pid in pids.values()), timeout=4.0), "Normal command exit left descendants running"
    assert owned[0].wait(timeout=1) == 23


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))

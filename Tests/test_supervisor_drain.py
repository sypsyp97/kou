"""Hold group SIGKILL delivery to test the supervisor's exit ordering."""

import os
from pathlib import Path
import signal
import subprocess

import pytest

from test_supervisor import alive, eventually, process_tree


SIGNAL_INSTRUMENTATION = r'''
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <unistd.h>

static pid_t held_group = 0;

int kou_test_kill(pid_t pid, int sig) {
    if (pid < -1 && sig == SIGKILL) {
        if (held_group == 0) {
            /* Model successful kill delivery before descendants can stop. */
            int result = kill(pid, SIGSTOP);
            if (result != 0) return result;
            held_group = pid;
            /* Reaping the leader alone must not count as group drainage. */
            (void)kill(-pid, SIGKILL);
            const char *path = getenv("KOU_TEST_DRAIN_MARKER");
            if (path == NULL) abort();
            FILE *marker = fopen(path, "w");
            if (marker == NULL) abort();
            fprintf(marker, "%ld\n", (long)pid);
            fclose(marker);
        }
        /* The test sends the real SIGKILL after checking exit ordering. */
        return 0;
    }
    return kill(pid, sig);
}

int kou_test_killpg(pid_t group, int sig) {
    return kou_test_kill(-group, sig);
}
'''


@pytest.fixture(scope="session")
def supervisor(tmp_path_factory):
    root = tmp_path_factory.mktemp("supervisor-drain-build")
    source = Path(__file__).resolve().parents[1] / "Support" / "kou-run.c"
    wrapper = root / "signal-instrumentation.c"
    wrapper.write_text(SIGNAL_INSTRUMENTATION)
    object_file = root / "supervisor.o"
    executable = root / "kou-run"
    commands = [
        ["clang", "-O2", "-Wall", "-Wextra", "-Werror",
         "-Dkill=kou_test_kill", "-Dkillpg=kou_test_killpg",
         "-c", str(source), "-o", str(object_file)],
        ["clang", "-O2", "-Wall", "-Wextra", "-Werror",
         str(object_file), str(wrapper), "-o", str(executable)],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
    return executable


@pytest.mark.parametrize("exit_reason", ["cancel", "leader-exit", "owner-death"])
def test_supervisor_waits_for_group_to_stop_before_exiting(process_tree, monkeypatch, exit_reason):
    start, root, owned = process_tree
    marker = root / "held-group-kill"
    monkeypatch.setenv("KOU_TEST_DRAIN_MARKER", str(marker))
    supervisor_pid, descendants, owner = start(
        mode="normal" if exit_reason == "leader-exit" else "stubborn",
        through_owner=exit_reason == "owner-death",
    )

    def supervisor_running():
        # poll() reaps our direct child, so its zombie is not mistaken for life.
        return owned[0].poll() is None if owner is None else alive(supervisor_pid)

    if exit_reason == "cancel":
        os.kill(supervisor_pid, signal.SIGTERM)
    elif exit_reason == "leader-exit":
        (root / "exit").write_text("exit")
    else:
        owner.kill()
        owner.wait(timeout=2)

    assert eventually(marker.exists, timeout=4), "Supervisor never escalated for unresponsive descendants"
    assert int(marker.read_text()) == -descendants["leader"]
    assert any(alive(pid) for pid in descendants.values()), "Instrumentation did not hold descendants alive"

    # A successful SIGKILL syscall does not prove process-group drainage.
    # Keep delivery pending until the supervisor has had time to return.
    exited_before_drain = eventually(lambda: not supervisor_running(), timeout=0.25)
    try:
        assert not exited_before_drain, "Supervisor exited while its child process group was still alive"
    finally:
        # Release only the group this test created; no helper process is needed.
        try:
            os.killpg(descendants["leader"], signal.SIGKILL)
        except ProcessLookupError:
            pass

    assert eventually(lambda: not supervisor_running(), timeout=4), "Supervisor did not finish after group termination"
    assert not any(alive(pid) for pid in descendants.values()), "Supervisor exit preceded descendant termination"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))

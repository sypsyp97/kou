// Keep child process groups tied to the app's lifetime.
#include <errno.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stop_signal;
static volatile sig_atomic_t child_pid;
static void stop(int sig) { stop_signal = sig; }
static void begin_capture(int sig) { (void)sig; if (child_pid > 0) kill(-child_pid, SIGUSR1); }
static void child_changed(int sig) { (void)sig; }
// Deadlines use the clock, not loop counts: a loaded machine can oversleep each interval.
static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    if (argc < 2) return 64;
    pid_t owner = getppid();
    struct sigaction action = {0};
    action.sa_handler = stop;
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGHUP, &action, NULL);
    action.sa_handler = begin_capture;
    sigaction(SIGUSR1, &action, NULL);
    action.sa_handler = child_changed;
    sigaction(SIGCHLD, &action, NULL);
    pid_t child = fork();
    if (child < 0) return 71;
    if (child == 0) {
        if (setsid() < 0) _exit(71);
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        signal(SIGHUP, SIG_DFL);
        signal(SIGUSR1, SIG_DFL);
        signal(SIGCHLD, SIG_DFL);
        execvp(argv[1], argv + 1);
        _exit(127);
    }
    child_pid = child;
    int status = 0;
    double kill_at = 0;
    struct timespec interval = {0, 100000000};
    while (1) {
        pid_t result = waitpid(child, &status, WNOHANG);
        if (result == child) break;
        if (result < 0 && errno != EINTR) return 71;
        if (getppid() != owner || stop_signal) {
            if (kill_at == 0) { kill(-child, SIGTERM); kill_at = now() + 2.5; }
            else if (now() >= kill_at) kill(-child, SIGKILL);
        }
        nanosleep(&interval, NULL);
    }
    // A shell can exit before its descendants. Do not leave those behind.
    kill(-child, SIGTERM);
    for (double end = now() + 1.0; now() < end && kill(-child, 0) == 0;) nanosleep(&interval, NULL);
    kill(-child, SIGKILL);
    // A successful signal does not mean the kernel has drained the group.
    while (1) {
        if (kill(-child, 0) < 0 && errno == ESRCH) break;
        nanosleep(&interval, NULL);
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 70;
}

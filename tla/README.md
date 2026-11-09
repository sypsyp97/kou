# Kou protocol models

These TLA+ models—written by hand from the safety contract—were not generated from Swift or C.
TLC checks bounded protocol states. These results do not prove the Swift implementation or macOS behavior.

Run all configurations with `bash tla/check.sh`. Pass configuration names to run a subset:

```bash
bash tla/check.sh Capture Capture-recovery
```

Set `TLA_JAVA` and `TLA_JAR` if Java and `tla2tools.jar` are elsewhere.
`TLA_OUT` selects the log directory; the default is a new temporary directory.
The runner uses two workers and a 768 MiB heap. It preserves logs, action coverage, and counterexample traces outside this directory.
It fails if a normal configuration fails or an expected counterexample is absent.
Expected counterexamples are test results, not passing application behavior.

| Model | Source correspondence | Checked properties |
| --- | --- | --- |
| `Hotkey` | `KouCore/Session.swift`: `HotkeyGate.update`, `resetChord`; `HotkeyMonitor.receive` | A trigger needs a rising chord edge and at least 0.4 s since the previous trigger. |
| `Capture` | `CaptureSession`; `CaptureController.startRecording`, recorder/transcription completions, `waitForRecovery`, `cancelAll`, `sleep`, `shutdown` | Generations do not repeat. Old callbacks cannot start work or complete another session. A failed managed transcription retries at most once. Sleep and quit block new recording. |
| `Clipboard` | `ClipboardPaster.paste`, `owns`, `restore`, `cancel`; `CaptureController.pasteGeneration` | A successful ownership check cannot adopt an external ownership generation. Event and restore checks validate their inputs. Events address the captured target PID. Separate configurations test strict effect-time guarantees and find residual races. |
| `Shutdown` | `AppDelegate.applicationShouldTerminate`; `ManagedTask.activeCount`; `RuntimeManager.shutdown`; `Support/kou-run.c` | No launch after quit. Reply occurs once, after descendants stop and private audio is swept. |
| `Runtime` | `RuntimeManager.startDaemon`, `checkHealth`, `recover`, `install`, `shutdown`, daemon completion | One resident model at a time. Only the current, non-retiring daemon can set `ready`. A stop that Kou requested is not counted as a failure. |
| `AsrServer` | `Runtime/zh-voice-daemon`: `main`, `cleanup`, `handle`, `ASRWorker.transcribe` | A client only receives the transcript of its own request. A starting instance never removes the socket of a live instance. |
| `Identity` | `build.sh`: staging, designated-requirement comparison, replacement; `Resources/Info.plist` | Replacement preserves the bundle identifier and designated requirement. An unreadable or different requirement cannot replace the installed identity. |

Paths in the table are relative to `Sources/` unless they name `Runtime`, `Support`, `Resources`, or `build.sh`.

The normal configurations explore all reachable states within these bounds:

| Model | Bound | Generated / distinct states |
| --- | --- | --- |
| `Hotkey` | Time age saturates at two 0.2 s ticks. | 23 / 6 |
| `Capture` | Three generations; callbacks may outlive cancellation; one retry per generation. | 5877 / 1078 |
| `Clipboard` | Two sessions, two operations, two focus targets, two external copies, one pending event. | 387561 / 97548 |
| `Shutdown` | Two descendants and two termination requests. | 29 / 16 |
| `Runtime` | Three daemon instances; probes, crashes, and exits interleave with every entry point. | 639 / 142 |
| `AsrServer` | Two instances, three requests, one worker deadline. | 585 / 236 |
| `Identity` | Two bundle identifiers and three requirement outcomes. | 13 / 13 |

Twenty mutation configurations produce their named invariant counterexamples. They remove guards, reuse a generation, sample ownership after writing, or broadcast events.
`Clipboard-marker` is a surviving mutation control. Its successful result does not establish that the marker check is unnecessary in macOS.
Recorder, transcription, and recovery mutations use two generations. Mutation searches stop at the first violation.
Their state counts can vary with worker scheduling. Normal checks complete in under two seconds per configuration on the development machine.

The models assume serialized application actions and unique UUIDs. Callback delivery is unordered and can occur after cancellation.
`Hotkey` abstracts raw modifier flags into a valid chord and assumes a monotonic clock. A tap reset rearms its logical edge.
They abstract short recordings, transcript content, model inference, permissions, and timers into protocol events.
The retry limit applies to one transcription, not the daemon's ongoing health recovery.
No fairness or liveness property is checked; a child, daemon, or callback can remain pending forever.

`Clipboard` separates the platform calls and permits external copies or focus changes between them:

| Model action | `ClipboardPaster.swift` correspondence |
| --- | --- |
| `Snapshot`, `CheckClaim` | Copy original items; compare `board.changeCount` with `before`. |
| `ClearClaim` | Use the return value of `board.clearContents()` as `mine`. Store the temporary snapshot without a marker. |
| `CheckWrite`, `Write` | Recheck `mine`, then call `writeObjects`. A failed write follows the temporary-snapshot restore path. |
| `RecordOwnership` | Require `owns(snapshot)` before accepting the completed write. Keep `mine`; never sample a replacement ownership count after writing. |
| `CheckEvent`, `PostEvent`, `DeliverEvent` | Check the session, ownership, and focused app; call `postToPid(targetPID)`; let the OS deliver the event later. |
| `CheckRestore`, `ClearRestore` | Check `owns(snapshot)`, then call `clearContents`. These are separate actions. |
| `CheckRestoreWrite`, `WriteRestore` | Check the second clear's return count, then write the original items. |

Markers `1` and `2` represent unique operation tokens; `0` is empty, `3` is foreign, and `4` is the original content.
External copies can preserve an operation marker, but increase `changeCount`. A successful write does not imply that ownership remained unchanged during the call.
`owns()` checks the count before and after reading the marker. The model combines those reads only; it separates every subsequent effect.
With a monotonic count and a successful write that installs its marker, removing the marker check alone creates no counterexample.
The model excludes data changes that retain the same ownership count, provider-read failures, count wraparound, and PID reuse.
It abstracts key-down and key-up into one directed event. Native tests must check canceled delayed callbacks and retained process instances.

Four `Clipboard-platform-*` configurations use the unmutated protocol and produce counterexamples:

| Configuration | Residual interleaving |
| --- | --- |
| `claim` | An external copy occurs after the final snapshot check and before `clearContents`. The clear removes that copy. |
| `restore` | An external copy occurs after the restore ownership check and before its clear, or after the next count check and before writing. |
| `focus` | Focus changes after validation and before delivery. PID-directed delivery still addresses the original app, which may now be in the background. |
| `content` | Clipboard ownership changes after event validation and before delivery. The target can then read different clipboard content. |

These are limitations of the modeled call sequence, not successful safety checks. Adding another separate check leaves a final check-to-effect interval.
The fixed `Clipboard` configuration checks only its listed protocol invariants within the bound. It does not prove strict end-to-end clipboard or focus safety.

`Shutdown` assumes supervisor completion means no owned descendant can run or create more audio.
After `SIGKILL`, the C supervisor waits until the process-group probe reports `ESRCH`.
`Shutdown-drain` shows the failure if supervisor completion precedes descendant quiescence.
Process-group disappearance is stronger than quiescence because orphan zombies can keep a group visible.
Termination can wait for macOS to reap those zombies. The model gives no upper bound on that wait.
The model excludes descendants that escape the group with `setsid`, operating-system signal failures, and failed file deletion.

`Identity` treats extracted signing requirements as authenticated inputs and replacement as one commit step.
It does not verify certificates, TCC permission retention, shell parsing, install destinations, filesystem crashes, or concurrent builders.
Source review and native tests must check those conditions separately.

`Runtime` mutations reproduce two defects of the previous code. `Runtime-exit`: a health probe that the exiting daemon answered set `ready` after the daemon was gone. `Runtime-backoff`: a restart requested by Kou counted as a failure, so recovery waited for the backoff. `Runtime-probe` removes the retiring check.
`AsrServer-reuse` keeps serving after a worker deadline, so a late result reaches the next client. `AsrServer-lock` binds before taking the lock.
Passing configurations must also reach every action; `check.sh` fails on an action with zero generated states.
Memory use is not a protocol property. Leaks are measured with `heap` and `vmmap` on a running process.

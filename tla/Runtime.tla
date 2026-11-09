---------------------------- MODULE Runtime ----------------------------
\* RuntimeManager owns at most one ASR daemon. A health probe reaches whichever
\* instance owns the socket path. Kou cancellation and daemon exit are separate steps.
EXTENDS Naturals, FiniteSets
CONSTANTS MaxInstances, Mutation
Ids == 1..MaxInstances
VARIABLES next, procs, sock, daemon, retiring, stopped, installing,
          probe, ready, bad
vars == <<next, procs, sock, daemon, retiring, stopped, installing, probe, ready, bad>>

\* procs[i]: "serving" or "exiting" for live processes. probe: 10 = none, 11 = pending, or answering id (0 = error answer).
Init == /\ next = 0 /\ procs = [i \in {} |-> "serving"] /\ sock = 0 /\ daemon = 0
        /\ retiring = FALSE /\ stopped = FALSE /\ installing = FALSE
        /\ probe = 10 /\ ready = FALSE /\ bad = {}
Live == DOMAIN procs
SetProc(i, s) == [j \in Live \cup {i} |-> IF j = i THEN s ELSE procs[j]]
DropProc(i) == [j \in Live \ {i} |-> procs[j]]

\* Kou stops its daemon: SIGTERM is requested; the process keeps running for a while.
CancelDaemon == /\ retiring' = (daemon # 0)
                /\ procs' = procs

StartDaemon ==
    /\ ~stopped /\ ~installing /\ daemon = 0 /\ next < MaxInstances
    /\ next' = next + 1 /\ daemon' = next + 1 /\ retiring' = FALSE
    /\ procs' = SetProc(next + 1, "serving")
    /\ bad' = bad \cup (IF Live # {} THEN {"double"} ELSE {})
    /\ UNCHANGED <<sock, stopped, installing, probe, ready>>
\* The worker reached READY and bound the socket path.
Bind(i) == /\ i \in Live /\ procs[i] = "serving" /\ sock = 0
           /\ sock' = i
           /\ UNCHANGED <<next, procs, daemon, retiring, stopped, installing, probe, ready, bad>>
\* SIGTERM, worker timeout, or crash: the instance starts its cleanup.
Retire(i) == /\ i \in Live /\ procs[i] = "serving"
             /\ procs' = [procs EXCEPT ![i] = "exiting"]
             /\ UNCHANGED <<next, sock, daemon, retiring, stopped, installing, probe, ready, bad>>
Unbind(i) == /\ i \in Live /\ procs[i] = "exiting" /\ sock = i
             /\ sock' = 0
             /\ UNCHANGED <<next, procs, daemon, retiring, stopped, installing, probe, ready, bad>>
\* kou-run reports completion after the process group is gone.
Exit(i) ==
    /\ i \in Live /\ procs[i] = "exiting" /\ sock # i
    /\ procs' = DropProc(i)
    /\ IF daemon = i
       THEN /\ daemon' = 0 /\ ready' = FALSE /\ retiring' = FALSE
            \* The completion also drops an in-flight probe of that instance.
            /\ probe' = IF Mutation = "keep-probe" THEN probe ELSE 10
            \* Counted as a failure (backoff, error status) only if Kou did not ask for it.
            /\ LET counted == IF Mutation = "stop-counts" THEN ~stopped ELSE ~retiring
               IN bad' = bad \cup (IF counted /\ retiring THEN {"backoff"} ELSE {})
       ELSE UNCHANGED <<daemon, ready, retiring, bad, probe>>
    /\ UNCHANGED <<next, sock, stopped, installing>>

ProbeStart ==
    /\ ~stopped /\ ~installing /\ daemon # 0 /\ probe = 10
    /\ (~retiring \/ Mutation = "probe-retiring")
    /\ probe' = 11
    /\ UNCHANGED <<next, procs, sock, daemon, retiring, stopped, installing, ready, bad>>
\* The health thread of the socket owner answers; it still answers while exiting until Unbind.
\* A self-initiated exit (worker timeout, crash) answers with an error; a SIGTERM exit still answers PONG.
ProbeAnswer == /\ probe = 11
               /\ probe' = IF sock # 0 /\ (procs[sock] = "serving" \/ (retiring /\ sock = daemon)) THEN sock ELSE 0
               /\ UNCHANGED <<next, procs, sock, daemon, retiring, stopped, installing, ready, bad>>
ProbeDone ==
    /\ probe \in Ids \cup {0}
    /\ ready' = (probe # 0)
    /\ bad' = bad \cup (IF probe # 0 /\ (probe # daemon \/ retiring)
                        THEN {"ready"} ELSE {})
    /\ probe' = 10
    /\ UNCHANGED <<next, procs, sock, daemon, retiring, stopped, installing>>

\* Main-actor entry points. Each cancels the probe (healthTask = nil).
Shutdown == /\ stopped' = TRUE /\ ready' = FALSE /\ probe' = 10 /\ CancelDaemon
            /\ UNCHANGED <<next, sock, daemon, installing, bad>>
Load == /\ stopped /\ ~installing /\ stopped' = FALSE
        /\ UNCHANGED <<next, procs, sock, daemon, retiring, installing, probe, ready, bad>>
Install == /\ ~installing /\ installing' = TRUE /\ stopped' = FALSE
           /\ ready' = FALSE /\ probe' = 10 /\ CancelDaemon
           /\ UNCHANGED <<next, sock, daemon, bad>>
InstallDone == /\ installing /\ installing' = FALSE
               /\ UNCHANGED <<next, procs, sock, daemon, retiring, stopped, probe, ready, bad>>
Recover == /\ ~stopped /\ ready' = FALSE /\ probe' = 10 /\ CancelDaemon
           /\ UNCHANGED <<next, sock, daemon, stopped, installing, bad>>

Next == StartDaemon \/ ProbeStart \/ ProbeAnswer \/ ProbeDone
        \/ Shutdown \/ Load \/ Install \/ InstallDone \/ Recover
        \/ \E i \in Ids : Bind(i) \/ Retire(i) \/ Unbind(i) \/ Exit(i)
Spec == Init /\ [][Next]_vars

TypeOK == /\ next \in 0..MaxInstances /\ Live \subseteq Ids
          /\ \A i \in Live : procs[i] \in {"serving", "exiting"}
          /\ sock \in Live \cup {0} /\ daemon \in Live \cup {0}
          /\ probe \in {10, 11} \cup Ids \cup {0}
          /\ bad \subseteq {"double", "ready", "backoff"}
\* One resident model: a second daemon never starts before the previous group is gone.
SingleModel == Cardinality(Live) <= 1 /\ "double" \notin bad
\* ready is only set by the current, non-retiring instance.
ReadyIsCurrent == "ready" \notin bad
\* A stop that Kou requested is not recorded as a service failure.
NoSpuriousBackoff == "backoff" \notin bad
=============================================================================

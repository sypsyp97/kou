---------------------------- MODULE Capture ----------------------------
EXTENDS Naturals, FiniteSets
CONSTANTS MaxSessions, Mutation
Ids == 1..MaxSessions
VARIABLES generation, phase, live, rec, tx, recovery, retries, cancelled, used, bad
vars == <<generation, phase, live, rec, tx, recovery, retries, cancelled, used, bad>>

Init == /\ generation = 0 /\ phase = "idle" /\ live = "running"
        /\ rec = {} /\ tx = {} /\ recovery = {} /\ cancelled = {} /\ used = {}
        /\ retries = [s \in Ids |-> 0] /\ bad = {}
Current(s) == s = generation /\ s \notin cancelled /\ live = "running"
Begin ==
    /\ generation < MaxSessions
    /\ (live = "running" \/ Mutation = "lifecycle")
    /\ LET next == IF Mutation = "generation" /\ generation > 0 THEN generation ELSE generation + 1
       IN /\ generation' = next /\ phase' = "recording"
          /\ rec' = rec \cup {next} /\ used' = used \cup {next}
          /\ cancelled' = cancelled \cup (IF generation = 0 THEN {} ELSE {generation})
          /\ bad' = bad \cup (IF live # "running" THEN {"lifecycle"} ELSE {})
                          \cup (IF next \in used THEN {"generation"} ELSE {})
    /\ UNCHANGED <<live, tx, recovery, retries>>
Stop == /\ phase = "recording" /\ phase' = "finishing"
        /\ UNCHANGED <<generation, live, rec, tx, recovery, retries, cancelled, used, bad>>
Cancel(kind) ==
    /\ live # "quitting"
    /\ live' = kind /\ phase' = "idle"
    /\ cancelled' = cancelled \cup (IF generation = 0 THEN {} ELSE {generation})
    /\ UNCHANGED <<generation, rec, tx, recovery, retries, used, bad>>
Wake == /\ live = "sleeping" /\ live' = "running"
        /\ UNCHANGED <<generation, phase, rec, tx, recovery, retries, cancelled, used, bad>>
RecorderDone(s) ==
    /\ s \in rec
    /\ LET valid == Current(s) /\ phase \in {"recording", "finishing"}
           accept == valid \/ Mutation = "recorder"
       IN /\ rec' = rec \ {s}
          /\ phase' = IF accept THEN "transcribing" ELSE phase
          /\ tx' = IF accept THEN tx \cup {s} ELSE tx
          /\ bad' = bad \cup (IF accept /\ ~valid THEN {"recorder"} ELSE {})
    /\ UNCHANGED <<generation, live, recovery, retries, cancelled, used>>
TranscriptionDone(s, failed) ==
    /\ s \in tx
    /\ LET valid == Current(s) /\ phase = "transcribing"
           accept == valid \/ Mutation = "transcript"
           retry == accept /\ failed /\ (retries[s] = 0 \/ Mutation = "retry")
       IN /\ tx' = tx \ {s}
          /\ phase' = IF accept THEN (IF retry THEN "recovering" ELSE "idle") ELSE phase
          /\ recovery' = IF retry THEN recovery \cup {s} ELSE recovery
          /\ bad' = bad \cup (IF accept /\ ~valid THEN {"transcript"} ELSE {})
    /\ UNCHANGED <<generation, live, rec, retries, cancelled, used>>
Recovered(s) ==
    /\ s \in recovery
    /\ retries[s] < 2
    /\ LET valid == Current(s) /\ phase = "recovering"
           accept == valid \/ Mutation = "recovery"
       IN /\ recovery' = recovery \ {s}
          /\ retries' = IF accept THEN [retries EXCEPT ![s] = @ + 1] ELSE retries
          /\ phase' = IF accept THEN "transcribing" ELSE phase
          /\ tx' = IF accept THEN tx \cup {s} ELSE tx
          /\ bad' = bad \cup (IF accept /\ ~valid THEN {"recovery"} ELSE {})
    /\ UNCHANGED <<generation, live, rec, cancelled, used>>
Next == Begin \/ Stop \/ Cancel("running") \/ Cancel("sleeping") \/ Cancel("quitting") \/ Wake
        \/ (\E s \in Ids : RecorderDone(s) \/ Recovered(s)
             \/ TranscriptionDone(s, TRUE) \/ TranscriptionDone(s, FALSE))
Spec == Init /\ [][Next]_vars
TypeOK == /\ generation \in 0..MaxSessions
          /\ phase \in {"idle", "recording", "finishing", "transcribing", "recovering"}
          /\ live \in {"running", "sleeping", "quitting"}
          /\ rec \subseteq Ids /\ tx \subseteq Ids /\ recovery \subseteq Ids
          /\ cancelled \subseteq Ids /\ used \subseteq Ids /\ retries \in [Ids -> 0..2]
          /\ bad \subseteq {"lifecycle", "recorder", "transcript", "recovery", "generation"}
NoStaleEffects == bad \subseteq {"generation"}
UniqueGenerations == "generation" \notin bad
RetryAtMostOnce == \A s \in Ids : retries[s] <= 1
=============================================================================

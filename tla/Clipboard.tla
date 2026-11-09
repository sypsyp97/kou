---------------------------- MODULE Clipboard ----------------------------
EXTENDS Naturals
CONSTANT Mutation
VARIABLE s
vars == <<s>>

Init == s = [generation |-> 0, cancelled |-> FALSE, op |-> 0, token |-> 0,
              focus |-> 1, target |-> 1, phase |-> "idle", event |-> "idle",
              count |-> 0, copies |-> 0, owner |-> 0, mark |-> 4,
              before |-> 0, claim |-> 0, saved |-> 0, requiredMarker |-> 0, restoreClaim |-> 0,
              previous |-> 4, bad |-> {}]

Current == s.token = s.generation /\ ~s.cancelled
Focused == s.focus = s.target
BoardGuard == s.count = s.saved /\ (s.requiredMarker = 0 \/ s.mark = s.requiredMarker)
Owned == BoardGuard /\ s.owner = s.op
AddBad(flag, condition) == s.bad \cup (IF condition THEN {flag} ELSE {})

NewSession == /\ s.generation < 2
              /\ s.phase \in {"idle", "owned", "restored", "aborted"}
              /\ s.event # "checked"
              /\ s' = [s EXCEPT !.generation = @ + 1, !.cancelled = FALSE]
Cancel == /\ s.phase \in {"idle", "owned", "restored", "aborted"}
          /\ s.event # "checked"
          /\ s' = [s EXCEPT !.cancelled = TRUE]
Focus(pid) == /\ pid \in {1, 2} /\ s' = [s EXCEPT !.focus = pid]
ExternalCopy(marker) ==
    /\ s.copies < 2 /\ marker \in {1, 2, 3}
    /\ s' = [s EXCEPT !.copies = @ + 1, !.count = @ + 1,
                       !.owner = 0, !.mark = marker]

Snapshot == /\ s.op < 2 /\ s.generation > 0 /\ ~s.cancelled /\ Focused
            /\ s.phase \in {"idle", "restored", "aborted"}
            /\ s.event \in {"idle", "delivered"}
            /\ s' = [s EXCEPT !.op = @ + 1, !.token = s.generation,
                               !.before = s.count, !.previous = s.mark,
                               !.phase = "snapshotted", !.event = "idle"]
CheckClaim == /\ s.phase = "snapshotted" /\ Current /\ Focused
              /\ s.count = s.before
              /\ s' = [s EXCEPT !.phase = "claimChecked"]
ClearClaim == /\ s.phase = "claimChecked"
              /\ s' = [s EXCEPT !.count = @ + 1, !.claim = s.count + 1,
                                 !.saved = s.count + 1, !.requiredMarker = 0,
                                 !.owner = s.op, !.mark = 0, !.phase = "claimed",
                                 !.bad = AddBad("claim", s.count # s.before)]
CheckWrite == /\ s.phase = "claimed" /\ s.count = s.claim
              /\ s' = [s EXCEPT !.phase = "writeChecked"]
LostClaim == /\ s.phase = "claimed" /\ s.count # s.claim
             /\ s' = [s EXCEPT !.phase = "aborted"]

\* Another process can take ownership while a write executes. A successful
\* write and the expected marker do not establish the ownership generation.
Write(good) == /\ s.phase = "writeChecked"
               /\ s' = [s EXCEPT !.phase = IF good THEN "written" ELSE "failedWrite",
                                  !.mark = IF good THEN s.op ELSE s.mark]
RecordOwnership ==
    /\ s.phase = "written"
    /\ LET recorded == IF Mutation = "late-count" THEN s.count ELSE s.claim
           accept == s.count = recorded /\ (s.mark = s.op \/ Mutation = "marker")
       IN s' = [s EXCEPT !.phase = IF accept THEN "owned" ELSE "aborted",
                         !.saved = IF accept THEN recorded ELSE 0,
                         !.requiredMarker = s.op,
                         !.bad = AddBad("adopt", accept /\
                             (s.count # s.claim \/ s.owner # s.op \/ s.mark # s.op))]

CheckEvent ==
    /\ s.phase = "owned" /\ s.event = "idle"
    /\ (Current \/ Mutation = "paste-session")
    /\ (Focused \/ Mutation = "paste-focus")
    /\ (BoardGuard \/ Mutation = "paste-board")
    /\ s' = [s EXCEPT !.event = "checked",
                       !.bad = AddBad("guard", ~(Current /\ Focused /\ Owned))]
PostEvent == /\ s.event = "checked"
             /\ s' = [s EXCEPT !.event = "pending"]
DeliverEvent ==
    /\ s.event = "pending"
    /\ LET destination == IF Mutation = "broadcast" THEN s.focus ELSE s.target
       IN s' = [s EXCEPT !.event = "delivered",
                         !.bad = s.bad
                             \cup (IF destination # s.target THEN {"destination"} ELSE {})
                             \cup (IF ~Focused THEN {"focus"} ELSE {})
                             \cup (IF ~Owned THEN {"content"} ELSE {})]

CheckRestore == /\ s.phase \in {"owned", "failedWrite"} /\ s.event # "checked"
                /\ (BoardGuard \/ Mutation = "restore-board")
                /\ s' = [s EXCEPT !.phase = "restoreChecked",
                                   !.bad = AddBad("guard", ~Owned)]
ClearRestore == /\ s.phase = "restoreChecked"
                /\ s' = [s EXCEPT !.count = @ + 1, !.restoreClaim = s.count + 1,
                                   !.owner = s.op, !.mark = 0, !.phase = "restoreCleared",
                                   !.bad = AddBad("restore", ~Owned)]
CheckRestoreWrite == /\ s.phase = "restoreCleared" /\ s.count = s.restoreClaim
                     /\ s' = [s EXCEPT !.phase = "restoreWriteChecked"]
LostRestoreClaim == /\ s.phase = "restoreCleared" /\ s.count # s.restoreClaim
                    /\ s' = [s EXCEPT !.phase = "restored"]
WriteRestore == /\ s.phase = "restoreWriteChecked"
                /\ s' = [s EXCEPT !.mark = s.previous, !.phase = "restored",
                                   !.bad = AddBad("restore", s.count # s.restoreClaim \/ s.owner # s.op)]

Next == NewSession \/ Cancel \/ Snapshot \/ CheckClaim \/ ClearClaim \/ CheckWrite \/ LostClaim
        \/ Write(TRUE) \/ Write(FALSE) \/ RecordOwnership
        \/ CheckEvent \/ PostEvent \/ DeliverEvent
        \/ CheckRestore \/ ClearRestore \/ CheckRestoreWrite \/ LostRestoreClaim \/ WriteRestore
        \/ (\E pid \in {1, 2} : Focus(pid))
        \/ (\E marker \in {1, 2, 3} : ExternalCopy(marker))
Spec == Init /\ [][Next]_vars
TypeOK == /\ s.generation \in 0..2 /\ s.cancelled \in BOOLEAN
          /\ s.op \in 0..2 /\ s.token \in 0..2 /\ s.focus \in {1, 2} /\ s.target = 1
          /\ s.phase \in {"idle", "snapshotted", "claimChecked", "claimed", "writeChecked", "written",
                           "owned", "failedWrite", "aborted", "restoreChecked", "restoreCleared",
                           "restoreWriteChecked", "restored"}
          /\ s.event \in {"idle", "checked", "pending", "delivered"}
          /\ s.count \in 0..6 /\ s.copies \in 0..2 /\ s.owner \in 0..2
          /\ s.mark \in 0..4 /\ s.previous \in 0..4
          /\ s.before \in 0..6 /\ s.claim \in 0..6 /\ s.saved \in 0..6 /\ s.restoreClaim \in 0..6
          /\ s.requiredMarker \in 0..2
          /\ s.bad \subseteq {"adopt", "guard", "destination", "claim", "restore", "focus", "content"}
NoAdoptedOwnership == "adopt" \notin s.bad
GuardedOperations == "guard" \notin s.bad
DirectedDelivery == "destination" \notin s.bad
StrictClaim == "claim" \notin s.bad
StrictRestore == "restore" \notin s.bad
StrictFocusedDelivery == "focus" \notin s.bad
StrictClipboardAtDelivery == "content" \notin s.bad
=============================================================================

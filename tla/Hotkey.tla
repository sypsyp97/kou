---------------------------- MODULE Hotkey ----------------------------
EXTENDS Naturals
CONSTANT Mutation
VARIABLES down, age, bad
vars == <<down, age, bad>>

Init == /\ down = FALSE /\ age = 2 /\ bad = FALSE
Tick == /\ age < 2 /\ age' = age + 1 /\ UNCHANGED <<down, bad>>
Event(chord) ==
    LET accepted == chord /\ (Mutation = "edge" \/ ~down)
                          /\ (Mutation = "debounce" \/ age = 2)
    IN /\ down' = chord
       /\ age' = IF accepted THEN 0 ELSE age
       /\ bad' = (bad \/ (accepted /\ (down \/ age < 2)))
Reset == /\ down' = FALSE /\ UNCHANGED <<age, bad>>
Next == Tick \/ Event(TRUE) \/ Event(FALSE) \/ Reset
Spec == Init /\ [][Next]_vars
TypeOK == /\ down \in BOOLEAN /\ age \in 0..2 /\ bad \in BOOLEAN
DebouncedEdge == ~bad
=============================================================================

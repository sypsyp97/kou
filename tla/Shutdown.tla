---------------------------- MODULE Shutdown ----------------------------
EXTENDS Naturals
CONSTANT Mutation
VARIABLES terminating, supervisor, descendants, audio, swept, replied, requests, bad
vars == <<terminating, supervisor, descendants, audio, swept, replied, requests, bad>>

Init == /\ terminating = FALSE /\ supervisor = TRUE /\ descendants = 2
        /\ audio = TRUE /\ swept = FALSE /\ replied = 0 /\ requests = 0 /\ bad = {}
Quit == /\ requests < 2 /\ requests' = requests + 1 /\ terminating' = TRUE
        /\ UNCHANGED <<supervisor, descendants, audio, swept, replied, bad>>
Launch == /\ ~supervisor
          /\ (~terminating \/ Mutation = "launch")
          /\ supervisor' = TRUE /\ descendants' = 2 /\ audio' = TRUE /\ swept' = FALSE
          /\ bad' = bad \cup (IF terminating THEN {"launch"} ELSE {})
          /\ UNCHANGED <<terminating, replied, requests>>
ChildExit == /\ descendants > 0 /\ descendants' = descendants - 1
             /\ UNCHANGED <<terminating, supervisor, audio, swept, replied, requests, bad>>
SupervisorExit == /\ supervisor
                  /\ (descendants = 0 \/ Mutation = "drain")
                  /\ supervisor' = FALSE
                  /\ UNCHANGED <<terminating, descendants, audio, swept, replied, requests, bad>>
Sweep == /\ terminating /\ ~supervisor
         /\ audio' = FALSE /\ swept' = TRUE
         /\ UNCHANGED <<terminating, supervisor, descendants, replied, requests, bad>>
Reply == /\ terminating /\ ~supervisor
         /\ (swept \/ Mutation = "sweep")
         /\ (replied = 0 \/ (Mutation = "once" /\ replied < requests))
         /\ replied' = replied + 1
         /\ bad' = bad \cup (IF descendants # 0 \/ ~swept \/ audio THEN {"reply"} ELSE {})
         /\ UNCHANGED <<terminating, supervisor, descendants, audio, swept, requests>>
Next == Quit \/ Launch \/ ChildExit \/ SupervisorExit \/ Sweep \/ Reply
Spec == Init /\ [][Next]_vars
TypeOK == /\ terminating \in BOOLEAN /\ supervisor \in BOOLEAN
          /\ descendants \in 0..2 /\ audio \in BOOLEAN /\ swept \in BOOLEAN
          /\ replied \in 0..2 /\ requests \in 0..2 /\ bad \subseteq {"launch", "reply"}
NoWorkAfterQuit == "launch" \notin bad
ReplyAfterCleanup == "reply" \notin bad
ReplyOnce == replied <= 1
=============================================================================

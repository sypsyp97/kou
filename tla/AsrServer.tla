---------------------------- MODULE AsrServer ----------------------------
\* zh-voice-daemon: one request per connection, serialized on the data listener;
\* a worker reply that misses its deadline ends the instance. Two instances share
\* one socket path and one lock file.
EXTENDS Integers
CONSTANTS MaxRequests, Mutation
Reqs == 1..MaxRequests
VARIABLES sent, serving, worker, replies, alive, lock, sock, bad
vars == <<sent, serving, worker, replies, alive, lock, sock, bad>>

\* worker: 0 idle, r computing r, -r late result of r still in the pipe.
\* alive[i]: "off", "starting", "up", "cleaning". lock/sock: owning instance or 0.
Init == /\ sent = 0 /\ serving = 0 /\ worker = 0 /\ replies = {}
        /\ alive = [i \in 1..2 |-> "off"] /\ lock = 0 /\ sock = 0 /\ bad = {}
Up == {i \in 1..2 : alive[i] = "up"}

Launch(i) == /\ alive[i] = "off" /\ alive' = [alive EXCEPT ![i] = "starting"]
             /\ UNCHANGED <<sent, serving, worker, replies, lock, sock, bad>>
\* Take the lock first; the loser exits without touching the socket path.
Acquire(i) ==
    /\ alive[i] = "starting"
    /\ IF lock = 0 \/ Mutation = "unlink-first"
       THEN /\ lock' = IF lock = 0 THEN i ELSE lock
            \* cleanup() removes a stale socket file, then bind() creates ours.
            /\ bad' = bad \cup (IF sock # 0 /\ sock # i /\ alive[sock] = "up" THEN {"stolen"} ELSE {})
            /\ sock' = i /\ alive' = [alive EXCEPT ![i] = "up"]
       ELSE /\ alive' = [alive EXCEPT ![i] = "off"] /\ UNCHANGED <<lock, sock, bad>>
    /\ UNCHANGED <<sent, serving, worker, replies>>
\* The instance exits: cleanup unlinks the path, then process exit releases the lock.
Cleanup(i) == /\ alive[i] = "up" /\ alive' = [alive EXCEPT ![i] = "cleaning"]
              /\ sock' = IF sock = i THEN 0 ELSE sock
              /\ serving' = 0 /\ worker' = 0
              /\ UNCHANGED <<sent, replies, lock, bad>>
Gone(i) == /\ alive[i] = "cleaning" /\ alive' = [alive EXCEPT ![i] = "off"]
           /\ lock' = IF lock = i THEN 0 ELSE lock
           /\ UNCHANGED <<sent, serving, worker, replies, sock, bad>>

\* A client connects to the current socket owner and the accept loop takes it.
Accept == /\ sock # 0 /\ alive[sock] = "up" /\ serving = 0 /\ worker \in {0} \cup {-r : r \in Reqs}
          /\ sent < MaxRequests
          /\ sent' = sent + 1 /\ serving' = sent + 1
          /\ UNCHANGED <<worker, replies, alive, lock, sock, bad>>
Send == /\ serving # 0 /\ worker \in {0} \cup {-r : r \in Reqs}
        /\ worker' = IF worker = 0 THEN serving ELSE worker
        /\ (worker >= 0 \/ Mutation = "reuse")
        /\ UNCHANGED <<sent, serving, replies, alive, lock, sock, bad>>
\* pipe.poll(budget) expires: reply ERROR. The fix ends the instance; the mutation keeps serving.
Timeout == /\ serving # 0 /\ worker = serving
           /\ worker' = (IF Mutation = "reuse" THEN -serving ELSE 0) /\ serving' = 0
           /\ IF Mutation = "reuse" THEN UNCHANGED alive
              ELSE alive' = [alive EXCEPT ![sock] = "cleaning"]
           /\ sock' = IF Mutation = "reuse" THEN sock ELSE 0
           /\ UNCHANGED <<sent, replies, lock, bad>>
\* recv() returns whatever result is next in the pipe.
Reply == /\ serving # 0 /\ worker # 0
         /\ LET r == IF worker < 0 THEN -worker ELSE worker
            IN /\ replies' = replies \cup {<<serving, r>>}
               /\ bad' = bad \cup (IF r # serving THEN {"crossed"} ELSE {})
         /\ worker' = 0 /\ serving' = 0
         /\ UNCHANGED <<sent, alive, lock, sock>>

Next == Accept \/ Send \/ Timeout \/ Reply
        \/ \E i \in 1..2 : Launch(i) \/ Acquire(i) \/ Cleanup(i) \/ Gone(i)
Spec == Init /\ [][Next]_vars

\* A client only receives the transcript of its own audio.
NoCrossedReply == "crossed" \notin bad
\* A starting instance never removes the socket of a live instance.
NoStolenSocket == "stolen" \notin bad
=============================================================================

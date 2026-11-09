---------------------------- MODULE Identity ----------------------------
EXTENDS Naturals
CONSTANT Mutation
VARIABLES candidateBundle, candidateRequirement, installedBundle, installedRequirement, state
vars == <<candidateBundle, candidateRequirement, installedBundle, installedRequirement, state>>
Init == /\ candidateBundle \in {"Kou", "Other"}
        /\ candidateRequirement \in {"stable", "changed", "unreadable"}
        /\ installedBundle = "Kou" /\ installedRequirement = "stable" /\ state = "staged"
Replace == /\ state = "staged"
           /\ (candidateBundle = installedBundle \/ Mutation = "bundle")
           /\ (candidateRequirement = installedRequirement \/ Mutation = "requirement")
           /\ installedBundle' = candidateBundle
           /\ installedRequirement' = candidateRequirement /\ state' = "installed"
           /\ UNCHANGED <<candidateBundle, candidateRequirement>>
Refuse == /\ state = "staged" /\ state' = "refused"
          /\ UNCHANGED <<candidateBundle, candidateRequirement, installedBundle, installedRequirement>>
Next == Replace \/ Refuse
Spec == Init /\ [][Next]_vars
TypeOK == /\ candidateBundle \in {"Kou", "Other"}
          /\ candidateRequirement \in {"stable", "changed", "unreadable"}
          /\ installedBundle \in {"Kou", "Other"}
          /\ installedRequirement \in {"stable", "changed", "unreadable"}
          /\ state \in {"staged", "installed", "refused"}
StableIdentity == installedBundle = "Kou" /\ installedRequirement = "stable"
=============================================================================

# Drift Outcomes

Three questions decide whether a knowledge layer earns trust: how do we prevent drift, how do we detect it, and how do we make sure the answer is right. Each one got measured, not asserted.

## 1. How do we prevent drift? Measure first: most drift is layout and process

- A full drain of the queue traced **≥95%** of drift noise to document layout and process, not facts
- The docs weren't wrong. They were shaped wrong for machines to read, and maintained by habit instead of by gate
- What that produced: a reformat standard. One asserter per fact, machine-owned sections for anything derivable, and write-time gates that keep it that way
- The measurement came first. The standard came out of it, not the other way around

## 2. How do we detect drift? Deterministic first, LLM judgment second

- Two paired rubrics: a deterministic rubric (what a $0 rule can verify, per doc class) and a judgment rubric (the known noise shapes, so calibration leaves only real questions)
- The rubric pre-filter proposes verdicts for $0 before the LLM sees anything
- The first full drain: **36 cards, ~540 spans**. After calibration, exactly **1** card needed a human ruling
- What's left is a quiet residue of genuinely important questions

## 3. How do we make sure it's right? Evals that measure trust

- Every human ruling becomes a labelled test fixture, so the golden set grows from normal review instead of a testing project nobody funds
- Check types graduate to trusted at measured precision, and demote themselves on decay
- Activation pass: **66** ruled fixtures reviewed per-entry into the golden set, and **8** stale labels caught and re-labeled on the way in
- The same pass measured four of its own check families at **50–82%** false-positive rates on the worst-case set, and refused to graduate them
- The claim this earns: "verified against your own rulings, at N samples, X% precision, as of this date"

## What I'd tell anyone building one of these

Trust that is measured beats trust that is asserted. A machine caught itself being confidently wrong because a human wrote down what true looks like.

#!/usr/bin/env python3
"""
Logic simulation for the GenX dual-stream RAG pipeline.

WHY THIS EXISTS: the build environment for this submission has no Docker,
so n8n + Postgres cannot be started there. This script runs the SAME logic
as the n8n Code nodes (sampler, gate, dual-stream parser, chunking, greeting
router, circuit breaker) against data/mock_transcripts.json, so the data
wrangling and gating behaviour are verified end to end. Two stand-ins are
used, and both are labelled as such:

  * classify() is a deterministic RULE-BASED stand-in for the classifier LLM
    (Workflow A, "Classification LLM" node). It returns the same strict-JSON
    shape the prompt demands. It proves the gate / parse / archive logic,
    not the LLM's judgement.
  * retrieval_gate() is a keyword-overlap stand-in for cosine similarity
    over real embeddings (Workflow B, gated SQL). It proves the threshold /
    zero-match circuit-breaker logic, not embedding quality.

Run:  python3 verification/simulate_pipeline.py
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "mock_transcripts.json"

HEAD_TURNS, TAIL_TURNS = 40, 20           # Bounded Sampler (Workflow A)
GATE_TYPE, GATE_CONFIDENCE = "Client Coaching", 0.80   # Ingestion Gate (Switch)
MIN_CHUNK, MAX_CHUNK = 400, 700           # Dual-Stream Parser chunk window
MIN_FRAMEWORK_TURN = 150                  # Dual-Stream Parser framework filter
FALLBACK = "No documented coaching frameworks match this topic."


# --- Bounded Sampler: identical logic to the n8n Code node -------------------
def sample_turns(turns):
    seen, sample = set(), []
    def add(turn, idx):
        if idx not in seen:
            seen.add(idx)
            sample.append(turn)
    for i, t in enumerate(turns[:HEAD_TURNS]):
        add(t, i)
    tail_start = max(0, len(turns) - TAIL_TURNS)
    for i, t in enumerate(turns[tail_start:], start=tail_start):
        add(t, i)
    return sample


# --- Classifier STAND-IN (for the LLM node) ----------------------------------
# Keyword evidence per class, scored over the bounded sample only.
SIGNALS = {
    "Client Coaching": [
        "burnout", "delegation", "delegate", "feedback", "executive presence",
        "accountability", "framework", "micromanagement", "boundary",
        "top performers", "leadership team", "nervous system",
    ],
    "Internal Operations": [
        "webhook", "sprint", "crm", "api", "docker", "pgvector", "hnsw",
        "git branch", "pr ", "bug", "migration", "staging", "code node",
    ],
    "Sales Discovery": [
        "pricing", "budget", "contract", "retainer", "pilot", "payment terms",
        "per quarter", "scope of work", "finance", "curriculum",
    ],
}


def classify(sample):
    text = " ".join(t["text"].lower() for t in sample)
    scores = {k: sum(text.count(sig) for sig in sigs) for k, sigs in SIGNALS.items()}
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    if total == 0 or scores[best] == 0:
        return {"meeting_type": "Other", "confidence_score": 0.0,
                "summary": "No recognisable coaching, operations or sales signals."}
    # Confidence stand-in: winner's share of all signal hits, floored so a
    # clean single-topic call lands in the same range an LLM would report.
    confidence = round(0.55 + 0.45 * (scores[best] / total), 2)
    summaries = {
        "Client Coaching": "Coach advising an external client on leadership behaviour.",
        "Internal Operations": "Internal technical sync: sprint, integrations and infra.",
        "Sales Discovery": "Prospect call centred on pricing, budget and contract terms.",
    }
    return {"meeting_type": best, "confidence_score": confidence,
            "summary": summaries[best]}


# --- Dual-Stream Parser: identical logic to the n8n Code node -----------------
def chunk_turns(turns):
    chunks, buffer = [], ""
    for turn in turns:
        text = turn["text"].strip()
        if not text:
            continue
        candidate = f"{buffer} {text}" if buffer else text
        if len(candidate) > MAX_CHUNK and len(buffer) >= MIN_CHUNK:
            chunks.append(buffer)
            buffer = text
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)
    return chunks


def dual_stream(session):
    coach = next((p["name"] for p in session["participants"] if p["is_team_member"]), None)
    coach_turns = [t for t in session["transcript"] if t["speaker"] == coach]
    framework = " ".join(
        t["text"].strip() for t in coach_turns if len(t["text"].strip()) >= MIN_FRAMEWORK_TURN
    )
    rows = []
    if framework:
        rows.append(("framework", framework))
    rows += [("voice_sample", c) for c in chunk_turns(coach_turns)]
    return coach, rows


# --- Workflow B stand-ins ------------------------------------------------------
GREETING = re.compile(
    r"^\s*(hello|hi+|hey+|howdy|greetings)(\s+there)?[\s!.]*$"
    r"|^\s*good\s+(morning|afternoon|evening)[\s!.]*$",
    re.IGNORECASE,
)
STOP = {"how", "do", "i", "my", "the", "a", "an", "should", "to", "of", "is", "it"}


def retrieval_gate(query, vault):
    """Keyword-overlap stand-in for the gated cosine SQL.
    A row 'passes' if it shares >= 2 content words with the query; the
    circuit breaker fires when nothing passes, exactly as with 0 SQL rows."""
    words = {w for w in re.findall(r"[a-z]+", query.lower()) if w not in STOP}
    hits = [r for r in vault if len(words & set(re.findall(r"[a-z]+", r[2].lower()))) >= 2]
    return hits


def main():
    sessions = json.loads(DATA.read_text())
    print("=" * 72)
    print("GENX PIPELINE - LOGIC SIMULATION (stand-ins labelled in the docstring)")
    print("=" * 72)

    vault, rejected = [], []
    for s in sessions:
        sample = sample_turns(s["transcript"])
        result = classify(sample)
        passed = result["meeting_type"] == GATE_TYPE and result["confidence_score"] >= GATE_CONFIDENCE
        print(f"\n{s['id']}  '{s['title']}'  turns={len(s['transcript'])} sampled={len(sample)}")
        print(f"  classifier (stand-in): {result['meeting_type']}  "
              f"confidence={result['confidence_score']:.2f}  - {result['summary']}")
        if passed:
            coach, rows = dual_stream(s)
            vault += [(s["id"], stream, content) for stream, content in rows]
            fw = sum(1 for st, _ in rows if st == "framework")
            vs = [(st, c) for st, c in rows if st == "voice_sample"]
            print(f"  GATE: PASS (>= {GATE_CONFIDENCE}) -> knowledge_vault")
            print(f"  coach (team member): {coach}")
            print(f"  framework chunks: {fw}   voice_sample chunks: {len(vs)} "
                  f"(chars: {[len(c) for _, c in vs]})")
        else:
            rejected.append((s["id"], result))
            print(f"  GATE: REJECT -> rejected_sessions (flat archive, NO embeddings)")
            print(f"  archived: meeting_type={result['meeting_type']}  "
                  f"confidence={result['confidence_score']:.2f}")

    print("\n" + "-" * 72)
    print(f"knowledge_vault rows (simulated): {len(vault)}  "
          f"({sum(1 for r in vault if r[1] == 'framework')} framework, "
          f"{sum(1 for r in vault if r[1] == 'voice_sample')} voice_sample)")
    print(f"rejected_sessions rows (simulated): {len(rejected)}  "
          f"-> {[r[0] for r in rejected]}")

    print("\n" + "-" * 72)
    print("WORKFLOW B - GATED RETRIEVAL")
    q1 = "Hello!"
    print(f"\n  query: {q1!r}")
    print(f"  greeting regex match: {bool(GREETING.match(q1))} "
          f"-> static greeting returned, NO embedding, NO database call")

    q2 = "How do I fix my database index?"
    hits = retrieval_gate(q2, vault)
    print(f"\n  query: {q2!r}  (off-domain)")
    print(f"  rows passing the gate (stand-in): {len(hits)}")
    if not hits:
        print(f"  CIRCUIT BREAKER: LLM not called. Fallback returned:")
        print(f"  \"{FALLBACK}\"")

    q3 = "How should I approach an underperforming VP - what is operational drift?"
    hits = retrieval_gate(q3, vault)
    print(f"\n  query: {q3!r}  (on-domain, bonus check)")
    print(f"  rows passing the gate (stand-in): {len(hits)} "
          f"from sessions {sorted({h[0] for h in hits})} "
          f"-> context built, persona LLM would be called")

    ok = (
        {r[0] for r in vault} == {"SES-101", "SES-102"}
        and [r[0] for r in rejected] == ["SES-103", "SES-104"]
        and bool(GREETING.match(q1))
        and not retrieval_gate(q2, vault)
    )
    print("\n" + "=" * 72)
    print("SIMULATION RESULT:", "PASS" if ok else "FAIL",
          "- SES-101/102 ingested, SES-103/104 rejected, greeting intercepted,",
          "off-domain query hit the circuit-breaker fallback.")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

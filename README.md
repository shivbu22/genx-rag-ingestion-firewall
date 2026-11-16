# GenX Leadership — Bounded Dual-Stream RAG & Ingestion Firewall

An end-to-end, fully local RAG pipeline in **n8n + PostgreSQL/pgvector** over
executive-coaching transcripts. Two ideas carry the design:

1. **An upstream firewall** — no transcript reaches the vector store until a
   classifier has labelled it *Client Coaching* with confidence ≥ 0.80.
   Everything else is archived in a flat table, with **no embeddings created**.
2. **A downstream gate** — retrieval is a hard SQL cosine-threshold filter,
   not a suggestion to the LLM. If nothing passes, a circuit breaker returns
   a fixed fallback sentence and the LLM is never called.

Approved coaching calls are stored as two streams in one table:
`framework` (the dense conceptual takeaway) and `voice_sample` (the coach's
verbatim dialogue, chunked at 400–700 characters), so answers can be grounded
in the framework *and* phrased in Dr. Sara Hegy's voice.

## Repository layout

```
├── docker-compose.yml            # n8n (latest) + pgvector/pgvector:pg16
├── init.sql                      # vector extension, knowledge_vault,
│                                 # rejected_sessions, HNSW cosine index
├── workflows/
│   ├── ingestion_firewall.json   # Workflow A — classify, gate, dual-stream, embed
│   └── gated_retrieval.json      # Workflow B — greeting router, gated SQL, persona LLM
├── data/
│   └── mock_transcripts.json     # 4 sessions: SES-101 … SES-104
└── verification/
    ├── simulate_pipeline.py      # logic simulation (see Verification)
    └── verification_log.txt      # its actual output
```

## Quickstart

Prerequisites: Docker with the Compose plugin, and a local LLM/embedding
endpoint. The defaults target **Ollama** on the host:

```bash
ollama pull llama3.2   # classifier + persona generation
ollama pull bge-m3     # 1024-dim embeddings (matches vector(1024) in init.sql)
```

```bash
git clone <this-repo> genx-rag && cd genx-rag
docker compose up -d
```

- Postgres starts first; `init.sql` is mounted into
  `/docker-entrypoint-initdb.d/`, so the `vector` extension, both tables and
  the HNSW index are created on first boot of an empty volume.
  (To re-run it later: `docker compose down -v && docker compose up -d`.)
- Open n8n at <http://localhost:5678> and create the owner account.
- Create one **Postgres credential**: host `db`, port `5432`, database
  `genx_rag`, user `n8n`, password `n8n_password` (or your `.env` overrides).
  Select it in the three Postgres nodes.
- Import both workflows from `workflows/` (the folder is also mounted into
  the container at `/workflows`, and the dataset at `/data`).
- **Run Workflow A** with the Manual Trigger. It reads
  `/data/mock_transcripts.json`, samples, classifies, gates, parses and
  embeds. Expected result: SES-101 and SES-102 in `knowledge_vault`
  (1 framework + 2 voice_sample rows each in the mock data), SES-103 and
  SES-104 in `rejected_sessions`.
- **Activate Workflow B**, then POST a question:

```bash
curl -s -X POST http://localhost:5678/webhook/coaching-chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "How should I approach an underperforming VP?"}'
```

Configuration is via environment variables in `docker-compose.yml`:
`LLM_CHAT_URL`, `LLM_MODEL`, `EMBEDDING_URL`, `EMBEDDING_MODEL` — any
OpenAI-compatible endpoint works (Ollama, Groq, OpenAI). If you switch to a
1536-dim embedding model, change `vector(1024)` in `init.sql` to match.

## How it works

### Workflow A — Ingestion & Semantic Firewall

`Manual Trigger → Read file → Extract JSON → Split Sessions → Bounded Sampler
→ Classification LLM → Parse Classification → Switch → …`

- **Bounded Sampler (Code):** first 40 turns + last 20 turns per session,
  de-duplicated by original index, so short transcripts pass through whole
  and classification never pays for unbounded context.
- **Classification LLM (HTTP):** temperature 0, a prompt that demands strict
  JSON only: `{"meeting_type": "Client Coaching | Internal Operations |
  Sales Discovery | Other", "confidence_score": 0.0–1.0, "summary": "…"}`.
- **Parse Classification (Code):** strips code fences, extracts the JSON
  object, validates the type against the allowed vocabulary and clamps the
  confidence to [0, 1]. Any failure degrades to `Other / 0.0` — a malformed
  reply can only ever route a session to the archive, never into the vault.
- **Ingestion Gate (Switch):** pass = `Client Coaching` **and**
  confidence ≥ 0.80 → dual-stream parsing. Everything else → a Postgres
  insert into `rejected_sessions` (session_id, meeting_type, confidence,
  summary). No embedding call is made on the reject path.
- **Dual-Stream Parser (Code):** the coach is the team-member participant
  (Dr. Sara Hegy in the mock data); client turns enter neither stream.
  *Framework* = the coach's substantive turns (≥ 150 chars) joined as one
  dense extractive takeaway. *Voice_sample* = only the coach's verbatim
  turns, accumulated on turn boundaries into 400–700 character chunks (a
  final remainder may be shorter).
- **Embed + Upsert:** each chunk is embedded via the embedding endpoint, a
  Code node re-joins chunks to their vectors (paired by item order) and
  formats the pgvector literal, and a Postgres node upserts into
  `knowledge_vault` with JSONB metadata (title, coach, chunk index,
  classifier confidence).

### Workflow B — Gated Retrieval & Persona Synthesis

`Webhook → Greeting Router (Code) → IF → … → Embed Query → Gated Retrieval
(Postgres) → Circuit Breaker + Context (Code) → IF → Persona LLM → Respond`

- **Greeting Router:** a regex intercepts pure greetings (`hello`, `hi`,
  `hey`, `good morning`, …) and returns a static greeting — no embedding
  call, no database query.
- **Gated SQL:** one raw query with independent cutoffs per stream,
  computed in SQL with the cosine operator:

  ```sql
  1 - (embedding <=> $query) >= 0.70   -- frameworks,   LIMIT 2
  1 - (embedding <=> $query) >= 0.65   -- voice samples, LIMIT 2
  ```

- **Zero-match circuit breaker:** the Postgres node is set to always output
  data, so an empty result still reaches the Code node, which sets
  `short_circuit = true`. The IF node then responds with exactly
  `No documented coaching frameworks match this topic.` — the Persona LLM
  node sits on the other branch and is never executed.
- **Persona synthesis:** when rows pass, the Code node builds `{context}`
  with separate FRAMEWORKS and VOICE SAMPLES sections (with session IDs and
  similarities). The LLM prompt uses **positive few-shot examples** — two
  worked answers in Dr. Sara's style (the Table and Shoulder Test for an
  underperforming VP; Completion with Integrity for a micromanaging
  founder) — rather than a list of prohibitions.

## Test queries

| Query | Expected behaviour |
|---|---|
| `Hello!` | Static greeting; no embedding, no DB call |
| `How should I approach an underperforming VP?` | Retrieves SES-102 framework + voice samples; answers with the Table and Shoulder Test in Dr. Sara's voice |
| `I check everything my team produces. How do I stop?` | Retrieves SES-101; answers with Completion with Integrity |
| `How do I fix my database index?` | Zero rows pass the thresholds → `No documented coaching frameworks match this topic.` LLM not called |

Useful checks in Postgres:

```sql
SELECT session_id, stream_type, count(*) FROM knowledge_vault GROUP BY 1, 2;
SELECT session_id, meeting_type, confidence FROM rejected_sessions;
```

## Verification

**Primary path:** the quickstart above (`docker compose up`, import, run) —
the workflows, schema and SQL are the deliverable and are written to run
there unchanged.

**Additional logic test:** this repository was assembled in an environment
with **no Docker available**, so the stack itself could not be booted during
development. To verify everything that does not depend on the runtime, the
repo includes `verification/simulate_pipeline.py`, which runs the *same*
logic as the Code nodes (sampler, gate, dual-stream parser, chunking,
greeting regex, circuit breaker) against `data/mock_transcripts.json`. Two
stand-ins are used and labelled in the script: a deterministic rule-based
classifier in place of the LLM, and keyword overlap in place of cosine
similarity. It verifies the data-wrangling and gating behaviour, not LLM
judgement or embedding quality. Run it with:

```bash
python3 verification/simulate_pipeline.py
```

Actual output (`verification/verification_log.txt`), summary:

- **SES-101** — classified Client Coaching (stand-in confidence 0.93) →
  PASS. Ingested: **1 framework chunk + 2 voice_sample chunks** (683 and
  154 chars).
- **SES-102** — classified Client Coaching (1.00) → PASS. Ingested:
  **1 framework chunk + 2 voice_sample chunks** (655 and 132 chars).
- **SES-103** — classified Internal Operations (1.00) → **REJECTED**,
  archived in `rejected_sessions`, no embeddings.
- **SES-104** — classified Sales Discovery (1.00) → **REJECTED**, archived,
  no embeddings.
- Simulated vault total: **6 rows** (2 framework, 4 voice_sample);
  rejected archive: **2 rows**.
- `Hello!` → greeting intercepted, no DB call. `How do I fix my database
  index?` → 0 rows pass, circuit breaker returns the exact fallback, LLM
  not called. A bonus on-domain query about operational drift retrieved
  2 rows from SES-102.
- Both workflow JSONs were also validated as parseable JSON, and every
  Code node's JavaScript was syntax-checked.

Not verified in the build environment (needs the Docker quickstart):
a live n8n import/execution, a live pgvector query, and real LLM/embedding
responses.

## Architectural reflection

### Q1 — Why does threshold gating in SQL beat asking the LLM to say "I don't know"?

A prompt instruction is a request; a SQL `WHERE` clause is a guarantee.
An LLM told to admit ignorance still *receives* the nearest neighbours —
which, in a small vault, are always *something* — and language models are
strongly biased toward producing a fluent answer from whatever context they
are given, especially when the retrieved text is topically adjacent. The
instruction is also non-deterministic: the same query can pass or fail
depending on sampling, phrasing, or a model update, and there is no way to
audit afterwards *why* it answered. SQL gating moves the decision upstream
and makes it mechanical: similarity is computed with `1 - (embedding <=>
$query)`, rows below the cutoff never leave the database, and if the result
set is empty the workflow short-circuits before the LLM node exists in the
execution at all. That gives you a tunable, testable, loggable boundary —
you can raise 0.70 to 0.75 and measure the effect — and it saves the cost
and latency of a generation call on every off-domain query. The LLM is then
only ever asked to do what it is good at: phrasing an answer from context
that has already been proven relevant.

### Q2 — Trade-offs of setting HNSW `ef_search` higher vs lower

`ef_search` is the size of the candidate list HNSW keeps while walking the
graph at query time (set per session with `SET hnsw.ef_search = …`; it must
be at least the `LIMIT` you ask for).

- **Higher `ef_search`:** explores more of the graph, so recall rises — the
  true nearest neighbours are less likely to be missed, which matters here
  because a missed neighbour near the 0.70/0.65 cutoffs changes what the
  gate admits. The cost is latency (and a little CPU) per query: more
  candidates are visited and distance-checked.
- **Lower `ef_search`:** faster, cheaper queries, but the search can settle
  into a local neighbourhood and return approximate neighbours — in this
  system that means borderline chunks can be silently dropped or admitted,
  making the threshold behaviour feel inconsistent.

For a vault of this size (thousands of chunks, `LIMIT 2` per stream) the
default of 40 is already generous and latency is negligible, so there is
little reason to lower it. The knob earns its keep at much larger scale,
where you tune it against measured recall on a labelled query set rather
than guessing — raise it until recall plateaus, then stop, because past
that point you are only buying latency.

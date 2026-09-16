# Retrieval golden set — baseline

Whether retrieval returns *something* was already covered. This measures
whether it returns the **right** messages, and it is the only place in the
repo where that question has a number for an answer.

## Running it

It is opt-in: a run seeds a corpus and issues 84 searches against the real
embedding endpoint, so an ordinary `pytest` must not spend that money. It also
truncates every table in the database it points at, which is why it points at
one of its own.

```bash
docker compose -f docker-compose.dev.yml up -d
docker exec discord_agent-db-1 psql -U chatmemory -d postgres \
  -c "CREATE DATABASE chatmemory_goldens OWNER chatmemory;"
docker exec discord_agent-db-1 psql -U chatmemory -d chatmemory_goldens \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
DATABASE_URL='postgresql+asyncpg://chatmemory:chatmemory@localhost:5432/chatmemory_goldens' \
  .venv/bin/alembic upgrade head

# then, with OPENAI_API_KEY set (or the key file the integration suite reads):
.venv/bin/pytest tests/evaluation -m goldens -s
```

`-s` is what puts the per-question table on screen. `RUN_RETRIEVAL_GOLDENS=1`
is equivalent to `-m goldens`, and `GOLDENS_DATABASE_URL` overrides the
database. To re-record the block at the bottom of this file, add
`GOLDENS_WRITE_BASELINE=1` — the numbers are written from the run, never typed,
so the date and the model beside a number are always the ones that produced it.

Unlike the integration suite, this one **fails** rather than skips when the
database or the key is missing. Somebody asked for these numbers; a skipped
measurement reported as a pass is worse than no measurement.

## What it measures

The corpus is 167 messages in 52 conversations across four channels with four
different memberships — conversations that run for several turns, paraphrase
rather than repeat the questions, and include two pairs of deliberately
confusable topics. The 21 questions name the messages that actually answer
them, and include questions answerable only by meaning, only by an exact
identifier, differently depending on who asks, and not at all.

| dimension | what it is | gated? |
| --- | --- | --- |
| **ACL breaches** | every question asked as every viewer; any result from an unreadable channel, or citing a message from one | **yes — any breach fails** |
| recall@10 | share of the answer-bearing messages carried by the top ten | floor 0.80 |
| MRR | 1 / rank of the first result carrying an expected message | floor 0.65 |
| worst rank | the deepest any known answer was found | must be ≤ 8 |
| outranked | an answer beaten by its own near-duplicate | must be zero |
| bait at rank 1 | a no-answer question led by its trap | must be zero |
| clean_unanswerable | baited no-answer questions that avoided the bait entirely | **recorded, not gated** |

A breach is never averaged into a score. `AclBreach` is a value, not a float,
and no aggregate consumes one: a mean that can absorb a disclosure will
eventually report one as 0.94 and be believed.

The floors sit well under the measured numbers on purpose. They exist to catch
a retrieval change that breaks a question — a dropped leg, a broken fusion, a
permission predicate turned into a post-filter — not to freeze today's
embedding model in place. A failure names the question, because "recall fell
from 0.93 to 0.88" is not something anyone can act on.

## What today's numbers say, beyond the summary line

* **Permissions hold.** 84 searches across four memberships, zero results from
  an unreadable channel and zero windows citing a message from one.
* **Recall@10 is 1.0 and MRR is 0.877.** Every question's answer is found;
  seventeen of nineteen are found at rank one. The two that are not
  (`hiring-allowance` at 6, `overnight-stall` at 4) are both paraphrases whose
  question words appear in unrelated threads, so the lexical leg contributes
  noise that RRF then has to out-vote.
* **Neither near-duplicate pair is confused.** `payment-failures` and
  `slow-results` each rank their own incident first and their twin at 7 and 3.
* **The weakest dimension is "the answer is nothing", and it is weak in a
  specific, understood way.** `cloud-provider` pulls the database *migration*
  thread to rank 5 purely because "migrating" and "migration" share a stem —
  the lexical leg matching a word in a question the corpus cannot answer. Two
  of three baited no-answer questions do this. Nothing is *led* by its bait,
  which is why that is the gated claim and the 1/3 is only recorded: it is a
  proportion over three questions, and any threshold on it would move on noise.
* **Retrieval cannot express "nothing".** Both legs are top-k with no
  similarity floor, so a question with no answer still returns twenty windows.
  Refusing to answer from them is the grounding stage's job, and this file
  deliberately does not test it at the retrieval layer.

## Recorded run

<!-- recorded-run -->

```
retrieval golden set  k=10
measured_on=2026-09-16
embedding_model=text-embedding-3-small dimensions=1536
corpus messages=167 windows=52 viewers=4

acl=0 breaches  recall@10=1.000  mrr=0.877  clean_unanswerable=0.333  traps=4/21  outranked=0

ACL
  none

per question
  coffee-paraphrase            recall@10=1.00 rr=1.000 rank=1     n=20
  sleep-advice                 recall@10=1.00 rr=1.000 rank=1     n=20
  payment-failures             recall@10=1.00 rr=1.000 rank=1     n=20  trap@7=[200301, 200302, 200303, 200304]
  slow-results                 recall@10=1.00 rr=1.000 rank=1     n=20  trap@3=[200101, 200102, 200103, 200104, 200105, 200106]
  overnight-stall              recall@10=1.00 rr=0.250 rank=4     n=20
  hiring-allowance             recall@10=1.00 rr=0.167 rank=6     n=20
  supplier-payment             recall@10=1.00 rr=1.000 rank=1     n=20
  company-look                 recall@10=1.00 rr=1.000 rank=1     n=20
  error-code                   recall@10=1.00 rr=1.000 rank=1     n=20
  migration-name               recall@10=1.00 rr=1.000 rank=1     n=20
  test-name                    recall@10=1.00 rr=1.000 rank=1     n=20
  parking-badge                recall@10=1.00 rr=1.000 rank=1     n=20
  party-venue                  recall@10=1.00 rr=1.000 rank=1     n=20
  contrast-failure             recall@10=1.00 rr=1.000 rank=1     n=20
  illustration-brief           recall@10=1.00 rr=1.000 rank=1     n=20
  office-change-lead           recall@10=1.00 rr=0.500 rank=2     n=20
  office-change-staff          recall@10=1.00 rr=1.000 rank=1     n=20
  relocation-newcomer          unanswerable  trap_rank=-          n=20
  bike-shed                    unanswerable  trap_rank=7          n=20  trap@7=[100201, 100202, 100203, 100204]
  cloud-provider               unanswerable  trap_rank=5          n=20  trap@5=[200201, 200202, 200203, 200204, 200205]
  unpaid-leave                 unanswerable  trap_rank=-          n=20

notes
  searches: 21 questions x 4 viewers, limit=20, scored at k=10
```

# RagDex API

FastAPI backend for the RagDex trading journal. Self-contained in `backend/` —
the Vite front end and the existing Vercel functions are untouched.

## Layout

```
backend/
├── app/
│   ├── main.py              app factory: settings, middleware, routers
│   ├── core/                config, auth, errors, logging, middleware
│   ├── api/
│   │   ├── deps.py          the dependencies routes are allowed to use
│   │   └── v1/              versioned routes, assembled in router.py
│   ├── schemas/             pydantic request/response models
│   ├── services/            business logic — stats, coach, insights
│   ├── repositories/        the only code that touches Firestore
│   └── db/firestore.py      Admin SDK setup
└── tests/
```

The rule the structure encodes: routes validate and delegate, services decide,
repositories persist. A route never imports Firestore, and a service never sees
a `Request`. That is what keeps `services/stats.py` — where every number in the
product comes from — testable without a network.

## Running it

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -e ".[dev]"
cp .env.example .env                              # then fill it in
uvicorn app.main:app --reload
```

Docs at `http://localhost:8000/docs` — disabled in production.

```bash
pytest          # tests
ruff check .    # lint, including bandit security rules
mypy app        # types, strict
```

## Security

**Identity.** The only identity trusted is a Firebase ID token verified by this
service, with `check_revoked=True` so a signed-out session stops working
immediately rather than at token expiry. No route accepts a uid — not in a
path, a query string, a body or a header. There is therefore no request a
client can construct that addresses another trader's journal.

**Writes need a confirmed email**, mirroring `firestore.rules`. Reads use
`ReadUser`, writes use `WriteUser`; the distinction is visible in every
signature.

**The Admin SDK bypasses Firestore rules entirely.** That is why every function
in `repositories/` takes `uid` first and reaches data through
`trades_collection(uid)`. There is no code path that builds a query spanning
accounts. If one is ever added, tenant isolation is gone — the rules are not
there to catch it.

**Input.** Every schema is `extra="forbid"`, so a client cannot smuggle an
unknown field into a document. Money and sizes are bounded. Screenshot URLs
must be http(s), because a `javascript:` URL stored here becomes a link the
client renders. P&L and R:R are computed server-side: a figure the caller can
set is a figure that can be made up, and every statistic is built on them.

**Output.** One error envelope, with a request id and never a traceback, a
document path, or an upstream provider's message. Those go to the log, where
the same id makes them findable.

**Transport.** Explicit CORS and host allowlists — no wildcards. Security
headers on every response, including `Cache-Control: no-store`, because
responses are per-user and must never be reused by a shared cache. Request
bodies are capped before parsing, including chunked ones.

**Rate limits** are per-uid, with a smaller budget for model-backed routes than
for journal reads. The limiter is in-process: it is a guard rail for a single
instance, not a distributed quota. **Running more than one replica means moving
it to Redis** — the interface is unchanged, only the storage.

**Secrets** are `SecretStr`, read from the environment, never logged or
returned. The service account is a full-access credential; `.gitignore` blocks
every name it is commonly saved under.

## Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/health` | Liveness. No dependencies touched. |
| `GET` | `/ready` | Readiness. 503 when Firestore is unreachable. |
| `GET` | `/api/v1/me` | Your account; upserts on read. |
| `PATCH` | `/api/v1/me` | Edit your own details. |
| `PUT` | `/api/v1/me/plan` | Individual or Coach. Nothing is charged. |
| `GET` | `/api/v1/trades` | Cursor-paged, newest first. |
| `POST` | `/api/v1/trades` | P&L computed server-side. |
| `GET/PATCH/DELETE` | `/api/v1/trades/{id}` | Scoped to you. |
| `POST` | `/api/v1/coach/chat` | Journal read server-side, never from the body. |
| `GET` | `/api/v1/insights/behavioral-leak` | `needed` when history is too short. |

Paging is cursor-based, not offset: a journal is append-heavy, and an offset
silently skips or repeats rows when something is inserted mid-read.

## How this relates to the existing app

Right now the web client talks to Firestore directly for trades and profile,
and to Vercel functions for the coach. This service implements all of it, so
you can move over one endpoint at a time — point `src/lib/trades.ts` at
`/api/v1/trades` first, leave the rest, and nothing else has to change.

Worth knowing before you migrate: going through the API means P&L is computed
in one place instead of two, and `firestore.rules` stops being the only thing
between a compromised client and the data. It also costs a network hop the
direct SDK does not have, and loses Firestore's realtime listeners — the
journal would need polling or a websocket to stay live.

`coach.md` at the repository root is still the coach's voice; this service
reads the same file.

## Deploying

`Dockerfile` builds a multi-stage image that runs as a non-root user with no
build toolchain in the runtime layer. Process count is left to the
orchestrator, which knows how much CPU the container was actually given.

On Cloud Run or GKE, leave `FIREBASE_SERVICE_ACCOUNT` unset and attach a
service account to the workload instead — Application Default Credentials are
picked up automatically, and no key file exists to leak.

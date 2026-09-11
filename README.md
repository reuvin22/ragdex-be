# RagDex API

FastAPI backend for the RagDex trading journal. Its own repository — the Vite
front end and the existing Vercel functions are untouched.

## Layout

```
.
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
├── coach.md                 the coach’s voice, read at request time
└── tests/
```

The rule the structure encodes: routes validate and delegate, services decide,
repositories persist. A route never imports Firestore, and a service never sees
a `Request`. That is what keeps `services/stats.py` — where every number in the
product comes from — testable without a network.

## Running it

```bash
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

`coach.md` is still the coach's voice, but this repository now carries its own
copy — a service that has to reach into a sibling checkout to boot cannot be
deployed on its own. The two are the same text today; edit both, or make one of
them the source and copy it across, because nothing enforces it.

## Deploying

`Dockerfile` builds a multi-stage image from this directory that runs as a
non-root user with no build toolchain in the runtime layer. It binds `$PORT`,
falling back to 8000. Process count is left to the orchestrator, which knows
how much CPU the container was actually given.

### Render

`render.yaml` is a Blueprint: in Render, **New → Blueprint**, pick this
repository, and it creates the service. Then fill in the three values the file
deliberately leaves blank, because two are secrets and one depends on where the
front end is hosted:

| Variable | What goes in it |
| --- | --- |
| `FIREBASE_SERVICE_ACCOUNT` | The whole service account JSON, one line. |
| `CORS_ORIGINS` | The web app's exact origin, e.g. `https://ragdex.vercel.app`. |
| `OPENROUTER_API_KEY` | Only the coach and the leak card need it. |

Two things differ from Cloud Run and are worth knowing before the first
deploy:

**There are no Application Default Credentials.** `init_firebase()` runs at
startup, so a missing or malformed `FIREBASE_SERVICE_ACCOUNT` fails the deploy
rather than the first request — which is the behaviour you want, but it means
the variable has to be set before the service will ever come up.

**`ALLOWED_HOSTS` has to include the Render hostname.** `TrustedHostMiddleware`
answers 400 to anything else, health checks included. The Blueprint sets
`ragdex-api.onrender.com,*.onrender.com,localhost,127.0.0.1`; correct the first
entry if Render appended a suffix to the service name. A deploy that builds but
is marked unhealthy, with 400s in the log, is always this.

The free instance sleeps after 15 minutes idle and takes roughly a minute to
come back, so the first request after a quiet spell is slow. It is also a
single instance, which is what the in-process rate limiter assumes — scaling
past one replica means moving the limiter to Redis.

### Cloud Run or GKE

Leave `FIREBASE_SERVICE_ACCOUNT` unset and attach a service account to the
workload instead — Application Default Credentials are picked up
automatically, and no key file exists to leak.

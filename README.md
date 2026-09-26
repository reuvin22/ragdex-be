# RagDex API

FastAPI backend for the RagDex trading journal.

**This service is the only thing that holds a credential.** The web client has
no Firebase SDK, no vendor key and no project configuration: it knows one
address and talks to nothing else. Sign-in, the journal, the coach, contact
search and chat all happen here.

## Layout

```
.
├── app/
│   ├── main.py              app factory: settings, middleware, routers
│   ├── core/                config, auth, error types, logging, middleware
│   ├── controllers/
│   │   ├── deps.py          the dependencies controllers may use
│   │   └── v1/              versioned controllers, assembled in router.py
│   ├── models/
│   │   ├── schemas/         pydantic request/response models
│   │   └── repositories/    the only code that touches Firestore
│   ├── views/               rendering — the JSON error envelope, email HTML
│   ├── services/            business logic — stats, coach, insights
│   └── db/firestore.py      Admin SDK setup
├── coach.md                 the coach’s voice, read at request time
└── tests/
```

Model, view, controller, with the decisions in a fourth layer. The rule the
structure encodes: controllers validate and delegate, services decide,
repositories persist. A controller never imports Firestore, and a service never
sees a `Request`. That is what keeps `services/stats.py` — where every number in
the product comes from — testable without a network.

`views/` is small, and honestly so: a JSON API renders almost nothing by hand.
What lives there is the work that genuinely turns a result into bytes — the
error envelope every failure is wrapped in, and the table-based HTML of the
verification email. The exceptions themselves stay in `core/errors.py`, because
a service raising `NotFoundError` is stating a fact, not choosing a status code.

`services/` sits outside the triad deliberately. Folding it into the model would
put coach prompting and R2 signing next to Firestore documents; leaving it out
would push that logic into controllers, which is the failure mode the split
exists to prevent.

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

**Identity.** The only identity trusted is a session cookie this service minted
and has just verified, with `check_revoked=True` so a signed-out session stops
working immediately rather than at expiry. The cookie is HttpOnly: script on the
page cannot read it, which a bearer token in localStorage cannot claim. No route
accepts a uid — not in a path, a query string, a body or a header. There is
therefore no request a client can construct that addresses another trader's
journal, or another trader's messages.

The cost of a cookie is CSRF, and `SameSite` is the answer — which is why the
API and the app are arranged to be same-site. See `SESSION_COOKIE_SAMESITE` and
the rewrite in the client's `vercel.json`.

**Passwords never touch this service's storage.** They go to Identity Toolkit
and nowhere else. `app/services/identity.py` also decides what an upstream
failure is allowed to reveal: a wrong password and an unknown address return the
same message, because distinguishing them hands an attacker an oracle for which
addresses are registered.

**Writes need a confirmed email.** Reads use `ReadUser`, writes use
`WriteUser`; the distinction is visible in every signature.

**The Admin SDK bypasses Firestore rules entirely.** That is why every function
in `models/repositories/` takes `uid` first and reaches data through
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
for journal reads. The sign-in routes are the exception and are keyed by client
address, because they are the only ones reachable without a session — and a
password endpoint with no ceiling is a guessing machine. The limiter is in-process: it is a guard rail for a single
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
| `GET` | `/api/v1/auth/session` | Who am I. 200 with a null user when nobody is. |
| `POST` | `/api/v1/auth/register` | Create an account and start a session. |
| `POST` | `/api/v1/auth/login` | Email and password. Sets the session cookie. |
| `POST` | `/api/v1/auth/logout` | Clears the cookie and revokes every session. |
| `POST` | `/api/v1/auth/verify-email` | Sends the branded Brevo message. |
| `POST` | `/api/v1/auth/password-reset` | Answers the same whether or not the address exists. |
| `GET` | `/api/v1/auth/google/start` | Redirects the browser to Google. |
| `GET` | `/api/v1/auth/google/callback` | Where Google returns. Always answers with a redirect. |
| `GET` | `/api/v1/me` | Your account; upserts on read. |
| `PATCH` | `/api/v1/me` | Edit your own details. |
| `PUT` | `/api/v1/me/plan` | Individual or Coach. Nothing is charged. |
| `GET` | `/api/v1/trades` | Cursor-paged, newest first. |
| `POST` | `/api/v1/trades` | P&L computed server-side. |
| `GET/PATCH/DELETE` | `/api/v1/trades/{id}` | Scoped to you. |
| `POST` | `/api/v1/coach/chat` | Journal read server-side, never from the body. |
| `GET` | `/api/v1/insights/behavioral-leak` | `needed` when history is too short. |
| `GET` | `/api/v1/market/candles` | Public market data, not yours. 503 when unconfigured. |
| `GET/POST` | `/api/v1/university/students` | Your roster; figures derived from each journal. |
| `GET` | `/api/v1/university/students/{uid}/journal` | A student's trades. Refused without an accepted enrolment. |
| `POST` | `/api/v1/university/invites` | Invite by email. Sends the coach's template. |
| `POST` | `/api/v1/university/invitations/{uid}/accept` | `{uid}` names the coach, not you. |
| `GET/PUT` | `/api/v1/university/settings` | The programme and its invitation email. Markup sanitised on write. |
| `GET/POST` | `/api/v1/community/posts` | The public feed. |
| `GET` | `/api/v1/uploads/public/{key}` | 302 to a signed URL. `university/` keys only, no session. |
| `GET` | `/api/v1/chat/directory` | Find a trader by email. Three-character floor. |
| `GET/POST` | `/api/v1/chat/contacts` | Your conversations; opening a new one. |
| `GET` | `/api/v1/chat/threads/{uid}` | One conversation, addressed by person. |
| `POST` | `/api/v1/chat/threads/{uid}/messages` | Send. The body carries text and nothing else. |
| `POST` | `/api/v1/chat/threads/{uid}/seen` | Stamp the read marker. |

Paging is cursor-based, not offset: a journal is append-heavy, and an offset
silently skips or repeats rows when something is inserted mid-read.

`/market/candles` is the one route whose answer does not depend on who asked.
Candles are public market facts, identical for every trader, so the cache
behind them is global and the handler never consults a uid. It still requires
a session — that is what keeps the endpoint, and the provider quota behind it,
off the open internet — and it carries a smaller rate limit than a journal
read, because every cache miss spends money upstream. It takes no uid, like
everything else here; it simply has no use for one.

No chat route takes a thread id. A conversation is addressed by the person at
the other end, and the thread is derived from that uid plus the caller's, which
is what makes it impossible to name a conversation you are not in.

## How this relates to the web client

The client talks to this service and to nothing else. It has no Firebase SDK,
no vendor key and no project id — `trades/src/lib/api.ts` is its entire
outbound surface, and `VITE_API_BASE_URL` its entire configuration.

Two consequences worth knowing:

**Firestore rules are no longer a security boundary.** They protected a client
that read the database directly; nothing does now, and `firestore.rules` is
deny-all. What protects the data is this service — every repository function
takes a verified uid first, and the Admin SDK bypasses rules regardless.

**Realtime listeners are gone.** That is the real cost of the move: a browser
that can subscribe to a collection is a browser holding credentials for it. The
journal refetches after a write. The chat dock polls — four seconds while open,
a minute while closed, skipped entirely in a hidden tab. Making chat feel live
again means SSE or a websocket here, not a database handle there.

`coach.md` is the coach's voice, and this repository is now its only home —
the client's copy went with the Vercel functions it fed. Edit it here.

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

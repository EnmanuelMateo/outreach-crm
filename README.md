# DR SMB Outreach CRM

A single-user personal CRM for the Dominican Republic SMB website/systems outreach
campaign. Rebuilds the `DR_Outreach_Tracker.xlsx` data model and Dashboard logic as a
real, mobile-first web app. **No login by design** — one user, personal use.

- **Stack:** Flask + SQLAlchemy Core + Jinja2 (server-rendered). No frontend framework.
- **Database:** SQLite locally, **Postgres in production** — selected by the
  `DATABASE_URL` env var. One codebase, both backends.
- **Design:** "Classical Nocturne" — warm near-black editorial theme, gold accent.
- **Currency:** Dominican pesos (RD$ / DOP). **Default province:** San Cristóbal.

---

## What's in the box

| File | Purpose |
|---|---|
| `app.py` | Flask app + all routes |
| `domain.py` | Enums + computed fields (`follow_up_status`, `stage_order`) — single source of truth |
| `db.py` | SQLAlchemy data layer (SQLite ↔ Postgres) + the live Dashboard metrics |
| `seed_import.py` | One-time import of the 42 researched San Cristóbal candidates |
| `templates/`, `static/` | HTML + CSS (mobile-first, desktop split-view) |
| `requirements.txt` | Dependencies |
| `render.yaml`, `Procfile`, `Dockerfile` | Deploy configs (Render / any host) |
| `seed_data/` | Bundled source spreadsheets so the seed runs anywhere |

The schema is defined in code (`db.py` SQLAlchemy `metadata`) and created automatically
on first run — there is no separate `.sql` file to keep in sync.

---

## Run it locally (SQLite)

From the `crm/` folder:

```bash
pip install -r requirements.txt
python seed_import.py        # loads the 42 San Cristóbal candidates (once)
python app.py                # http://127.0.0.1:5000
```

Re-seeding: `python seed_import.py` refuses to run if the DB already has companies.
Use `python seed_import.py --reset` to wipe and reload the seed.

---

## Pages

- **`/` Dashboard** — overdue follow-ups are the most prominent thing on the page, then
  the stat figures, the descending-bar funnel with stage-to-stage conversion, lost rate,
  Won deal value, and counts by province/sector.
- **`/companies`** — filter by province / sector / stage, search, sort. **Desktop:** a
  two-pane split (table left, detail/edit right). **Mobile:** card list + a `+` FAB.
- **Add** — 3-field quick capture (name · phone · sector) with the rest folded into
  "Más detalles"; sensible defaults (San Cristóbal · Identified · follow-up +3 days).
- **Company detail** — WhatsApp-first; "Mover en el pipeline" is the daily action:
  change stage, log a dated note, set the next follow-up, capture deal value (Won) or
  reason (Lost).
- **CSV export** — the `↓ CSV` button exports the *current filtered* list.

Follow-up colors: Vencido = red, Hoy = amber, Próximo = green; Won = green, Lost = red.

---

## Dashboard math (matches the spreadsheet exactly)

- **Funnel** — for each stage Identified→Won: count *currently* at that stage, plus a
  cumulative "reached this stage or further" = companies with `stage_order >= N` and
  `stage != Lost` (Lost excluded from the funnel).
- **Conversion** — `cumulative(N) / cumulative(N-1)`, shown as a % next to each stage.
- **Lost rate** — `lost / (identified-reached + lost)` = lost / total.
- **Deal value** — total and average `deal_value_dop` where stage = Won.

---

## Deploy to production — Render + Supabase Postgres

GitHub → Render auto-deploys on every push; the data lives in Supabase Postgres
(free, persistent, automatic backups). Local dev keeps using SQLite unchanged.

### 1. Supabase — create the database
1. Create a free project at https://supabase.com.
2. **Project Settings → Database → Connection string → URI.** Copy it (looks like
   `postgresql://postgres:PASSWORD@db.xxxx.supabase.co:5432/postgres`). This is your
   `DATABASE_URL`. `db.py` auto-rewrites the scheme to the `psycopg` driver.

### 2. GitHub — push this `crm/` folder as a repo
```bash
cd crm
git init && git add . && git commit -m "Outreach CRM"
gh repo create outreach-crm --private --source=. --push   # or create it in the browser
```

### 3. Render — connect and deploy
1. At https://render.com: **New → Blueprint**, pick the GitHub repo. Render reads
   `render.yaml` and provisions the free web service.
2. On the service, add these env vars, then **Deploy**:
   - **`DATABASE_URL`** = your Supabase URI (creates the tables on first boot).
   - **`SECRET_KEY`** = any long random string (signs the login session; without it you
     get logged out on every restart).
   - **`CRM_USER`** = your username, **`CRM_PASSWORD`** = your password. Setting these
     turns on the login gate. *(Alternatively set `CRM_PASSWORD_HASH` from
     `werkzeug.security.generate_password_hash` instead of the plaintext `CRM_PASSWORD`.)*

**Login:** with `CRM_PASSWORD` (or `CRM_PASSWORD_HASH`) set, the whole app requires
sign-in at `/login`; **Salir** logs out. Locally, if no password env is set, the gate is
off so dev stays open. Change the password anytime by updating the Render env var.

### 4. Seed production once (from your laptop, pointing at Postgres)
```bash
cd crm
DATABASE_URL="postgresql://postgres:PASSWORD@db.xxxx.supabase.co:5432/postgres" \
  python seed_import.py            # prints "→ Postgres", expect 42
```

### 5. Verify
Open the Render URL (`https://outreach-crm.onrender.com`) on your phone — 42 companies,
add/advance/CSV/WhatsApp all work. Add it to your home screen. Push to `main` → Render
redeploys. Your data is safe in Postgres across every deploy.

**Notes.** Render's free web service sleeps when idle (~30–60s cold start on the first
hit — fine for personal use; Railway avoids it with the same repo). Never commit
`DATABASE_URL` — it holds the DB password; set it only in Render and your shell.

Any Docker host (Railway, Fly.io) works too via the included `Dockerfile` — same
`DATABASE_URL` contract.

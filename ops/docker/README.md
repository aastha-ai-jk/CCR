# Running the CCR twin + advisor in Docker

One image (`ccr-twin-app`) runs every backend-side process; a second
(`ccr-twin-frontend`) runs the dashboard. `docker-compose.yml` (repo root)
replaces the PM2 setup in `ecosystem.config.js`:

| PM2 app | Compose service | What it does |
|---|---|---|
| ccr-backend | `backend` (port 8010) | API + `/advisor` page |
| ccr-livefeed | `livefeed` | KEPServer OPC UA (`opc.tcp://10.4.222.139:49320`) → DB every 2 s |
| ccr-shadow | `shadow` | twin beside the CCR, live accuracy + bias, every 30 s |
| ccr-optimizer | `optimizer` | plant fuel-advisory loop (`OPTIMIZER_MODE=cost`), every 45 s |
| ccr-monitor + ccr-retrain | `scheduler` | monitor every 6 h; guarded retrain Sunday 02:00 |
| ccr-frontend | `frontend` (port 3010) | Next.js dashboard |
| — | `elims` (optional, `--profile elims`) | eLIMS clinker free-lime feed every 15 min (needs `.env.elims`) |

Data and models are **volumes** (`./data`, `./models`), so the SQLite database,
`plant_settings.json`, the trained model and logs survive container rebuilds.
Nothing is baked into the images (see `.dockerignore`). The model reloads by
itself when the weekly retrain promotes a new file.

## Prerequisites
- Docker Engine + Compose plugin on the host (the GPU box currently has **no
  Docker** — IT must install it, and the `antpc` user, or whoever runs it, needs
  to be in the `docker` group).
- The host can reach the OPC server `10.4.222.139:49320` (and eLIMS for `elims`).
- In the project folder on the host: `data/` (with `digital_twin.sqlite` and
  `plant_settings.json`), `models/` (with `twin_gbt.pkl`), `.env`.

## First run on the GPU box (replacing PM2)
```bash
cd /home/antpc/CCR-Project
cp ops/docker/compose.env.example .env.compose
#   edit .env.compose: APP_UID=$(id -u antpc), PUBLIC_API_BASE=http://10.53.2.10:8010
docker compose --env-file .env.compose build
pm2 stop all            # never run PM2 and Docker side by side (double OPC reads / DB writes)
docker compose --env-file .env.compose up -d
docker compose ps        # backend becomes "healthy" after the model loads (~1-3 min)
curl -s localhost:8010/api/health
```
Then open `http://10.53.2.10:8010/advisor` and `http://10.53.2.10:3010/twin`.

## Everyday
```bash
docker compose logs -f backend shadow          # follow logs
docker compose restart shadow                    # restart one service
docker compose exec backend python ops/docker/scheduler.py --once monitor   # run a job now
docker compose --env-file .env.compose up -d --build   # deploy new code (after git pull)
```

## Roll back to PM2
```bash
docker compose down
pm2 start ecosystem.config.js && pm2 save
```

## Status
Written and statically checked (compose parses, every module and COPY source
exists, scheduler timing tested). **Not yet built or run** — no Docker on the
development laptop. First build should happen on a test host before the GPU box.

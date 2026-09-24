// PM2 process config for the CCR digital-twin app on the GPU box.
// Start:  pm2 start ecosystem.config.js && pm2 save
module.exports = {
  apps: [
    {
      name: "ccr-backend",
      cwd: "/home/antpc/CCR-Project",
      script: "/home/antpc/CCR-Project/.venv/bin/python",
      args: "-m uvicorn backend.main:app --host 0.0.0.0 --port 8010",
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      env: { PYTHONUNBUFFERED: "1" },
    },
    {
      name: "ccr-frontend",
      cwd: "/home/antpc/CCR-Project/frontend",
      script: "npm",
      args: "run start -- -H 0.0.0.0 -p 3010",
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      env: { NODE_ENV: "production", PORT: "3010" },
    },
    {
      name: "ccr-livefeed",
      cwd: "/home/antpc/CCR-Project",
      script: "/home/antpc/CCR-Project/.venv/bin/python",
      args: "-m etl.live_feed --interval 2",
      interpreter: "none",
      autorestart: true,
      max_restarts: 30,
      env: { PYTHONUNBUFFERED: "1" },
    },
    {
      name: "ccr-shadow",
      cwd: "/home/antpc/CCR-Project",
      script: "/home/antpc/CCR-Project/.venv/bin/python",
      args: "-m twin.shadow --interval 30",
      interpreter: "none",
      autorestart: true,
      max_restarts: 30,
      env: { PYTHONUNBUFFERED: "1" },
    },
    {
      // Continuous fuel-advisory loop: economic setpoint optimiser (min ₹/ton |
      // max TSR | min coal) + clinker-quality gate, logged to optimizer_log with
      // realised-savings M&V. ADVISORY/OBSERVE-ONLY. Set OPTIMIZER_MODE to switch
      // objective emphasis. Serves /api/fuel, /api/savings, /api/optimizer.
      name: "ccr-optimizer",
      cwd: "/home/antpc/CCR-Project",
      script: "/home/antpc/CCR-Project/.venv/bin/python",
      args: "-m twin.optimizer_loop --interval 45",
      interpreter: "none",
      autorestart: true,
      max_restarts: 30,
      env: { PYTHONUNBUFFERED: "1", OPTIMIZER_MODE: "cost" },
    },
    {
      // Weekly guarded retrain (Sun 02:00): ingest live data -> retrain ->
      // promote only if not worse -> reload serving. models/retrain_log.jsonl.
      name: "ccr-retrain",
      cwd: "/home/antpc/CCR-Project",
      script: "/home/antpc/CCR-Project/.venv/bin/python",
      args: "-m twin.retrain",
      interpreter: "none",
      autorestart: false,
      cron_restart: "0 2 * * 0",
      env: { PYTHONUNBUFFERED: "1" },
    },
    {
      // 6-hourly monitoring job: accuracy trail, conformal recalibration,
      // AFR summary, followed-vs-ignored M&V. Appends models/monitor_log.jsonl.
      name: "ccr-monitor",
      cwd: "/home/antpc/CCR-Project",
      script: "/home/antpc/CCR-Project/.venv/bin/python",
      args: "-m twin.monitor",
      interpreter: "none",
      autorestart: false,
      cron_restart: "0 */6 * * *",
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};

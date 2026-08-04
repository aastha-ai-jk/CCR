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
  ],
};

/** PM2 definition for the isolated FaceTime media bridge. */
module.exports = {
  apps: [{
    name: "chusky-voice",
    cwd: __dirname,
    script: "app.py",
    interpreter: "./.venv/bin/python",
    exec_mode: "fork",
    instances: 1,
    autorestart: true,
    min_uptime: "10s",
    max_restarts: 10,
    restart_delay: 3000,
    kill_timeout: 15000,
    max_memory_restart: "500M",
    env: { VOICE_BRIDGE_HOST: "127.0.0.1", VOICE_BRIDGE_PORT: "3004" },
  }],
};

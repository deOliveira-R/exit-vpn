# Connection monitor

What a Mac using the exit node actually experiences, logged for days, to find what makes
connections drop and to check a fix against the same measure. Runs on the Mac (stdlib Python,
`curl`, the `tailscale` CLI); reads the exit's `/v1/events` and `/v1/status`; changes nothing.

- `install.sh` — install as a launchd agent (`org.exit-vpn.connection-monitor`: starts now and at
  login, restarted if it exits); `install.sh --remove` removes it.
- `mark.sh "what happened"` — a note in the log at the moment a drop is noticed.
- `connection_monitor.py` — the monitor; its docstring lists every record kind.

Logs: `~/Library/Logs/exit-vpn/connection-YYYY-MM-DD.jsonl` (one JSON object per line, UTC
dates). Cost: two probes every 10 s and two downloads trickling at 256 B/s (about 45 MB a day).

The exit's side of the same window is in its container log (rotated at 5 × 20 MB, which holds
days): `sudo docker logs -t --since 48h exit-proton` on the VPS (IPv6 gate, route-keeper,
tailscaled path changes).

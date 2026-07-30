# Security

`orchestra ui` listens on loopback by default. It does not provide application-level authentication.

`orchestra ui --tailscale` binds only to the machine's Tailscale IPv4 address. Anyone allowed by your tailnet ACLs to reach that host and port can view the registered projects' run metadata, prompts, transcripts, and logs, stop active runs from the details pane, and restart the dashboard server. Review those ACLs before enabling tailnet access.

Provider credentials stay in the server process. The browser API receives normalized quota state, never API keys, access tokens, credential file contents, or Codex reset-credit identifiers.

Operator contracts and approvals are private control-plane state. Orchestra
stores them in an owner-only directory and SQLite file, rejects
credential-bearing contract fields, and snapshots only registered project
bindings. Do not put credentials in free-text goals, gates, or escalation
notes; those fields are durable by design.

Report vulnerabilities through [GitHub's private vulnerability reporting](https://github.com/batteryshark/orchestra/security/advisories/new). Do not include secrets or private run logs in a public issue.

# Security

`orchestra ui` listens on loopback by default. It does not provide application-level authentication.

`orchestra ui --tailscale` binds only to the machine's Tailscale IPv4 address. Anyone allowed by your tailnet ACLs to reach that host and port can view the registered projects' run metadata, prompts, transcripts, and logs. Review those ACLs before enabling tailnet access.

Provider credentials stay in the server process. The browser API receives normalized quota state, never API keys, access tokens, credential file contents, or Codex reset-credit identifiers.

Report vulnerabilities through [GitHub's private vulnerability reporting](https://github.com/batteryshark/orchestra/security/advisories/new). Do not include secrets or private run logs in a public issue.

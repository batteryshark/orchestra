# Linux and Windows through WSL

Orchestra officially supports Linux with Python 3.11 or newer. Windows is
supported by running Orchestra and every worker harness inside Windows
Subsystem for Linux (WSL). Native Win32 execution from PowerShell or Command
Prompt is intentionally out of scope.

## Linux installation

Install Git, Python 3.11 or newer, and `uv`, then install Orchestra from the
repository:

```sh
git clone https://github.com/batteryshark/orchestra.git
cd orchestra
uv tool install --editable .
orchestra doctor
```

Install and authenticate at least one worker CLI—OpenCode, Codex, or Claude
Code—in the same Linux environment. Orchestra launches those executables
directly, so a harness installed only on the Windows host is not visible to an
Orchestra process running inside WSL.

Global configuration is stored at `~/.config/orchestra/config.toml` by
default. Project state, run logs, briefs, and the SQLite database remain under
the project's `.orchestra/` directory. Override the global config path with
`ORCHESTRA_CONFIG` when required.

Initialize and verify a project:

```sh
cd ~/projects/example
orchestra init
orchestra doctor
orchestra ui --no-open
```

The dashboard binds to `127.0.0.1` by default and is normally reachable from a
browser on the same Linux machine. Remote Linux hosts should use an SSH tunnel
or Orchestra's explicit Tailscale mode; do not expose the unauthenticated UI on
a public interface.

## WSL installation

Install WSL 2 and a Linux distribution, then perform the entire Linux
installation above from that distribution's shell. Keep Orchestra, the agent
CLIs, their credentials, Git, and the project in the same WSL distribution.

Prefer repositories in the WSL filesystem:

```sh
mkdir -p ~/projects
cd ~/projects
git clone <your-project-url>
```

Projects under `/mnt/c` or another mounted Windows drive can work, but Git,
worktrees, file watching, permissions, and agent filesystem scans are usually
more reliable and faster under the distribution's native filesystem.

The Windows browser normally reaches `orchestra ui` through its printed
localhost URL. If a particular WSL networking configuration does not forward
localhost, use the distribution's current IP address and an explicit safe
networking setup instead of binding the dashboard broadly without access
controls.

WSL must remain running while workers are active. Stopping the distribution or
rebooting Windows terminates its Linux processes; use Orchestra's persisted run
state and session-resume commands after the environment starts again.

## Supported boundary

The supported configuration is:

- Orchestra runs as a Linux process.
- OpenCode, Codex, and Claude Code workers run in that same Linux/WSL environment.
- Git worktrees and project files are managed from Linux.
- Process interruption and cancellation use POSIX process groups.

Native Windows process management, Windows Job Objects, PowerShell-native
workers, and cross-boundary orchestration of harnesses installed only on the
Windows host are not supported.

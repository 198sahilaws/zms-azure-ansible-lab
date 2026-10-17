# baseline role

Cross-platform baseline applied to every managed host. The entry point
(`tasks/main.yml`) dispatches to `linux.yml` or `windows.yml` based on
`ansible_os_family`, so the role is safe to include from either OS-scoped play.

## What it does

- Sets a consistent timezone.
- Installs a small set of baseline packages (override via vars).
- On Linux, ensures the time-sync service is enabled.

## Key variables

See `defaults/main.yml`. Common overrides:

| Variable | Default | Notes |
|---|---|---|
| `baseline_timezone` | `Asia/Kolkata` | Linux timezone (IST) |
| `baseline_windows_timezone` | `India Standard Time` | Windows timezone id (IST) |
| `baseline_linux_packages` | `[htop, curl, vim]` | apt/dnf packages |
| `baseline_windows_packages` | `[7zip]` | Chocolatey packages (signed) |
| `baseline_chrony_service` | auto: `chronyd` (RedHat) / `chrony` (Debian) | override per host/group if needed |

## Hardened Windows note

Prefer signed installers (`win_package` / signed `win_chocolatey` packages) over
large custom PowerShell. Under WDAC/AppLocker enforce — especially Constrained
Language Mode — Ansible's staged PowerShell can fail; route CLM-locked hosts to
the golden-image path instead of runtime push.

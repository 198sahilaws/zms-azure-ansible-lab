# ZMS flow tests (web-app-adds)

Quick reference. The full step-by-step procedure is in `FLOW-TESTS-GUIDE.md`.

Generates traffic for every flow in Table 5 so it appears in ZMS flow data, and later checks the
enforced policy: allowed flows must connect, denied flows must not. **Manual use only**: nothing
here is called by `converge.sh`, `orchestrate.yml` or any timer.

## Two commands

```bash
sudo /opt/control-repo/scripts/flow-test.sh learn    # while ZMS learns flows
sudo /opt/control-repo/scripts/flow-test.sh verify   # after the policy is enforced
```

| | `learn` | `verify` |
|---|---|---|
| `flow-exercise.yml` F1-F3, F7-F10 | yes | yes |
| `flow-internet.yml` F10 sites | yes (client skipped) | yes, plus client (N7) |
| `flow-bastion.yml` F5, F6 | yes | yes, plus bastion negatives (N6) |
| `flow-negative.yml` N1-N5 | **never** | yes |
| Rounds | 6, 30 s apart | 1 |
| A denied flow that connects | not tested | **fails the run** |

`learn` never sends a denied flow, so the learning data holds only the flows you intend to allow.
Extra arguments go to `ansible-playbook`, e.g. `flow-test.sh learn -e flow_rounds=12`.
Skip a playbook with `FLOW_SKIP=flow-bastion sudo -E .../flow-test.sh learn`.

Results: one line per probe in `/var/log/ansible/flow-exercise.log`, each tagged `run=<id>`;
the script prints a per-flow summary and every failure at the end.

## What each flow test does

Every probe runs **on the flow's source host**, so ZMS records the right source. No logins, no
changes to any host.

| Flow | Source -> destination | Probe |
|---|---|---|
| F1 | client -> web 8090 | HTTP GET `/api/tickets` (the web tier proxies it, so it also drives real F2 and F3) |
| F2 | web -> middleware 8091 | HTTP GET `/health` |
| F3 | middleware -> db 3306 | MySQL server greeting |
| F5 | Linux and/or Windows bastion -> web, middleware, db, client, control node 22 | SSH banner exchange |
| F6 | Linux and/or Windows bastion -> DCs 3389 | RDP connection request and negotiation |
| F7 | control node -> web, middleware, db, client 22 | Ansible's own SSH session |
| F8 | control node -> DCs 5986 | Ansible's own WinRM session |
| F9 | dc <-> rodc | `dcdiag /s:<peer>` and `repadmin /showrepl <peer>` on each DC; `repadmin /replicate` from the forest root |
| F10 | web, middleware, db, control node, DCs -> Internet 443 | HTTPS GET (github.com, plus the sites in `vars/flows.yml`) |
| N1 | every host -> portquiz.net:666 | HTTP GET |
| N2 | every host -> neverssl.com:80, httpforever.com:80 | HTTP GET |
| N3 | web, middleware, db -> every other Linux host, incl. the Linux bastion, 22 | SSH banner exchange |
| N4 | web, middleware, db -> DCs and the Windows bastion 3389 | RDP connection request |
| N5 | web -> db 3306 | MySQL server greeting |
| N6 | each bastion -> web 8090, middleware 8091, db 3306, DCs 5986 and 445 | TCP connect |
| N7 | client -> the internet sites 443 | HTTPS GET (Intranet Users are not an F10 source) |

Every negative target has a listener, so a failure to connect is the policy, not a closed port.

Results: `OPEN` connected, `REFUSED` reset, `TIMEOUT` dropped, `ERROR` (DNS, unreachable).
Verdicts: `PASS` matches Table 5, `FAIL` does not, `INFO` a denied flow connected before enforcement.

## Running one playbook

```bash
cd /opt/control-repo
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-exercise.yml
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-internet.yml -e flow_mode=all
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-bastion.yml
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-negative.yml -e flow_enforced=true
```

| Variable | Default | Meaning |
|---|---|---|
| `flow_mode` | `positive` | `positive` allowed only, `negative` denied only, `all` both (internet and bastion playbooks) |
| `flow_enforced` | `false` | `true`: a denied flow that connects fails the run |
| `flow_rounds`, `flow_interval` | 1, 30 | repeat the probes N times, seconds apart |
| `flow_timeout` | 5 | seconds per connect or read |
| `flow_client_internet` | `deny` | `allow` if the client should browse (its probes then run as F10) |
| `flow_validate_certs` | `true` | `false` for ZIA SSL inspection without the Zscaler root CA |
| `flow_ignore_lock` | `false` | run even while a converge holds the run lock |

The Windows bastion script also works by hand on that bastion (RDP to it first):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\flow-bastion.ps1 -SshTargets @{ web='10.193.10.4'; middleware='10.193.10.5'; db='10.193.10.6'; client='10.193.40.4'; ansible_control='10.193.60.4' }
.\flow-bastion.ps1 -SshTargets $t -Mode All -Enforced      # after enforcement
```

## Prerequisites and policy notes

- **Bastions** for F5/F6: `flow-bastion.yml` runs from whichever exist (`bastion_type` linux,
  windows or both). Terraform tags both `Role=bastion`; only the Windows one has `OS=bastion`.
  - **Linux bastion** (`role_bastion` minus `os_bastion`): reached over SSH as `ubuntu` with the
    estate key, nothing to set up. Override detection with `-e flow_linux_bastion=<IP>`.
  - **Windows bastion** (`os_bastion`): Terraform enables WinRM on it (`modules/bastion-windows`,
    `bootstrap_winrm`, default true); an older estate gets it on its next `terraform apply`. Its admin is the account in the
    Key Vault secret.
  - Limit to one with `-e flow_bastion_kinds=linux` (or `windows`).
- **Ansible RG -> Bastion RG** (TCP 22 for the Linux bastion, 5986 for the Windows one) is used by
  `flow-bastion.yml` and is not in Table 5. It appears in the learning data; allow it, or
  `flow-bastion.yml` stops working once ZMS enforces.
- **Do not run negatives while ZMS is learning.** `flow-test.sh learn` never does; running
  `flow-negative.yml` or `-e flow_mode=all|negative` by hand during that window puts denied flows
  into the data the recommendations are built from.
- **Ubuntu packages use TCP 80** (`azure.archive.ubuntu.com`). Denying outbound 80 (N2) also
  breaks `apt` on Ubuntu tiers unless that mirror is allowed.
- If a converge or the repo refresh holds `.git/zms-ansible.lock`, `flow-test.sh` says which and waits
  for it (up to 30 minutes; `FLOW_LOCK_WAIT=0` to stop at once). A playbook run on its own stops instead.

## Files

```
scripts/flow-test.sh             one-command runner (learn | verify)
vars/flows.yml                   sites, negative targets, phase defaults
playbooks/flow-exercise.yml      F1-F3, F7-F10
playbooks/flow-internet.yml      F10 sites, N7
playbooks/flow-bastion.yml       F5, F6, N6 (Linux and Windows bastions)
playbooks/flow-negative.yml      N1-N5
playbooks/flow-preflight.yml     lock check, run ID (imported)
playbooks/flow-report.yml        summary, fail on FAIL (imported)
playbooks/tasks/flow-run.yml     runs the probe engine for the host's OS
playbooks/tasks/flow-record.yml  tags results, appends to the log
playbooks/files/flow-probe.py    Linux probe engine (stdlib, Python 3.6+)
playbooks/files/flow-probe.ps1   Windows probe engine (PowerShell 5.1+)
playbooks/files/flow-bastion.ps1 Windows bastion F5/F6/N6 script (also for manual use)
```

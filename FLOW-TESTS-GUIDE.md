# ZMS Flow Tests: Operator Guide (web-app-adds)

How to generate the **Table 5** network flows in a newly deployed `web-app-adds` lab, so that
Zscaler Microsegmentation (ZMS) learns them, and how to check the policy once it is enforced.

Everything the tests need is set up by `terraform apply`. There is nothing to install or configure.

---

## Quick start

On the Ansible control node (section 3):

```bash
sudo bash /opt/control-repo/scripts/flow-test.sh learn     # while ZMS is learning: allowed flows only
sudo bash /opt/control-repo/scripts/flow-test.sh verify    # after the policy is enforced: allowed + denied
```

Both runs should end with every playbook `ok` and `Failures: none`.

---

## Contents

1. [What is tested](#1-what-is-tested)
2. [Before you start](#2-before-you-start)
3. [Connect to the control node](#3-connect-to-the-control-node)
4. [Check the lab is ready](#4-check-the-lab-is-ready)
5. [Phase 1: learn](#5-phase-1-learn)
6. [Phase 2: verify](#6-phase-2-verify)
7. [Running it with the Azure CLI instead of SSH](#7-running-it-with-the-azure-cli-instead-of-ssh)
8. [Reading the results](#8-reading-the-results)
9. [Options](#9-options)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What is tested

Each probe runs on the flow's source host, so ZMS sees the right source. Probes only open a
connection and read the first reply: nothing signs in and nothing on the hosts is changed.

### Allowed flows (Table 5)

F4 (operators to the bastion) is not tested: it starts outside Azure.

| Flow | Source | Destination | Port | Probe |
|---|---|---|---|---|
| F1 | client | web | TCP 8090 | HTTP GET `/api/tickets` |
| F2 | web | middleware | TCP 8091 | HTTP GET `/health` |
| F3 | middleware | db | TCP 3306 | MySQL greeting |
| F5 | bastion | web, middleware, db, client, control node | TCP 22 | SSH banner |
| F6 | bastion | dc, rodc | TCP 3389 | RDP connection request |
| F7 | control node | web, middleware, db, client | TCP 22 | Ansible's SSH connection |
| F8 | control node | dc, rodc | TCP 5986 | Ansible's WinRM connection |
| F9 | dc and rodc | each other | AD ports | `dcdiag`, `repadmin /showrepl`, `repadmin /replicate` |
| F10 | web, middleware, db, control node, dc, rodc | Internet | TCP 443 | HTTPS to github.com, google.com, zscaler.com, cnn.com, github.com/198sahilaws, ip.zscaler.com |

### Denied flows (sent only by `verify`)

| ID | Source | Destination | Port |
|---|---|---|---|
| N1 | every host except the client | portquiz.net | TCP 666 |
| N2 | every host except the client | neverssl.com, httpforever.com | TCP 80 |
| N3 | web, middleware, db | every other Linux host, including the control node and Linux bastion | TCP 22 |
| N4 | web, middleware, db | dc, rodc, Windows bastion | TCP 3389 |
| N5 | web | db | TCP 3306 |
| N6 | each bastion | web 8090, middleware 8091, db 3306, DCs 5986 and 445 | as listed |

Every denied-flow target has a service listening, so a denied flow that does not connect was
blocked by the policy, not by a closed port.

---

## 2. Before you start

- **The lab is deployed** with `deployments/web-app-adds`, and you have the files Terraform wrote
  there: the SSH key (`.pem`) and `connection-details.txt`.
- **Wait 30-45 minutes after `terraform apply`.** The control node builds the Service Desk app and
  the AD forest (dc, then rodc) in that time. Section 4 shows how to check it has finished.
- **The ZMS agents are installed** and reporting on the hosts.
- **Bastions.** F5 and F6 run from whichever bastion `bastion_type` built (`linux` by default,
  `windows` or `both`). With `bastion_type = "none"`, skip them: `FLOW_SKIP=flow-bastion` (section 9).
- **Before you enforce the policy**, make sure it also allows these flows, which are not in Table 5:

  | Flow | Why |
  |---|---|
  | Ansible RG → Bastion RG, TCP 22 | The control node reaches the Linux bastion to run F5/F6 |
  | Ansible RG → Bastion RG, TCP 5986 | The control node reaches the Windows bastion to run F5/F6 |
  | Ubuntu hosts → `azure.archive.ubuntu.com`, TCP 80 | Ubuntu package updates. A deny on outbound 80 (N2) would otherwise stop `apt` |

  Both management flows show up in the flow data during `learn`.

---

## 3. Connect to the control node

The control node has a private IP only. Connect through the Linux bastion, or skip SSH and use the
Azure CLI (section 7).

Add this to `~/.ssh/config` on your PC (Windows: `C:\Users\<you>\.ssh\config`), using the IPs and the
bastion user from `connection-details.txt`:

```text
Host zms-bastion
    HostName <Linux bastion public IP>
    User <Linux bastion user>
    IdentityFile <full path to the .pem file>

Host zms-control
    HostName <control node private IP>
    User ubuntu
    IdentityFile <full path to the .pem file>
    ProxyJump zms-bastion
```

Then:

```bash
ssh zms-control
```

If OpenSSH refuses the key ("UNPROTECTED PRIVATE KEY FILE"), restrict it:

- Linux and macOS: `chmod 600 <file>.pem`
- Windows PowerShell: `icacls <file>.pem /inheritance:r /grant:r "$($env:USERNAME):R"`

Your public IP must be in `bastion_allowed_cidrs` in `terraform.tfvars`, or the bastion does not
answer.

---

## 4. Check the lab is ready

On the control node:

```bash
tail -n 4 /var/log/ansible/converge-status.log
```

The latest line that says `estate` must contain `result=success` (the `bootstrap` lines are the repo
refresh). If there is no `estate` line yet, the first build has not finished. If it says
`result=partial` (it names the unreachable hosts) or any other result, wait for the next hourly
converge and check again.

Optionally, confirm every host is in the inventory:

```bash
sudo /opt/control-repo/scripts/estate-shell.sh ansible-inventory --graph
```

You should see one host under each of `role_web`, `role_middleware`, `role_db`, `role_client`,
`role_ansible_control`, `role_dc`, `role_rodc` and `role_bastion` (plus `os_bastion` for a Windows
bastion).

You do not need to check whether a converge is running: `flow-test.sh` waits for it (up to 30
minutes) and says so.

---

## 5. Phase 1: learn

Run this while ZMS is learning flows, before you build the policy:

```bash
sudo bash /opt/control-repo/scripts/flow-test.sh learn
```

It sends the allowed flows only (F1-F3, F5-F10), 6 rounds 30 seconds apart, and takes about 15-20
minutes. It runs three playbooks (`flow-exercise`, `flow-internet`, `flow-bastion`) and then prints a
summary.

**Expected:** every playbook `ok`, every verdict `PASS`, `Failures: none`.

Run it as often as you like during the learning window. For a longer burst add `-e flow_rounds=12`.
Then check in ZMS that each Table 5 flow appears, and build and enforce the policy.

> **Do not run `verify` during the learning window.** It sends the denied flows, which would end up in
> the data the policy is built from.

---

## 6. Phase 2: verify

After the policy is enforced:

```bash
sudo bash /opt/control-repo/scripts/flow-test.sh verify
```

It runs the allowed and the denied flows once (adds `flow-negative`), and takes about 5-10 minutes.

**Expected:**

- every F-flow `PASS` with result `OPEN` (`OK` for F9)
- every N-flow `PASS` with result `TIMEOUT`, `REFUSED` or `ERROR`
- `Failures: none`

**If something fails:**

| Failure | Meaning |
|---|---|
| An F-flow `TIMEOUT` or `REFUSED` | The policy blocks a flow Table 5 allows: missing allow rule |
| An N-flow `OPEN` | The policy lets through a flow Table 5 denies: missing or wrong deny |
| Every N-flow `OPEN` | Enforcement is not active yet |
| F7 or F8 `UNREACHABLE` | The control node cannot reach that host: policy, or the host is down |

Run `verify` again after each policy change.

---

## 7. Running it with the Azure CLI instead of SSH

`az vm run-command invoke` runs the same commands on the control node through the Azure VM agent. No
SSH or bastion is needed.

You need:

- the Azure CLI, signed in (below)
- a role that can run commands on VMs in the lab's resource group, for example **Virtual Machine
  Contributor** (or Contributor)
- the resource group name, from the top of `connection-details.txt`

### Sign in with your user account

```bash
az login                                            # opens a browser to sign in
az login --use-device-code                          # instead, on a machine without a browser
az login --tenant <tenant ID or domain>             # if your account is in more than one tenant

az account set --subscription "<subscription ID or name>"   # if you have more than one subscription
az account show --query "{user:user.name, type:user.type, subscription:name}" -o table
```

The last command should show your account, type `user`, and the lab's subscription. These commands are
the same in bash and PowerShell.

### Sign in as a service principal

With a client secret, in bash:

```bash
az login --service-principal --username "<appId>" --password "<client secret>" --tenant "<tenant ID>"
az account set --subscription "<subscription ID or name>"
az account show --query "{user:user.name, type:user.type, subscription:name}" -o table
```

In PowerShell, the same login, or with the `ARM_*` variables if Terraform already uses this service
principal:

```powershell
az login --service-principal --username "<appId>" --password "<client secret>" --tenant "<tenant ID>"
# or
az login --service-principal --username $env:ARM_CLIENT_ID --password $env:ARM_CLIENT_SECRET --tenant $env:ARM_TENANT_ID

az account set --subscription "<subscription ID or name>"
az account show --query "{user:user.name, type:user.type, subscription:name}" -o table
```

`az account show` should show the appId and type `servicePrincipal`.

- **With a certificate instead of a secret:** use `--certificate <path to a PEM file holding the
  certificate and private key>` instead of `--password` (Azure CLI versions before 2.66 take the PEM
  path with `--password`).
- **The service principal needs the same role** (Virtual Machine Contributor or Contributor) on the
  lab's resource group.
- **`az login` replaces whoever is signed in.** To keep your own sign-in as well, give the service
  principal its own CLI profile before signing in, in the same window:
  `export AZURE_CONFIG_DIR=~/.azure-sp` (bash) or `$env:AZURE_CONFIG_DIR = "$HOME\.azure-sp"`
  (PowerShell). Run `az logout` when you have finished.
- Do not save the client secret in scripts or files you share.

### Run the tests: bash

```bash
RG=<resource-group>
VM=$(az vm list -g "$RG" --query "[?tags.Role=='ansible-control'].name" -o tsv)

# Ready? (the latest estate line must say result=success)
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts 'tail -n 4 /var/log/ansible/converge-status.log' --query "value[0].message" -o tsv

# Learn (during the learning window)
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts 'bash /opt/control-repo/scripts/flow-test.sh learn' --query "value[0].message" -o tsv

# Verify (after enforcement)
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts 'bash /opt/control-repo/scripts/flow-test.sh verify' --query "value[0].message" -o tsv

# Failures from the last run
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts 'L=/var/log/ansible/flow-exercise.log; R=$(tail -n 1 $L | sed -n "s/.* run=\([^ ]*\) .*/\1/p"); echo "run $R"; grep " run=$R " $L | grep " verdict=FAIL " || echo "no failures"' \
  --query "value[0].message" -o tsv
```

### Run the tests: Windows PowerShell

The last command contains `$`, `|` and double quotes, which `az` on Windows can alter, so it is sent
from a file.

```powershell
$RG = "<resource-group>"
$VM = az vm list -g $RG --query "[?tags.Role=='ansible-control'].name" -o tsv

# Ready? (the latest estate line must say result=success)
az vm run-command invoke -g $RG -n $VM --command-id RunShellScript `
  --scripts "tail -n 4 /var/log/ansible/converge-status.log" --query "value[0].message" -o tsv

# Learn (during the learning window)
az vm run-command invoke -g $RG -n $VM --command-id RunShellScript `
  --scripts "bash /opt/control-repo/scripts/flow-test.sh learn" --query "value[0].message" -o tsv

# Verify (after enforcement)
az vm run-command invoke -g $RG -n $VM --command-id RunShellScript `
  --scripts "bash /opt/control-repo/scripts/flow-test.sh verify" --query "value[0].message" -o tsv

# Failures from the last run
$cmd = 'L=/var/log/ansible/flow-exercise.log; R=$(tail -n 1 $L | sed -n "s/.* run=\([^ ]*\) .*/\1/p"); echo "run $R"; grep " run=$R " $L | grep " verdict=FAIL " || echo "no failures"'
[IO.File]::WriteAllText("$PWD\last-failures.sh", $cmd + "`n")
az vm run-command invoke -g $RG -n $VM --command-id RunShellScript `
  --scripts "@last-failures.sh" --query "value[0].message" -o tsv
```

### Notes

- Each command waits until the run finishes (learn takes 15-20 minutes), then prints the **last 4 KB**
  of output, which is the summary. The "failures" command fetches the detail from the log.
- Run one command at a time: the VM accepts one Run Command at a time.
- Commands run as root, so there is no `sudo`. Options go after `learn` or `verify`, as in section 9.
- If `$VM` comes back empty, check the resource group name and that `az account show` shows the lab's
  subscription.

---

## 8. Reading the results

### The summary

```text
==================== summary: phase=verify run=20261126T101500 ====================
flow-exercise  ok
flow-internet  ok
flow-bastion   ok
flow-negative  FAILED (rc=2)

Probes by flow and verdict:
  F1   PASS  1
  ...
  N5   FAIL  1

Failures:
<the log line of every failed probe>
```

The script exits 0 if every playbook succeeded, 1 otherwise.

### The log

Every probe is one line in `/var/log/ansible/flow-exercise.log` on the control node:

```text
2026-11-26T10:15:42+05:30 run=20261126T101500 playbook=flow-exercise verdict=PASS flow=F1 src=client/10.193.40.4 dst=web 10.193.10.4:8090 expect=allow result=OPEN ms=12 round=1 detail="HTTP/1.1 200 OK"
```

| Result | Meaning |
|---|---|
| `OPEN` | The connection completed |
| `REFUSED` | The destination answered with a reset |
| `TIMEOUT` | No answer: dropped silently |
| `ERROR` | DNS failure, network unreachable or another socket error |
| `UNREACHABLE` | F7/F8 only: Ansible could not connect |
| `OK` / `FAILED` | F9 only: the AD operation succeeded or failed |

| Verdict | Meaning |
|---|---|
| `PASS` | Matches Table 5 |
| `FAIL` | Does not match: an allowed flow failed, or (in `verify`) a denied flow connected |

`OPEN` is what matters to ZMS. The application's answer is in `detail`, so F1 with
`detail="HTTP/1.1 503 ..."` is still a `PASS`: the flow worked, but the Service Desk behind it has a
problem.

Useful commands on the control node:

```bash
LOG=/var/log/ansible/flow-exercise.log
grep ' run=<run id> ' $LOG                          # one run
grep ' run=<run id> ' $LOG | grep ' verdict=FAIL '  # its failures
tail -f $LOG                                        # watch a run from a second session
```

---

## 9. Options

Add these after `learn` or `verify`, for example
`sudo bash /opt/control-repo/scripts/flow-test.sh learn -e flow_rounds=12`.

| Option | Default | Effect |
|---|---|---|
| `-e flow_rounds=N` | 6 in learn, 1 in verify | Repeat each probe set N times, 30 s apart |
| `-e flow_timeout=N` | 5 | Seconds to wait for each connection (try 10 if internet probes time out) |
| `-e flow_bastion_kinds=linux` | both | Run F5/F6 from one bastion only (`linux` or `windows`) |
| `-e flow_validate_certs=false` | true | Skip certificate checks, for SSL inspection without the inspecting CA on the hosts |

Settings that go **before** `bash`:

| Setting | Effect |
|---|---|
| `sudo FLOW_SKIP=flow-bastion bash ...` | Skip a playbook (here the bastion tests, e.g. with no bastion) |
| `sudo FLOW_LOCK_WAIT=0 bash ...` | Do not wait for a running converge; stop at once instead |

The list of internet sites is `flow_internet_sites` in `vars/flows.yml` in the Ansible repo.

---

## 10. Troubleshooting

| Symptom | What to do |
|---|---|
| `The estate converge ... is running` / `Waiting up to 30 min` | Nothing: the tests start when the converge finishes |
| `Still held after 30 min` | The converge is stuck: `journalctl -u ansible-estate.service -n 50` |
| `Run with sudo: estate.env is readable by root only.` | Add `sudo` |
| `No bastion found, so F5/F6 cannot run` | `bastion_type = "none"`: run with `FLOW_SKIP=flow-bastion` |
| Windows bastion `UNREACHABLE` before enforcement | Its WinRM setup failed during the deploy. Check it from your PC: `az vm run-command show -g <rg> --vm-name <windows bastion vm> --run-command-name winrm-bootstrap --instance-view`. Meanwhile use `-e flow_bastion_kinds=linux` |
| A bastion `UNREACHABLE` after enforcement | Allow Ansible RG → Bastion RG on TCP 22 (Linux) or TCP 5986 (Windows) |
| F7 or F8 `UNREACHABLE` | The host is stopped (`az vm list -d -g <rg> -o table`), or the policy blocks F7/F8 |
| F9 `FAILED: this host is not a domain controller` | The AD build has not finished or failed on that DC. Check `converge-status.log`; to see the error, run `cd /opt/control-repo && sudo scripts/estate-shell.sh ansible-playbook playbooks/windows-rodc.yml` |
| F9 `FAILED` with `Failed to become user`, `Access is denied` or `LDAP error 49` | The domain admin sign-in was refused: check `windows_admin_password` in `secrets.auto.tfvars` matches the account |
| F9 `FAILED` with RPC or `server is unavailable` | The other DC is down, or (after enforcement) the policy blocks AD traffic between dc and rodc |
| F1 `PASS` with `detail="HTTP/1.1 503 ..."` | The flow works; the app behind it does not. From `/opt/control-repo`, `sudo scripts/estate-shell.sh ansible-playbook playbooks/servicedesk-verify.yml` names the broken tier |
| Internet probe `OPEN` with `certificate verify failed` | Traffic is SSL-inspected. Install the inspecting CA on the hosts, or use `-e flow_validate_certs=false` |
| Internet probe `TIMEOUT` on one site now and then | A slow site. Rerun, or use `-e flow_timeout=10` |

To see the full Ansible output for one part, run its playbook on its own with `-v`:

```bash
cd /opt/control-repo
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-exercise.yml -v   # or flow-internet, flow-bastion, flow-negative
```

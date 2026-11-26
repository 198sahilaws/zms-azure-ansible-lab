# ZMS Flow Tests: Operator Guide (web-app-adds)

This guide is for someone who can sign in to the lab's Azure estate and wants to generate and check
the network flows in **Table 5** of the lab, so that Zscaler Microsegmentation (ZMS) sees them in
its flow data and, later, so that the enforced policy can be verified.

It covers the `web-app-adds` deployment. A quick reference is in `FLOW-TESTS.md`; this guide is the
full procedure.

---

## Contents

1. [What the tests do](#1-what-the-tests-do)
2. [How they work](#2-how-they-work)
3. [Before you start](#3-before-you-start)
4. [Connect to the Ansible control node](#4-connect-to-the-ansible-control-node)
5. [Check the estate is ready](#5-check-the-estate-is-ready)
6. [Phase 1: learn (generate the allowed flows)](#6-phase-1-learn-generate-the-allowed-flows)
7. [Phase 2: verify (after the policy is enforced)](#7-phase-2-verify-after-the-policy-is-enforced)
7A. [The whole workflow with the Azure CLI](#7a-the-whole-workflow-with-the-azure-cli)
8. [Reading the results](#8-reading-the-results)
9. [Running one playbook, and options](#9-running-one-playbook-and-options)
10. [Running the bastion probes by hand](#10-running-the-bastion-probes-by-hand)
11. [Troubleshooting](#11-troubleshooting)
12. [What the tests change, and what they do not](#12-what-the-tests-change-and-what-they-do-not)
13. [Reference](#13-reference)

---

## 1. What the tests do

### Allowed flows (Table 5)

F4 (operators to the bastion) is not tested: it starts outside Azure.

| Flow | Source | Destination | Port | How the test creates it |
|---|---|---|---|---|
| F1 | client | web | TCP 8090 | HTTP GET `/api/tickets`. The web tier passes it on, so it also produces a real F2 and F3 |
| F2 | web | middleware | TCP 8091 | HTTP GET `/health` |
| F3 | middleware | db | TCP 3306 | Reads the MySQL/MariaDB greeting (no login) |
| F5 | bastion | web, middleware, db, client, control node | TCP 22 | Reads the SSH banner and sends a client version string (no login) |
| F6 | bastion | dc, rodc | TCP 3389 | Sends an RDP connection request and reads the reply (no login) |
| F7 | control node | web, middleware, db, client | TCP 22 | Ansible's own SSH connection to each host |
| F8 | control node | dc, rodc | TCP 5986 | Ansible's own WinRM connection to each DC |
| F9 | dc and rodc | each other | AD ports | `dcdiag` and `repadmin /showrepl` on each DC against the other, and `repadmin /replicate` from the dc |
| F10 | web, middleware, db, control node, dc, rodc | Internet | TCP 443 | HTTPS GET to `https://github.com/` and to five sites (section 9) |

### Denied flows (checked only after enforcement)

| ID | Source | Destination | Port | Probe |
|---|---|---|---|---|
| N1 | every host | portquiz.net | TCP 666 | HTTP GET |
| N2 | every host | neverssl.com, httpforever.com | TCP 80 | HTTP GET |
| N3 | web, middleware, db | every other Linux host, including the control node and the Linux bastion | TCP 22 | SSH banner |
| N4 | web, middleware, db | dc, rodc, Windows bastion | TCP 3389 | RDP connection request |
| N5 | web | db | TCP 3306 | MySQL greeting (skips the middleware tier) |
| N6 | each bastion | web 8090, middleware 8091, db 3306, DCs 5986 and 445 | as listed | TCP connect |
| N7 | client | the five internet sites | TCP 443 | HTTPS GET (Table 5 does not list Intranet Users as an F10 source) |

Every denied-flow target has a service listening on that port. So if a denied flow cannot
connect, the policy blocked it; the port was not simply closed.

---

## 2. How they work

- **Everything runs from the Ansible control node**, and only when you start it. Nothing is
  scheduled, and the hourly converge never runs these playbooks.
- **Each probe runs on the flow's source host.** For F1, the control node connects to the client
  over SSH (that connection is an F7 flow), and the client opens the connection to the web tier.
  ZMS therefore sees the client as the source of F1, as it would in real use.
- **Two phases, one command each:**

  | Command | Runs | Rounds | A denied flow that connects |
  |---|---|---|---|
  | `flow-test.sh learn` | Allowed flows only (F1-F3, F5-F10, internet sites) | 6, 30 s apart | Not tested |
  | `flow-test.sh verify` | Allowed and denied flows (adds N1-N7) | 1 | Fails the run |

  `learn` never sends a denied flow, so the data ZMS learns from contains only flows you intend
  to allow.

- **The script runs four playbooks**, each as its own `ansible-playbook` process:
  `flow-exercise.yml`, `flow-internet.yml`, `flow-bastion.yml`, and in `verify` also
  `flow-negative.yml`. It then prints one summary.

---

## 3. Before you start

### 3.1 What you need

| Item | Where it comes from |
|---|---|
| The estate's SSH key (`.pem`) | Written by `terraform apply` into `deployments/web-app-adds/` |
| `connection-details.txt` | Same folder. Lists the bastion's public IP and every host's private IP |
| Your public IP in `bastion_allowed_cidrs` | `deployments/web-app-adds/terraform.tfvars`. Only these addresses can reach the bastion |
| An SSH client | OpenSSH on Linux, macOS or Windows 10/11 |
| Azure CLI, signed in (optional) | For the manual WinRM step in section 3.4 and the Azure CLI workflow in section 7A |

### 3.2 The code must be on the control node

The control node runs whatever is on the repository's branch. Its repo refresh (every 30 minutes by
default) resets `/opt/control-repo` to `origin/<branch>`. So:

1. Commit and push the flow-test files to the branch the control node cloned (normally `main`).
2. Either wait up to 30 minutes, or refresh now on the control node (section 4):

   ```bash
   sudo systemctl start ansible-bootstrap.service
   ```

   This waits until the refresh finishes, usually a minute or two. If a converge is running at that
   moment, the refresh skips itself; run it again once the converge has finished.

3. Confirm the files arrived:

   ```bash
   ls /opt/control-repo/scripts/flow-test.sh /opt/control-repo/playbooks/flow-*.yml
   git -C /opt/control-repo log -1 --oneline
   ```

   The refresh also makes `scripts/*.sh` executable, so `flow-test.sh` runs directly.

### 3.3 Bastions (for F5 and F6)

`flow-bastion.yml` runs from whichever bastion exists. `bastion_type` in `terraform.tfvars`
chooses which are built; the `web-app-adds` default is `"linux"`.

| `bastion_type` | F5 and F6 from | Extra setup |
|---|---|---|
| `"linux"` (default) | Linux bastion | None |
| `"windows"` | Windows bastion | Section 3.4 |
| `"both"` | Both bastions | Section 3.4 |
| `"none"` | Not possible | Skip the bastion tests (section 9) |

How the playbook finds them: Terraform tags both bastions `Role=bastion`, and only the Windows one
also `OS=bastion`. So the Windows bastion is the inventory group `os_bastion`, and the Linux bastion
is in `role_bastion` but not in `os_bastion`. It is reached over SSH as `ubuntu` with the same estate
key the control node already holds.

### 3.4 Windows bastion only: WinRM

The control node needs WinRM (TCP 5986) on the Windows bastion to run the probe script there.
Terraform sets it up: `modules/bastion-windows` runs the same `winrm-bootstrap.ps1` the Windows
workloads use, through a Terraform-managed Run Command (`bootstrap_winrm`, default `true`). It is
used only by the flow tests; the converge never targets the bastion.

**An estate deployed before this change** gets it on its next apply, from the deployment folder:

```bash
terraform plan -out tfplan     # expect 1 to add, 0 to change, 0 to destroy:
                               #   module.estate.module.bastion_windows[0].azurerm_virtual_machine_run_command.winrm_bootstrap[0]
terraform apply tfplan
```

Nothing is rebuilt, and re-running the script on a bastion that already has a listener is harmless (it
reuses the certificate). If the plan shows anything else, stop and check it.

**Without Terraform**, the same script can be run by hand (Azure CLI signed in, from the Terraform
repo root):

```bash
# Find the Windows bastion's VM name (it contains "bastion-win")
az vm list -g <resource-group> --query "[].name" -o tsv

az vm run-command invoke -g <resource-group> -n <windows-bastion-vm-name> \
  --command-id RunPowerShellScript \
  --scripts @modules/compute-windows/winrm-bootstrap.ps1 \
  --parameters "AdminUser=<windows_admin_username>"
```

The leading `@` matters: it makes the Azure CLI send the file's contents. Without it, the path itself
is sent and the bastion fails with "is not recognized as the name of a cmdlet".

- `<resource-group>` is on the first lines of `connection-details.txt`.
- `<windows_admin_username>` is the `windows_admin_username` setting (default `zmsadmin`).
- The output ends with `PASS: HTTPS listener on 5986`. The script's log is
  `C:\Windows\Temp\winrm-bootstrap.log` on the bastion.

Check from the control node (section 4) that the bastion now answers on 5986:

```bash
nc -zv -w 5 <windows-bastion-private-ip> 5986      # "succeeded"
```

The playbook signs in with the account in the estate's Key Vault secret (`winrm_username` /
`winrm_password`), which is the Windows admin account and password from `secrets.auto.tfvars`.

To keep the Windows bastion fully unmanaged instead, set `bootstrap_winrm = false` on the
`bastion-windows` module and run F5/F6 from the Linux bastion (`-e flow_bastion_kinds=linux`).

### 3.5 ZMS and the policy

- The ZMS agents must be installed and reporting on the hosts whose flows you want to see.
- **Two management flows are not in Table 5** but are used by `flow-bastion.yml`:
  - Ansible RG to Bastion RG, TCP 22 (Linux bastion)
  - Ansible RG to Bastion RG, TCP 5986 (Windows bastion)

  They appear in the flow data during `learn`. Allow them in the policy, or `flow-bastion.yml`
  cannot reach the bastions once ZMS enforces.
- **Ubuntu package mirrors use TCP 80** (`azure.archive.ubuntu.com`). A policy that denies outbound
  80 (which N2 tests) also stops `apt` on Ubuntu hosts unless that mirror is allowed.

---

## 4. Connect to the Ansible control node

The control node has only a private IP. Use one of the three options below.

### 4.1 SSH through the Linux bastion (recommended)

Add this to `~/.ssh/config` on your PC (on Windows: `C:\Users\<you>\.ssh\config`), with the values
from `connection-details.txt`:

```text
Host zms-bastion
    HostName <Linux bastion public IP>
    User ubuntu
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

The key file must be readable only by you, or OpenSSH refuses it ("UNPROTECTED PRIVATE KEY FILE"):

- Linux and macOS: `chmod 600 <file>.pem`
- Windows (PowerShell): `icacls <file>.pem /inheritance:r /grant:r "$($env:USERNAME):R"`

### 4.2 From the Windows bastion

RDP to the Windows bastion (`mstsc /v:<Windows bastion public IP>`, user `zmsadmin`), copy the
`.pem` file to it, restrict its permissions as in section 4.1, and from PowerShell:

```powershell
ssh -i C:\path\to\key.pem ubuntu@<control node private IP>
```

This needs the Windows OpenSSH client. Check with
`Get-WindowsCapability -Online -Name OpenSSH.Client*`; if its `State` is not `Installed`, add it with
`Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0` (downloads from Windows Update).

### 4.3 Without SSH: Azure CLI

You can run the whole procedure from your PC with the Azure CLI and never sign in to a VM. See
[section 7A](#7a-the-whole-workflow-with-the-azure-cli).

Sections 5 to 7 assume an SSH session as `ubuntu` on the control node. Section 7A gives the same
steps with the Azure CLI.

---

## 5. Check the estate is ready

Run these on the control node before the first test. Allow 30-45 minutes after `terraform apply`
for the Service Desk and the AD forest to be built.

**1. The last converge succeeded**

```bash
tail -n 3 /var/log/ansible/converge-status.log
```

The latest line should contain `result=success`. A `result=partial` line names hosts that could not
be reached; their flows will fail too.

**2. Every host is in the inventory**

```bash
sudo /opt/control-repo/scripts/estate-shell.sh ansible-inventory --graph
```

Look for one host under each of: `role_web`, `role_middleware`, `role_db`, `role_client`,
`role_ansible_control`, `role_dc`, `role_rodc`, and `role_bastion` (plus `os_bastion` for a Windows
bastion). Each host appears as its private IP.

**3. No converge is running**

```bash
systemctl is-active ansible-estate.service ansible-bootstrap.service
```

Both should print `inactive`. If one says `activating`, a converge or repo refresh is running.
You do not have to wait yourself: `flow-test.sh` says which one is running, waits for it to finish
(up to 30 minutes), and then starts the tests. A playbook run on its own (section 9.1) does not wait;
it stops with a message instead.

**4. Optional: the Service Desk works end to end**

```bash
sudo /opt/control-repo/scripts/estate-shell.sh ansible-playbook playbooks/servicedesk-verify.yml
```

---

## 6. Phase 1: learn (generate the allowed flows)

Run this while ZMS is learning flows, before you build the policy.

```bash
sudo /opt/control-repo/scripts/flow-test.sh learn
```

What happens:

1. The script prints `Flow tests: phase=learn run=<run id>`. The run ID tags every result. If a
   converge is running, it says so and waits for it to finish first.
2. `flow-exercise.yml`: F7 (SSH to each Linux host), then F1, F2, F3 and F10 from the Linux hosts;
   then F8 (WinRM to each DC), F9 (dcdiag and repadmin on each DC) and F10 from the DCs.
3. `flow-internet.yml`: HTTPS to the five sites from web, middleware, db, the control node and the
   DCs. The client is skipped in `learn` (its HTTPS is N7, a denied flow).
4. `flow-bastion.yml`: F5 and F6 from each bastion found.
5. A summary of every playbook and the number of probes per flow and verdict.

Each probe set runs 6 times, 30 seconds apart, so the flows keep appearing for a few minutes. Plan
for roughly 15-20 minutes for the whole run; most of it is the gaps between rounds, and the F9
`dcdiag` runs take up to a minute or two each.

**Expected result:** every playbook `ok`, every verdict `PASS`, `Failures: none`, exit code 0.

Run `learn` as many times as you like during the learning window. For a longer burst:

```bash
sudo /opt/control-repo/scripts/flow-test.sh learn -e flow_rounds=12
```

Then check in ZMS that each Table 5 flow appears with the expected source and destination, and
build and enforce the policy.

> **Do not run `verify`, `flow-negative.yml`, or any playbook with `-e flow_mode=all` or
> `-e flow_mode=negative` during the learning window.** They send denied flows, which would end up
> in the same data the policy is built from.

---

## 7. Phase 2: verify (after the policy is enforced)

```bash
sudo /opt/control-repo/scripts/flow-test.sh verify
```

This runs all four playbooks once. Allowed flows must connect, and denied flows must not. Plan for
roughly 5-10 minutes.

**Expected result once enforcement is right:**

- every allowed flow (F1-F3, F5-F10): `PASS` with result `OPEN` (`OK` for F9)
- every denied flow (N1-N7): `PASS` with result `TIMEOUT`, `REFUSED` or `ERROR`
- `Failures: none`, exit code 0

**How to read failures in `verify`:**

| Failure | Meaning |
|---|---|
| An F-flow with `TIMEOUT` or `REFUSED` | The policy blocks a flow Table 5 allows. Missing allow rule |
| An N-flow with `OPEN` | The policy lets through a flow Table 5 denies. Missing or wrong deny |
| Every N-flow `OPEN` | Enforcement is not active yet: everything still connects |
| F7 or F8 `UNREACHABLE` | The control node cannot reach that host. Either policy, or the host is down |
| F9 `FAILED` | See section 11 |

Run `verify` again after each policy change.

After enforcement it is also worth starting a normal converge and checking that it still succeeds:
it exercises F7, F8, F10 and the package repositories in real use.

```bash
sudo systemctl start --no-block ansible-estate.service
# wait for it to finish, then:
tail -n 3 /var/log/ansible/converge-status.log
```

---

## 7A. The whole workflow with the Azure CLI

`az vm run-command invoke` runs a command on the control node through the Azure VM agent. No SSH,
bastion or VPN is needed, and it adds no east-west flows to the ZMS data. The same checks and runs as
sections 5 to 7, from your PC.

### 7A.1 What you need

- The Azure CLI, signed in: `az login` (and `az account set --subscription <id>` if you have several
  subscriptions).
- Permission to run commands on VMs in the lab's resource group, for example the
  **Virtual Machine Contributor** role.
- The resource group name, from the first lines of `connection-details.txt`.
- A bash shell: Linux, macOS, WSL, or Azure Cloud Shell. The commands below use bash quoting;
  Windows PowerShell quotes differently.

### 7A.2 Steps

**1. Set the resource group and find the control node**

```bash
RG=<resource-group>
VM=$(az vm list -g "$RG" --query "[?tags.Role=='ansible-control'].name" -o tsv)
echo "$VM"
```

This prints one VM name. If it prints nothing, check `RG` and that you are in the right subscription.

**2. Check the estate is ready**

```bash
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts 'tail -n 1 /var/log/ansible/converge-status.log; systemctl is-active ansible-estate.service ansible-bootstrap.service' \
  --query "value[0].message" -o tsv
```

Expected: a line containing `result=success`, then `inactive` twice.

**3. Positive flows: during the learning window**

```bash
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts '/opt/control-repo/scripts/flow-test.sh learn' \
  --query "value[0].message" -o tsv
```

The command waits until the run finishes, roughly 15-20 minutes. Expected: every playbook `ok` and
`Failures: none`.

**4. Negative flows: after the policy is enforced**

```bash
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts '/opt/control-repo/scripts/flow-test.sh verify' \
  --query "value[0].message" -o tsv
```

Expected: every playbook `ok` and `Failures: none` (section 7 explains the failures).

**5. Failures from the last run**

```bash
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts 'L=/var/log/ansible/flow-exercise.log; R=$(tail -n 1 $L | sed -n "s/.* run=\([^ ]*\) .*/\1/p"); echo "run $R"; grep " run=$R " $L | grep " verdict=FAIL " || echo "no failures"' \
  --query "value[0].message" -o tsv
```

To see every probe of the last run instead, remove `| grep " verdict=FAIL "`.

### 7A.3 Notes

- **Only the end of the output comes back.** Run Command returns the last 4,096 bytes of output.
  That is normally the summary at the end of a run; step 5 retrieves the details from the log.
- **It runs as root**, so no `sudo`. `flow-test.sh` still runs Ansible as `ubuntu` through
  `estate-shell.sh`, exactly as in an SSH session.
- **One Run Command at a time per VM.** Start the next only after the previous one has returned.
- **A running converge makes the command take longer**: `flow-test.sh` waits for it (up to 30
  minutes) before testing. Add `FLOW_LOCK_WAIT=0` in front of the script path to fail at once instead:
  `--scripts 'FLOW_LOCK_WAIT=0 /opt/control-repo/scripts/flow-test.sh learn'`.
- **Options** go after `learn` or `verify`, as in section 9. For example, without a working Windows
  bastion:

  ```bash
  az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
    --scripts '/opt/control-repo/scripts/flow-test.sh learn -e flow_bastion_kinds=linux' \
    --query "value[0].message" -o tsv
  ```

- **Refreshing the code** on the control node (section 3.2) also works this way:
  `--scripts 'systemctl start ansible-bootstrap.service; git -C /opt/control-repo log -1 --oneline'`.

---

## 8. Reading the results

### 8.1 The summary

At the end, `flow-test.sh` prints:

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
<the full log line of every failed probe>
```

The exit code is 0 if every playbook succeeded, 1 otherwise.

### 8.2 The log

Every probe is one line in `/var/log/ansible/flow-exercise.log`:

```text
2026-11-26T10:15:42+05:30 run=20261126T101500 playbook=flow-exercise verdict=PASS flow=F1 src=client/10.193.40.4 dst=web 10.193.10.4:8090 expect=allow result=OPEN ms=12 round=1 detail="HTTP/1.1 200 OK"
```

| Field | Meaning |
|---|---|
| timestamp | When the probe ran, in the source host's time zone |
| `run` | The run ID printed at the start |
| `playbook` | Which playbook produced the line |
| `verdict` | `PASS`, `FAIL` or `INFO` (below) |
| `flow` | F1-F10 or N1-N7 |
| `src` | Source role and private IP |
| `dst` | Destination name, IP and port. `AD` for F9 |
| `expect` | `allow` or `deny`, from Table 5 |
| `result` | What happened on the network (below) |
| `ms` | Time the probe took. 0 for F7, F8 and F9, which are not timed |
| `round` | Which round of a multi-round run |
| `detail` | Protocol detail: HTTP status, SSH banner, MySQL version, RDP security protocol, TLS version, or the error |

**Result:**

| Result | Meaning |
|---|---|
| `OPEN` | The connection completed |
| `REFUSED` | Something answered with a reset |
| `TIMEOUT` | No answer: the connection was dropped silently |
| `ERROR` | Name resolution failed, network unreachable, or another socket error |
| `UNREACHABLE` | F7/F8 only: Ansible could not connect |
| `OK` / `FAILED` | F9 only: the AD operation succeeded or failed |

**Verdict:**

| Verdict | Meaning |
|---|---|
| `PASS` | Matches Table 5: an allowed flow connected, or a denied flow did not |
| `FAIL` | Does not match: an allowed flow failed, or (in `verify`) a denied flow connected |
| `INFO` | A denied flow connected before enforcement. Only when negatives run with enforcement off |

`OPEN` means the TCP connection was made, which is what ZMS sees as a flow. The application answer is
in `detail`: for example F1 with `detail="HTTP/1.1 503 ..."` is still `PASS` because the network flow
worked, but the Service Desk behind it has a problem.

### 8.3 Useful commands

```bash
LOG=/var/log/ansible/flow-exercise.log
grep ' run=<run id> ' $LOG                         # one run
grep ' run=<run id> ' $LOG | grep ' verdict=FAIL ' # its failures
grep ' flow=F9 ' $LOG | tail                       # recent F9 results
tail -f $LOG                                       # watch a run live from a second session
```

---

## 9. Running one playbook, and options

### 9.1 One playbook at a time

```bash
cd /opt/control-repo
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-exercise.yml    # F1-F3, F7-F10
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-internet.yml    # F10 sites (+ N7 with flow_mode=all)
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-bastion.yml     # F5, F6 (+ N6 with flow_mode=all)
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-negative.yml    # N1-N5
```

Always go through `estate-shell.sh`: it loads the estate settings that the inventory needs.
A playbook run on its own defaults to allowed flows only, 1 round, enforcement off.
`flow-negative.yml` on its own reports denied flows that connect as `INFO`; add
`-e flow_enforced=true` to make them fail.

### 9.2 Options

Pass with `-e name=value`, either to `flow-test.sh` (applies to every playbook) or to one
`ansible-playbook` command.

| Variable | Default | Effect |
|---|---|---|
| `flow_rounds` | 6 in `learn`, otherwise 1 | Repeat each probe set this many times |
| `flow_interval` | 30 | Seconds between rounds |
| `flow_timeout` | 5 | Seconds to wait for each connection or reply |
| `flow_mode` | `positive`; `all` in `verify` | `positive` allowed flows, `negative` denied flows, `all` both. Used by the internet and bastion playbooks |
| `flow_enforced` | `true` in `verify`, otherwise `false` | `true`: a denied flow that connects is a `FAIL` |
| `flow_client_internet` | `deny` | `allow` if the client should browse: its HTTPS then runs as F10 in every phase |
| `flow_validate_certs` | `true` | `false` skips certificate checks (e.g. Zscaler Internet Access SSL inspection without the Zscaler root CA on the hosts) |
| `flow_bastion_kinds` | both | `linux` or `windows`: run F5/F6 from one bastion only |
| `flow_linux_bastion` | auto | Private IP of the Linux bastion, if detection picks the wrong host |
| `flow_ignore_lock` | `false` | Run even while a converge holds the run lock (expect false failures) |

The internet sites, the denied-flow targets and the bastion denied ports are in `vars/flows.yml`:

```yaml
flow_internet_sites:
  - https://www.google.com/
  - https://www.zscaler.com/
  - https://cnn.com/
  - https://github.com/198sahilaws/
  - https://ip.zscaler.com/
```

Redirects are reported, not followed: `cnn.com` shows `301 ... -> https://www.cnn.com/`.
For `ip.zscaler.com` the detail also says whether the request went through Zscaler and which public
IP it left from (normally the NAT Gateway's IP).

### 9.3 Examples

```bash
# Skip the bastion playbook (no bastion, or WinRM not set up yet)
sudo FLOW_SKIP=flow-bastion /opt/control-repo/scripts/flow-test.sh learn

# Do not wait for a running converge: stop at once instead (default: wait up to 1800 s)
sudo FLOW_LOCK_WAIT=0 /opt/control-repo/scripts/flow-test.sh learn

# F5/F6 from the Linux bastion only
sudo /opt/control-repo/scripts/flow-test.sh learn -e flow_bastion_kinds=linux

# Only F9 (domain controllers), one round
cd /opt/control-repo
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-exercise.yml --limit 'localhost,role_dc:role_rodc'

# The client's internet access, before enforcement (sends N7)
sudo scripts/estate-shell.sh ansible-playbook playbooks/flow-internet.yml -e flow_mode=negative --limit 'localhost,role_client'
```

When you use `--limit`, always include `localhost`. The lock check, the run ID and the summary run
on `localhost`; without it they are skipped silently, results are logged with an empty `run=`, and
a failed probe no longer fails the playbook.

---

## 10. Running the bastion probes by hand

### 10.1 Windows bastion

`playbooks/files/flow-bastion.ps1` also runs directly on the Windows bastion, with no WinRM and no
control node. It needs no modules and works in Windows PowerShell 5.1.

1. RDP to the Windows bastion and copy the file to it.
2. In a PowerShell window, with the IPs from `connection-details.txt`:

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   $t = @{ web='<web IP>'; middleware='<middleware IP>'; db='<db IP>'; client='<client IP>'; ansible_control='<control node IP>' }
   .\flow-bastion.ps1 -SshTargets $t                              # F5, plus F6 to the DCs
   .\flow-bastion.ps1 -SshTargets $t -Rounds 6 -IntervalSeconds 30 # a learning burst
   .\flow-bastion.ps1 -SshTargets $t -Mode All -Enforced          # after enforcement
   ```

Run it inside a PowerShell session as shown. `powershell -File` cannot pass the hashtable.

The DC targets default to `10.193.50.10` (dc) and `10.193.50.12` (rodc), which are correct for the
default `web-app-adds` network. If you changed `vnet_cidr` or `infra_subnet_cidr`, pass them:
`-RdpTargets @{ dc='<dc IP>'; rodc='<rodc IP>' }`.

The script prints a table and a summary, and exits with code 1 if any probe failed. Its results
are not written to the control node's log.

### 10.2 Linux bastion

There is no separate script for the Linux bastion. Use `flow-bastion.yml` (section 9), which runs
`flow-probe.py` there over SSH.

---

## 11. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `flow-test.sh` prints `The estate converge ... is running` and `Waiting up to 30 min` | The hourly converge (or the 30-minute repo refresh) holds the run lock | Nothing: the tests start when it finishes. To stop waiting, press Ctrl-C; to never wait, run with `FLOW_LOCK_WAIT=0` (section 9.3) |
| `Still held after 30 min; not running the tests` | The converge is taking unusually long or is stuck | `journalctl -u ansible-estate.service -n 50` |
| A playbook run on its own stops with `A converge or reconverge holds .../zms-ansible.lock` | As above; single playbooks do not wait | Wait until `systemctl is-active ansible-estate.service ansible-bootstrap.service` prints `inactive` twice, or use `flow-test.sh` |
| `Cannot check the run lock ... Permission denied` | The lock file is not owned by `ubuntu`, usually because a converge was once run as root | `sudo chown ubuntu:ubuntu /opt/control-repo/.git/zms-ansible.lock` (only while no converge is running) |
| `Run with sudo: estate.env is readable by root only.` | `estate-shell.sh` was run without `sudo` | Add `sudo` |
| `flow-test.sh: command not found` or `Permission denied` | The repo refresh has not run since the files were pushed | `sudo systemctl start ansible-bootstrap.service`, or run it as `sudo bash /opt/control-repo/scripts/flow-test.sh learn` |
| `Invalid setting: ...` | A misspelled `-e` value | Use the values in section 9.2 |
| `No bastion found, so F5/F6 cannot run` | `bastion_type = "none"`, or the bastion is not in the inventory | Check `ansible-inventory --graph role_bastion`, or skip with `FLOW_SKIP=flow-bastion` |
| Windows bastion `UNREACHABLE` in `flow-bastion.yml` | WinRM not enabled on it (an estate built before the Terraform change, section 3.4), or after enforcement the 5986 management flow is not allowed | `terraform apply` (or run the bootstrap by hand, section 3.4); allow Ansible RG to Bastion RG TCP 5986. Use `-e flow_bastion_kinds=linux` meanwhile |
| Linux bastion `UNREACHABLE` | After enforcement, Ansible RG to Bastion RG TCP 22 is not allowed | Allow it |
| F7 or F8 `UNREACHABLE` | Host stopped, or (after enforcement) the policy blocks F7/F8 | `az vm list -d -g <rg> -o table` for power state; then the policy |
| F9 `FAILED`, `did not run: Failed to become user ...` | The second sign-in that dcdiag and repadmin use to reach the other DC was refused | Check that `windows_admin_password` is still the domain admin's password and that `<NETBIOS>\zmsadmin` can sign in |
| F9 `FAILED` with `Access is denied` or `LDAP error 49` | Wrong or expired domain credentials | As above |
| F9 `FAILED` with RPC or `server is unavailable` errors | The other DC is down, or (after enforcement) AD replication ports are not allowed between dc and rodc | Check both DCs are running; review the F9 rule |
| F1 `PASS` but `detail="HTTP/1.1 503 ..."` | The network flow worked; the middleware or database behind the web tier is down | `servicedesk-verify.yml` names the broken tier |
| F10 or internet site `OPEN` with `TLS failed: certificate verify failed` | Traffic is being SSL-inspected (for example by Zscaler Internet Access) and the hosts do not trust the inspecting CA | Install the CA on the hosts, or run with `-e flow_validate_certs=false`. The flow itself worked |
| An internet probe `ERROR` with `DNS: ...` | The host could not resolve the site's name | Check outbound DNS from that host |
| In `verify`, every N-flow `OPEN` | The policy is not enforcing yet | Enforce it, then run `verify` again |
| `flow-bastion` step `FAILED` but the Linux bastion lines are `PASS` | The Windows bastion part failed (usually WinRM) | See the Windows bastion row above |

For a run that stops early, rerun the failing playbook on its own with `-v` (section 9.1) to see
the full Ansible output.

---

## 12. What the tests change, and what they do not

- **No configuration changes on any host.** Probes open a connection and close it. SSH and RDP probes
  never sign in. The MySQL probe never logs in.
- **Temporary files only.** On Linux hosts, Ansible copies `flow-probe.py` to its temporary directory
  and deletes it after the run. On Windows hosts, the probe script runs in memory through WinRM.
- **One real AD action.** `repadmin /replicate` asks the rodc to replicate the domain partition from
  the dc now. That is the same replication that happens on schedule anyway.
- **Logs.** Results go to `/var/log/ansible/flow-exercise.log` on the control node, and the
  Ansible output also to `/var/log/ansible/ansible.log`, as with any manual run. Converge status
  logs are not touched.
- **No schedules.** Nothing runs these tests automatically.
- **Linux sshd logs** show `Connection closed by <source> port ... [preauth]` for each SSH probe.
  The DCs log RDP connection attempts, and the runas sign-ins for F9 appear as normal sign-in events
  in their Security log.

---

## 13. Reference

### Files

| File | Purpose |
|---|---|
| `scripts/flow-test.sh` | One-command runner: `learn` or `verify` |
| `vars/flows.yml` | Internet sites, denied-flow targets, bastion settings, phase defaults |
| `playbooks/flow-exercise.yml` | F1-F3, F7-F10 |
| `playbooks/flow-internet.yml` | Internet sites (F10), client (N7) |
| `playbooks/flow-bastion.yml` | F5, F6, N6 from the Linux and/or Windows bastion |
| `playbooks/flow-negative.yml` | N1-N5 |
| `playbooks/flow-preflight.yml` | Lock check, setting checks, run ID (imported by each playbook) |
| `playbooks/flow-report.yml` | Summary; fails the playbook if any probe failed (imported) |
| `playbooks/tasks/flow-run.yml` | Runs the probe engine for the host's operating system |
| `playbooks/tasks/flow-record.yml` | Tags the results and appends them to the log |
| `playbooks/files/flow-probe.py` | Linux probe engine (Python 3.6+, standard library only) |
| `playbooks/files/flow-probe.ps1` | Windows probe engine for the DCs (PowerShell 5.1+) |
| `playbooks/files/flow-bastion.ps1` | Windows bastion F5/F6/N6 script, also for use by hand |
| `FLOW-TESTS.md` | Quick reference |

### Commands at a glance

```bash
# On your PC
ssh zms-control                                                    # section 4.1

# On the control node
tail -n 3 /var/log/ansible/converge-status.log                     # estate ready?
sudo /opt/control-repo/scripts/estate-shell.sh ansible-inventory --graph
sudo /opt/control-repo/scripts/flow-test.sh learn                  # during the learning window
sudo /opt/control-repo/scripts/flow-test.sh verify                 # after enforcement
grep ' run=<run id> ' /var/log/ansible/flow-exercise.log           # results of one run
```

```bash
# Or from your PC with the Azure CLI (section 7A)
RG=<resource-group>
VM=$(az vm list -g "$RG" --query "[?tags.Role=='ansible-control'].name" -o tsv)
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts '/opt/control-repo/scripts/flow-test.sh learn' --query "value[0].message" -o tsv
az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript \
  --scripts '/opt/control-repo/scripts/flow-test.sh verify' --query "value[0].message" -o tsv
```

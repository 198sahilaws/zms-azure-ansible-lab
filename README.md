# ZMS Azure Lab: Ansible

Azure port of `ZMS-AWS-Ansible-2`. Terraform (the `Azure-Infrastructure-Code` repo) builds the network, identities, Key Vault and VMs and stops at the guest boundary. This repo configures everything inside the guests: baseline packages, web servers, databases, an Active Directory forest, and two multi-tier demo applications (Service Desk and ZMS microservices).

It runs from the Ansible control node inside the VNet, in **push mode**, on two systemd timers. The control node pulls this repo; it pushes configuration out over SSH (Linux) and WinRM over HTTPS (Windows).

## Status

Written 2026-10-16. Statically checked: every playbook passes `--syntax-check` under ansible-core 2.21, `ansible-lint` reports nothing new against the AWS baseline, the inventory file renders the expected groups from fake VM data, and the Key Vault lookups and the multi-service deployment loop were exercised against mocks. **It has not run against a live Azure estate yet.** The first live gate is step 4 of the work breakdown: `ansible-inventory --graph` on the control node shows the hosts in the right groups.

## How it joins the Terraform

Neither repo references the other by resource ID. They meet at three contracts, and this repo follows the Azure Terraform as written.

| Contract | Terraform produces | This repo consumes |
| --- | --- | --- |
| **Tags → groups** | VM tags `ManagedBy=Terraform`, `Environment`, `OS`, `Distro`, `OSVersion` (RHEL/SLES only), `Role`, `AppEnv` (ring fence only) | `inventory/azure_rm.yml` → `os_*`, `distro_*`, `osver_*`, `role_*`, `env_*`, `appenv_*` |
| **Login users** | `admin_username` per distro: `ubuntu`, `ec2-user` (RHEL), `ec2-user` (SLES); control node `ubuntu`; Windows `zmsadmin` | `group_vars/distro_*.yml` `ansible_user`; Windows user from the secret |
| **Secret** | One Key Vault per estate, one JSON secret with **10 keys** (the 9 AWS keys plus `zms_app_db_password`); control identity has *Key Vault Secrets User* | `zms_keyvault_secret` lookups (repo `lookup_plugins/`), one field at a time, one shared managed-identity token |
| **`/etc/ansible/estate.env`** | `AZURE_LOCATION`, `AZURE_SUBSCRIPTION_ID`, `AZURE_CLIENT_ID`, `ANSIBLE_AZURE_AUTH_SOURCE=msi`, `ANSIBLE_RESOURCE_GROUP`, `ANSIBLE_KEYVAULT_NAME`, `ANSIBLE_SECRET_NAME`, `ANSIBLE_ESTATE`, `CONTROL_REPO_DIR` | `group_vars/all.yml`, `scripts/*.sh`, the inventory plugin |

Tag values are case-sensitive and used verbatim: `Role=Middleware` produces `role_Middleware` and matches no playbook.

## Layout

```
ansible.cfg              inventory = inventory/azure_rm.yml, forks = 20, become = True
site.yml                 pre-flight asserts, then the Linux and Windows baseline
orchestrate.yml          every convergence playbook, in dependency order (manual runs)
bootstrap.yml            control-node self-converge: writes the SSH key from Key Vault
local.yml                ansible-pull shim -> bootstrap.yml
inventory/azure_rm.yml   azure_rm dynamic inventory (replaces aws_ec2.yml)
inventory/group_vars/    all, os_linux, os_windows, distro_{ubuntu,rhel,sles}, osver_8, osver_15
playbooks/               29 playbooks, plus tasks/azure-dns-forwarder.yml
roles/                   baseline, servicedesk_{app,db,client}, zms_{app_db,microservice}
vars/                    servicedesk.yml, zms-app.yml (application topology maps)
scripts/                 converge.sh, reconverge.sh, notify-result.sh, collect-debug.sh, estate-shell.sh
systemd/                 ansible-bootstrap and ansible-estate services and timers, estate.env.example
```

## What changed from AWS

Everything above the inventory and the secret lookup is the AWS code, with the 2026-10-15 fix-pass behaviour applied where the as-built document records it. The changes:

| Area | AWS | Azure |
| --- | --- | --- |
| Inventory | `amazon.aws.aws_ec2`, filtered by `tag:ManagedBy` and `tag:Environment` | `azure.azcollection.azure_rm`, scoped to the estate's **resource group**, plus the same two tag filters. Hostname is still the primary private IP |
| Secret lookup | `amazon.aws.aws_secret` | `zms_keyvault_secret` (repo `lookup_plugins/`) with the control node's managed identity: one token shared by all workers and retries on HTTP 429, because the collection's `azure_keyvault_secret` requested a token per call and hit Azure's 5-concurrent limit. Still one field per lookup; no global credentials variable |
| Windows secret keys | No `default('', true)` (hard failure on an empty secret) | Same default as every other key |
| ZMS app password | Fell back silently to the MariaDB root password | Reads the new `zms_app_db_password` key; falls back to root only if it is empty, with a warning |
| Amazon Linux | `distro_amazon`, three playbooks | Removed, no replacement (decision 1). `amazonlinux-{httpd,mysql,setup}.yml` and `distro_amazon.yml` are gone |
| ZMS topology | catalog DB on Amazon Linux, inventory service on Amazon Linux | catalog DB on RHEL, inventory service on Ubuntu (see below) |
| Alerts | SNS topic ARN, never written by cloud-init, so off | None, by design. Results go to `/var/log/ansible/converge-status.log` and `converge-failures.log` only |
| Control-node Python | `python3-boto3` from cloud-init | `reconverge.sh` installs azure.azcollection's requirements and `python3-winrm` when an import check fails |
| Domain controllers | Root hints | Also forward to Azure DNS `168.63.129.16` (`ad_dns_forwarders`) |
| Manual runs | Source `estate.env` by hand | `sudo scripts/estate-shell.sh` |
| Debug bundle | IMDSv2, IAM role, Secrets Manager | Azure IMDS, managed-identity token status, Key Vault secret metadata |

### Fixes found while porting

- **ZMS restart handler.** Calling the role in a loop (needed for two services on one host) broke its handler: in testing it resolved the service name from another host's loop item and restarted the wrong unit. The handler now reads a fact pinned per call.
- **Shared payload restart.** With two services on one host, only the first call saw the payload change and restarted. The second call now restarts too.
- **Service Desk web tier.** `servicedesk-web.yml` failed every converge on an estate that has an Ubuntu web host but no middleware tier (the ZMS estate). It now skips when the estate has no `role_middleware` group at all, the same rule `zms-app-services.yml` uses. `servicedesk-client.yml` now has the same guard.

### Choosing the distro and the AD domain (deployments/web-app-adds)

- **Service Desk on any distro.** `vars/servicedesk.yml` places each tier on `role_<tier>` & `os_linux` instead of `distro_ubuntu`. The roles already handled Ubuntu, RHEL and SLES (apt/dnf/zypper, MySQL or MariaDB, firewalld), but the Ubuntu pin silently left RHEL or SLES tiers out. The database follows the db host: MySQL on Ubuntu, MariaDB on RHEL and SLES.
- **ZMS lab stays off Service Desk estates.** With RHEL or SLES Service Desk tiers, the ZMS placement map would match too (orders on the web host, `zms_catalog`/`zms_orders` on the db). `zms_app_enabled` in `vars/zms-app.yml` is false whenever the estate has a `role_middleware` tier, and `zms-app-db`, `-services` and `-frontend` end there. Override with `-e zms_app_enabled=true`.
- **AD domain from Terraform.** `ad_domain_name` / `ad_domain_netbios` live in `group_vars/os_windows.yml` and read the `ADDomain` / `ADNetBIOS` VM tags; without them (adds, custom) they stay `alcor.co.in` / `ALCOR`. The four AD plays no longer pin the name as play vars, which outranked the inventory.
- **Ubuntu 26.04 MySQL.** 26.04 ships MySQL 8.4, which no longer loads `mysql_native_password`. Every account on an Ubuntu db host uses it (root, `sdapp`), so `ubuntu-mysql.yml` writes `/etc/mysql/mysql.conf.d/zz-native-password.cnf` (`mysql_native_password = ON`) and restarts MySQL, only when `mysqld --version` is Oracle MySQL 8.4.x; 22.04 and 24.04 (MySQL 8.0) are untouched. MySQL 9.0 removed the plugin, so a 9.x server would need the accounts moved to `caching_sha2_password`.
- **SLES 15 Python.** SLES 15 ships Python 3.6, which ansible-core 2.21 cannot use on a managed node. `rhel8-python-bootstrap.yml` gained a SLES 15 play that installs `python311` (activating SUSE's Python 3 Module if needed), and `group_vars/osver_15.yml` selects it, as `osver_8.yml` does for RHEL 8.

## The ZMS topology on Azure

| Service | Port | App host | DB host | Schema |
| --- | --- | --- | --- | --- |
| catalog | 8001 | Ubuntu web | **RHEL db** | `zms_catalog` |
| inventory | 8002 | **Ubuntu web** | SLES db | `zms_inventory` |
| orders | 8003 | RHEL web | RHEL db | `zms_orders` |
| storefront | 8080 | SLES web | — | — |

That is five VMs, the host set commented out in `deployments/custom/terraform.tfvars.example`. The Ubuntu web host runs two services, so `zms-app-services.yml` now calls the role once per service. A deployment without these distros (such as `web-app`) skips the lab and says why. It does not fail.

## Running it

**Automatically.** Terraform's cloud-init clones this repo to `/opt/control-repo`, installs the collections and the two timers, and starts the first bootstrap.

| Unit | Schedule | Runs |
| --- | --- | --- |
| `ansible-bootstrap.timer` | 2 min after boot, then every 30 min | `reconverge.sh`: hard-reset to origin, refresh collections, ensure the Python dependencies, `bootstrap.yml` |
| `ansible-estate.timer` | 10 min after boot, then every 60 min | `converge.sh`: each playbook as its own `ansible-playbook` process |

`converge.sh`, not `orchestrate.yml`, is what the timer runs. The two play lists must be kept in step by hand.

**By hand**, on the control node (`estate.env` is root-only, so use the wrapper):

```bash
sudo /opt/control-repo/scripts/estate-shell.sh ansible-inventory --graph
sudo /opt/control-repo/scripts/estate-shell.sh ansible-playbook site.yml --check --diff
sudo /opt/control-repo/scripts/estate-shell.sh scripts/converge.sh --linux
sudo /opt/control-repo/scripts/estate-shell.sh ansible-playbook playbooks/zms-app-verify.yml
sudo /opt/control-repo/scripts/estate-shell.sh          # interactive shell as ubuntu
```

**From a workstation** with `az login` (inventory only; the Key Vault lookups need the vault role too):

```bash
ANSIBLE_AZURE_AUTH_SOURCE=cli ANSIBLE_AZURE_VM_RESOURCE_GROUPS=<rg> ansible-inventory --graph
```

## Checks, in build-sequence order

| Work-breakdown step | Check on the control node |
| --- | --- |
| 4. control node + one Ubuntu host | `sudo tail /var/log/zms-control-bootstrap.log` ends in `complete`; `estate-shell.sh ansible-inventory --graph` shows the host in `os_linux`, `distro_ubuntu`, `role_*`, `env_*`; `ls -l /etc/ansible/keys/ansible_rsa` |
| 5. remaining Linux distros | `estate-shell.sh ansible os_linux -m ping`; `estate-shell.sh ansible-playbook site.yml` |
| 6. Windows and bastions | `estate-shell.sh ansible os_windows -m ansible.windows.win_ping`; `estate-shell.sh scripts/converge.sh --windows` builds the forest |
| 8. full converge | `systemctl status ansible-estate.service`; `tail /var/log/ansible/converge-status.log`; `servicedesk-verify.yml` and `zms-app-verify.yml` by hand |

When something fails: `sudo scripts/collect-debug.sh` writes a redacted bundle under `/tmp`.

## Things to know

- **The resource group is the estate boundary.** The plugin reads it from `ANSIBLE_AZURE_VM_RESOURCE_GROUPS`, which the scripts copy from `ANSIBLE_RESOURCE_GROUP`. A run with neither set falls back to the whole subscription, which the control identity cannot list. It fails closed, unlike the AWS `*` fallback.
- **`fail_on_template_errors: false` in the inventory is load-bearing.** The plugin defaults to strict mode, where a VM without one tag loses all the groups after it. The fake-data render caught this.
- **The control node converges itself.** It is tagged `OS=linux`, so `site.yml` applies the Linux baseline to it. It has no `Distro` tag and connects to itself as `ubuntu`. Its role group is `role_ansible_control`: the plugin turns the hyphen into an underscore.
- **WinRM certificate validation stays off.** The listener is self-signed and Ansible connects by IP (work-breakdown correction 7).
- **Teaching choices are kept on purpose:** flat subnet NSGs, one SSH key per estate, 503s that name the failed tier, no reverse proxy, no connection pooling, `gunicorn --workers 2` on `0.0.0.0`.
- **Not yet confirmed on Azure:** the PPA Ansible bundle's `azure.azcollection` version on a live node, the pip install of its requirements into system Python, and the AD chain on Azure DCs (static IPs, DHCP-configured NIC inside the guest).

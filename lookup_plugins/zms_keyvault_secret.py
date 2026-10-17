# -*- coding: utf-8 -*-
# Repo-local lookup: read a Key Vault secret with the control node's managed
# identity, sharing ONE access token between every Ansible worker process.
#
# WHY THIS EXISTS (instead of azure.azcollection.azure_keyvault_secret)
# The collection lookup asks the Instance Metadata Service (IMDS) for a fresh
# token on EVERY call, with no cache and no retry. Ansible re-templates
# connection variables for every task on every host, and os_windows.yml makes
# ansible_user and ansible_password lookups, so five Windows hosts on 20 forks
# fire about ten token requests at the same moment. Azure limits the managed
# identity endpoint to 20 requests per second and 5 concurrent requests per VM
# and answers the rest with HTTP 429; the collection lookup then gives up and
# the task fails on that host.
#
# This plugin:
#   - caches the token on tmpfs (/dev/shm/zms-ansible-<uid>/, 0700 dir, 0600
#     file) under an exclusive lock, so concurrent workers make one IMDS call
#     between them and reuse the token until five minutes before it expires;
#   - retries IMDS and Key Vault on 429 and 5xx with backoff, honouring
#     Retry-After;
#   - never caches the SECRET itself: every lookup is still one Key Vault GET,
#     and the field-scoped convention in group_vars/all.yml is unchanged;
#   - off Azure (no IMDS, e.g. a workstation with `az login`), hands the call
#     to the collection lookup with use_msi=false, so CLI auth keeps working.
#     Not on the control node: there estate.env sets ANSIBLE_AZURE_AUTH_SOURCE=msi,
#     so a connection error to IMDS is retried and then fails loudly, rather
#     than silently switching to an auth method the node does not have.
#
# The cached token is a bearer token for https://vault.azure.net, scoped by
# RBAC to Key Vault Secrets User on this estate's vault (one secret). It lives
# in RAM (tmpfs), is readable only by the service user, and is gone at reboot.

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
name: zms_keyvault_secret
short_description: Key Vault secret via managed identity, with a shared token cache
description:
  - Returns the value of each named secret in an Azure Key Vault.
  - Drop-in for C(azure.azcollection.azure_keyvault_secret) on the control node, for the
    vault_url and client_id options this repo uses.
  - Authenticates with the VM's managed identity. The access token is shared by all
    Ansible worker processes through a locked cache on tmpfs, and IMDS and Key Vault
    calls are retried on HTTP 429 and 5xx.
  - When IMDS is unreachable (not an Azure VM), delegates to
    C(azure.azcollection.azure_keyvault_secret) with C(use_msi=false), unless the
    environment sets C(ANSIBLE_AZURE_AUTH_SOURCE=msi) (the control node), in which
    case IMDS connection errors are retried and then reported.
options:
  _terms:
    description: Secret name(s).
    required: true
  vault_url:
    description: Vault URL, e.g. https://kvexample.vault.azure.net (no trailing slash needed).
    type: str
    required: true
  client_id:
    description: Client ID of the user-assigned managed identity. Empty uses the VM's only identity.
    type: str
    default: ''
"""

EXAMPLES = r"""
- ansible.builtin.debug:
    msg: >-
      {{ (lookup('zms_keyvault_secret', ansible_secret_name,
          vault_url=azure_keyvault_url, client_id=azure_client_id)
          | from_json).winrm_username }}
"""

RETURN = r"""
_raw:
  description: The secret value(s).
  type: list
  elements: str
"""

import errno
import fcntl
import hashlib
import json
import os
import random
import stat
import time

from ansible.errors import AnsibleError
from ansible.module_utils.six.moves.urllib.error import HTTPError, URLError
from ansible.module_utils.six.moves.urllib.parse import quote, urlencode
from ansible.module_utils.six.moves.urllib.request import ProxyHandler, Request, build_opener
from ansible.plugins.loader import lookup_loader
from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display

display = Display()

IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
VAULT_RESOURCE = "https://vault.azure.net"
VAULT_API_VERSION = "7.4"
CACHE_BASE = os.environ.get("ZMS_TOKEN_CACHE_BASE", "/dev/shm")

# Refresh this long before expiry, so a token never expires mid-play.
TOKEN_REFRESH_MARGIN = 300
# How long a worker waits for another worker's token fetch.
LOCK_TIMEOUT = 90
# After IMDS is found unreachable (not an Azure VM), skip it for this long so a
# workstation run does not pay a connect timeout on every lookup.
NO_IMDS_TTL = 600
# IMDS documents retrying 404, 410, 429 and 5xx; Key Vault throttles with 429.
RETRY_STATUSES = frozenset([404, 410, 429, 500, 502, 503, 504])
IMDS_ATTEMPTS = 8
VAULT_ATTEMPTS = 6
MAX_BACKOFF = 20.0


class _NoImds(Exception):
    """IMDS cannot be reached at all: not an Azure VM."""


def _backoff(attempt, retry_after=None):
    if retry_after:
        try:
            return min(float(retry_after), MAX_BACKOFF) + random.uniform(0, 0.5)
        except ValueError:
            pass
    return min(0.5 * (2 ** attempt), MAX_BACKOFF) * random.uniform(0.5, 1.0)


class _TokenCache(object):
    """One token per client_id in /dev/shm/zms-ansible-<uid>/, guarded by flock."""

    def __init__(self, client_id):
        self.dir = None
        uid = os.getuid()
        path = os.path.join(CACHE_BASE, "zms-ansible-%d" % uid)
        try:
            os.mkdir(path, 0o700)
        except OSError as e:
            if e.errno != errno.EEXIST:
                display.vvv("zms_keyvault_secret: cannot create %s (%s); token cache off" % (path, e))
                return
        st = os.lstat(path)
        # Refuse a directory someone else planted or loosened: the token is a credential.
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
            display.warning("zms_keyvault_secret: %s is not a private directory owned by uid %d; "
                            "token cache disabled" % (path, uid))
            return
        self.dir = path
        tag = hashlib.sha256((client_id or "default").encode("utf-8")).hexdigest()[:16]
        self.file = os.path.join(path, "kv-token-%s.json" % tag)
        self.lockfile = os.path.join(path, "kv-token-%s.lock" % tag)
        self.no_imds_file = os.path.join(path, "no-imds")

    def __enter__(self):
        if self.dir is None:
            return self
        self.fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (IOError, OSError) as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.time() > deadline:
                    os.close(self.fd)
                    raise AnsibleError("zms_keyvault_secret: timed out waiting for the token cache lock %s"
                                       % self.lockfile) from None
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.dir is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        return False

    def read(self):
        if self.dir is None:
            return None
        try:
            fd = os.open(self.file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
        try:
            with os.fdopen(fd, "r") as f:
                data = json.load(f)
            if float(data["expires_on"]) - time.time() > TOKEN_REFRESH_MARGIN:
                return data["access_token"]
        except (ValueError, KeyError, TypeError, OSError):
            pass
        return None

    def write(self, token, expires_on):
        if self.dir is None:
            return
        tmp = "%s.%d.tmp" % (self.file, os.getpid())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"access_token": token, "expires_on": expires_on}, f)
        os.rename(tmp, self.file)

    def no_imds_recent(self):
        if self.dir is None:
            return False
        try:
            return time.time() - os.lstat(self.no_imds_file).st_mtime < NO_IMDS_TTL
        except OSError:
            return False

    def mark_no_imds(self):
        if self.dir is None:
            return
        try:
            os.close(os.open(self.no_imds_file, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
            os.utime(self.no_imds_file, None)
        except OSError:
            pass

    def invalidate(self):
        if self.dir is None:
            return
        try:
            os.unlink(self.file)
        except OSError:
            pass


def _imds_token(client_id, expect_imds):
    params = {"api-version": "2018-02-01", "resource": VAULT_RESOURCE}
    if client_id:
        params["client_id"] = client_id
    req = Request(IMDS_TOKEN_URL + "?" + urlencode(params), headers={"Metadata": "true"})
    opener = build_opener(ProxyHandler({}))  # IMDS must never go through a proxy
    last = None
    for attempt in range(IMDS_ATTEMPTS):
        try:
            resp = opener.open(req, timeout=3)
            body = json.loads(resp.read().decode("utf-8"))
            return body["access_token"], float(body.get("expires_on") or time.time() + 3600)
        except HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code not in RETRY_STATUSES:
                raise AnsibleError("zms_keyvault_secret: IMDS refused the token request (%s). Is the "
                                   "managed identity %s attached to this VM?" % (last, client_id or "(default)")) from None
            delay = _backoff(attempt, e.headers.get("Retry-After") if e.headers else None)
        except (URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
            if not expect_imds:
                # No TCP answer and nothing says this is an Azure VM: treat it as
                # a workstation and let the caller fall back without delay.
                raise _NoImds(last) from None
            delay = _backoff(attempt)
        display.vvv("zms_keyvault_secret: IMDS %s, retrying in %.1fs" % (last, delay))
        time.sleep(delay)
    raise AnsibleError("zms_keyvault_secret: no token from IMDS after %d attempts (last: %s)"
                       % (IMDS_ATTEMPTS, last))


def _vault_get(vault_url, name, token):
    url = "%s/secrets/%s?api-version=%s" % (vault_url.rstrip("/"), quote(name, safe=""), VAULT_API_VERSION)
    req = Request(url, headers={"Authorization": "Bearer " + token})
    opener = build_opener()  # honour proxy settings, as the collection lookup does
    last = None
    for attempt in range(VAULT_ATTEMPTS):
        try:
            resp = opener.open(req, timeout=30)
            return json.loads(resp.read().decode("utf-8"))["value"]
        except HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code == 401:
                return None  # caller drops the cached token and tries once more
            if e.code == 403:
                raise AnsibleError("zms_keyvault_secret: Key Vault denied secret '%s' (403). The control "
                                   "identity needs Key Vault Secrets User on the vault; right after the "
                                   "first apply this is RBAC propagation and clears on its own." % name) from None
            if e.code == 404:
                raise AnsibleError("zms_keyvault_secret: secret '%s' not found in %s" % (name, vault_url)) from None
            if e.code not in RETRY_STATUSES:
                raise AnsibleError("zms_keyvault_secret: Key Vault returned %s for '%s'" % (last, name)) from None
            delay = _backoff(attempt, e.headers.get("Retry-After") if e.headers else None)
        except (URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
            delay = _backoff(attempt)
        except (ValueError, KeyError):
            raise AnsibleError("zms_keyvault_secret: unexpected Key Vault response for '%s'" % name) from None
        display.vvv("zms_keyvault_secret: Key Vault %s for '%s', retrying in %.1fs" % (last, name, delay))
        time.sleep(delay)
    raise AnsibleError("zms_keyvault_secret: could not read '%s' after %d attempts (last: %s)"
                       % (name, VAULT_ATTEMPTS, last))


class LookupModule(LookupBase):

    def run(self, terms, variables=None, **kwargs):
        self.set_options(var_options=variables, direct=kwargs)
        vault_url = self.get_option("vault_url")
        client_id = self.get_option("client_id") or ""
        if not vault_url:
            raise AnsibleError("zms_keyvault_secret: vault_url is required")

        # The control node's estate.env sets msi; a workstation sets cli or nothing.
        expect_imds = os.environ.get("ANSIBLE_AZURE_AUTH_SOURCE", "").strip().lower() == "msi"
        cache = _TokenCache(client_id)
        try:
            with cache:
                if not expect_imds and cache.no_imds_recent():
                    raise _NoImds("marked unreachable within the last %d s" % NO_IMDS_TTL)
                token = cache.read()
                if token is None:
                    try:
                        token, expires_on = _imds_token(client_id, expect_imds)
                    except _NoImds:
                        cache.mark_no_imds()
                        raise
                    cache.write(token, expires_on)
        except _NoImds as e:
            display.vvv("zms_keyvault_secret: IMDS unreachable (%s); using azure.azcollection's "
                        "non-MSI authentication" % e)
            fallback = lookup_loader.get("azure.azcollection.azure_keyvault_secret",
                                         loader=self._loader, templar=self._templar)
            if fallback is None:
                raise AnsibleError("zms_keyvault_secret: IMDS is unreachable and azure.azcollection is "
                                   "not installed, so there is no other way to authenticate") from None
            return fallback.run(terms, variables, vault_url=vault_url, use_msi=False)

        ret = []
        for term in terms:
            value = _vault_get(vault_url, term, token)
            if value is None:
                # 401: the cached token was revoked or rotated early. Another
                # worker may already have replaced it; otherwise fetch a new one.
                failed = token
                with cache:
                    current = cache.read()
                    if current is not None and current != failed:
                        token = current
                    else:
                        cache.invalidate()
                        token, expires_on = _imds_token(client_id, expect_imds)
                        cache.write(token, expires_on)
                value = _vault_get(vault_url, term, token)
                if value is None:
                    raise AnsibleError("zms_keyvault_secret: Key Vault rejected a fresh token (401) for '%s'"
                                       % term)
            ret.append(value)
        return ret

"""WSGI entrypoint. gunicorn loads sdapp.wsgi:app (see the systemd unit).

One payload, two tiers, selected by SD_TIER — the same trick zmsapp.wsgi uses
with ZMS_SERVICE. Both hosts get an identical /opt/servicedesk tree and an
identical unit file; only the environment differs. That is what makes the
"promote a host to a different tier" exercise a one-line env change rather
than a redeploy.

Importing conditionally matters: sdapp.api imports pymysql at module scope, so
an unconditional import would make the web tier depend on a driver it has no
credentials for, and a missing-wheel failure there would look like a tier
outage instead of an install bug.
"""
from . import config

TIERS = ("middleware", "web")

if config.TIER == "web":
    from .web import app
elif config.TIER == "middleware":
    from .api import app
else:
    raise RuntimeError(
        "SD_TIER=%r is not one of %s. It is set from sd_tier in the playbook "
        "and written into /etc/servicedesk/servicedesk.env; an empty value "
        "usually means the env file was not regenerated after an upgrade."
        % (config.TIER, ", ".join(TIERS)))

__all__ = ["app"]

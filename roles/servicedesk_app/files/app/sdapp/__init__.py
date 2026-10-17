"""ZMS lab service desk — a small Flask + MySQL application in three tiers.

Exists to put realistic, continuously changing data in front of the Ubuntu
MySQL host and to make the tiers talk to each other over the network, so the
estate produces enterprise-shaped east-west traffic instead of an idle port.

ONE PAYLOAD, TWO TIERS. This package is installed identically on the web and
middleware hosts; SD_TIER in the environment file decides which Flask app
wsgi.py imports:

    SD_TIER=web         -> sdapp.web   :8090  HTML, no database driver in play
    SD_TIER=middleware  -> sdapp.api   :8091  JSON, business rules, all the SQL

So the presence of db.py and pymysql on the web host is expected and is not a
credential leak — web.py never imports them, and the web host has no password.
"""
# 2.x = three tiers (web / middleware / db). 1.x was the combined web+logic app.
# Keep in step with sd_app_version in vars/servicedesk.yml.
__version__ = "2.0.0"

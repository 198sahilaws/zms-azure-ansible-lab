"""ZMS microservices lab.

Four Flask applications that share this package:

    catalog      product master data          -> its own MariaDB host
    inventory    stock levels per warehouse   -> a different MariaDB host
    orders       orders and order lines       -> a third MariaDB host
    storefront   web UI, owns no database     -> calls the three over HTTP

The hard rule the lab is built to demonstrate: a service touches its OWN
database and nothing else. There is no cross-database join anywhere in this
package. Whenever a page needs data from more than one service, the join
happens in Python over HTTP responses -- see zmsapp.storefront.

Everything is configured from the environment (see zmsapp.config) so the same
payload is deployed unchanged to all four hosts; only /etc/zms-app/<svc>.env
differs.
"""

__version__ = "1.0.0"

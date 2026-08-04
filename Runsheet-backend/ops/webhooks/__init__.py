"""
Ops webhooks subpackage for the Ops Intelligence Layer.

Now holds only :mod:`ops.webhooks.hmac_util`, the single HMAC-SHA256
sign/verify implementation shared by the order-intake pipeline and the Dinee
voice bridge. The ``POST /webhooks/dinee`` receiver that this package was
created for has been removed; inbound order webhooks are served by
``fuel/api/order_webhook_endpoints.py`` against the ``intake_channels``
registry, with a per-channel secret rather than one global credential.
"""

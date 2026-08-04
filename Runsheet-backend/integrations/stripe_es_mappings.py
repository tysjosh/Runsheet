"""Elasticsearch mapping for the Stripe demo payment-intent index.

``StripeConnector`` serves ``list_payment_intents`` from Elasticsearch when the
tenant is ``demo-tenant`` (``use_mock_data``), reading the documents that
``scripts/data/stripe_payment_seeds.json`` loads. Nothing declared a mapping for
that index, so it was created implicitly by the seeder's bulk write and got a
**dynamic** mapping — which infers analyzed ``text`` for every string, including
``tenant_id``.

The connector scopes its query with ``{"term": {"tenant_id": self._tenant_id}}``.
A term query against analyzed text matches only when a produced token equals the
whole term, and the standard analyzer splits ``demo-tenant`` into ``demo`` +
``tenant``. So the index held 15 payment intents and the connector matched 0 of
them — a populated index serving an empty list, with no error anywhere.

Declaring the mapping is the fix: ``tenant_id`` is a ``keyword`` and ``created``
is a ``date`` so the range filter and the ``created desc`` sort both work on a
real type rather than on whatever the first document happened to look like.

Not ``dynamic: strict``: this index mirrors a third-party payload we do not
control, so an unexpected Stripe field should be stored, not cause the whole
document to be rejected.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

STRIPE_PAYMENT_INTENTS_INDEX = "stripe_payment_intents"

STRIPE_PAYMENT_INTENTS_MAPPING = {
    "mappings": {
        "properties": {
            "payment_id": {"type": "keyword"},
            # The whole reason this file exists — see the module docstring.
            "tenant_id": {"type": "keyword"},
            # Minor units (cents), matching Stripe's own representation.
            "amount": {"type": "long"},
            "currency": {"type": "keyword"},
            "status": {"type": "keyword"},
            "customer_id": {"type": "keyword"},
            "customer_email": {"type": "keyword"},
            "customer_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "description": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "payment_method": {"type": "keyword"},
            # Third-party shapes; stored and queryable but not pinned field by
            # field, since Stripe may add nested keys at any time.
            "payment_method_details": {"type": "object", "dynamic": True},
            "metadata": {"type": "object", "dynamic": True},
            # Filtered with a range and used as the sort key, so it must be a
            # date rather than the string a dynamic mapping might infer.
            "created": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
}

STRIPE_INDEX_MAPPINGS = {
    STRIPE_PAYMENT_INTENTS_INDEX: STRIPE_PAYMENT_INTENTS_MAPPING,
}


def setup_stripe_indices(es_service) -> None:
    """Create the Stripe demo indices if they do not already exist.

    Same shape as every other ``setup_*_indices`` helper so the seeder can call
    it uniformly. Creation must run BEFORE the JSON fixtures load, or the bulk
    write creates the index dynamically and this mapping never applies.
    """
    from services.elasticsearch_service import ElasticsearchService

    client = es_service.client
    for index_name, mapping in STRIPE_INDEX_MAPPINGS.items():
        try:
            if not client.indices.exists(index=index_name):
                body = mapping
                if es_service.is_serverless:
                    body = ElasticsearchService.strip_serverless_incompatible_settings(
                        mapping
                    )
                client.indices.create(index=index_name, body=body)
                logger.info(f"✅ Created Stripe index: {index_name}")
            else:
                logger.info(f"📋 Stripe index already exists: {index_name}")
        except Exception:
            logger.exception("Failed to create Stripe index %s", index_name)


__all__ = [
    "STRIPE_PAYMENT_INTENTS_INDEX",
    "STRIPE_PAYMENT_INTENTS_MAPPING",
    "STRIPE_INDEX_MAPPINGS",
    "setup_stripe_indices",
]

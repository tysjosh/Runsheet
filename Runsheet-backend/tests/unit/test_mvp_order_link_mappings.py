"""Coverage for exact fuel-order linkage in MVP planning documents."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from Agents.support.mvp_es_mappings import (
    MVP_LOAD_PLANS_INDEX,
    MVP_ROUTES_INDEX,
    setup_mvp_indices,
)


def test_existing_plan_indices_receive_additive_order_link_fields():
    indices = MagicMock()
    indices.exists.return_value = True
    es_service = SimpleNamespace(
        client=SimpleNamespace(indices=indices),
        is_serverless=False,
    )

    setup_mvp_indices(es_service)

    updates = {
        call.kwargs["index"]: call.kwargs["body"]
        for call in indices.put_mapping.call_args_list
    }
    assert updates[MVP_LOAD_PLANS_INDEX]["properties"]["assignments"][
        "properties"
    ]["order_id"] == {"type": "keyword"}
    assert updates[MVP_ROUTES_INDEX]["properties"]["stops"]["properties"][
        "order_ids"
    ] == {"type": "keyword"}

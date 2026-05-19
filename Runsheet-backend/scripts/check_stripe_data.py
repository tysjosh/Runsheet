#!/usr/bin/env python3
"""Quick script to verify Stripe payment data in Elasticsearch."""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from elasticsearch import Elasticsearch

env_file = Path(__file__).parent.parent / ".env.development"
load_dotenv(env_file)

ELASTIC_ENDPOINT = os.getenv("ELASTIC_ENDPOINT")
ELASTIC_API_KEY = os.getenv("ELASTIC_API_KEY")

es = Elasticsearch(ELASTIC_ENDPOINT, api_key=ELASTIC_API_KEY)

# Check if index exists and count documents
if es.indices.exists(index="stripe_payment_intents"):
    count = es.count(index="stripe_payment_intents")
    print(f'✅ Index exists with {count["count"]} documents')
    
    # Get a sample document
    result = es.search(
        index="stripe_payment_intents",
        body={"query": {"match_all": {}}, "size": 3, "sort": [{"created": "desc"}]}
    )
    
    if result["hits"]["hits"]:
        print("\nSample payments:")
        for hit in result["hits"]["hits"]:
            doc = hit["_source"]
            amount_usd = doc["amount"] / 100
            print(f'  • {doc["payment_id"]} - ${amount_usd:.2f} - {doc["status"]} - {doc.get("customer_name", "N/A")}')
else:
    print("❌ Index does not exist")

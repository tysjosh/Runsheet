from types import SimpleNamespace

from fuel.intake.csv_adapter import CsvIntakeAdapter


def test_csv_order_converts_decimal_dollar_rate_to_exact_micros():
    result = CsvIntakeAdapter().transform(
        {
            "customer_id": "CITY-JOLIET",
            "customer_name": "City of Joliet",
            "ship_to_address": "Joliet, IL",
            "ship_to_lat": 41.525,
            "ship_to_lon": -88.082,
            "product_code": "DIESEL_2",
            "gallons_requested": 7_000,
            "unit_price_usd": "2.9660",
            "call_type": "one_off",
            "delivery_window_start": "2025-05-06T06:00:00-05:00",
            "delivery_window_end": "2025-05-06T11:30:00-05:00",
            "source_system": "city_of_joliet_public_records",
            "source_order_id": "W1736698",
            "import_batch_id": "public-record-regression",
            "csv_row_number": 1,
        },
        SimpleNamespace(channel=SimpleNamespace(channel_id="csv-public")),
    )

    assert result.order_doc["unit_price_micros"] == 2_966_000
    assert result.order_doc["unit_price_cents"] == 297
    assert result.order_doc["subtotal_cents"] == 2_076_200
    assert result.order_doc["total_cents"] == 2_076_200

"""
Fuel-specific notification template definitions.

Defines four fuel distribution notification templates:
- low_tank_autofill_alert: Sent when tank level drops below reorder point
- past_due_invoice: Sent when an invoice transitions to overdue status
- delivery_completed: Sent when POD is confirmed for a fuel delivery
- e_bol_delivery: Sent when a signed BOL PDF is generated

Each template defines event_type, channel, subject_template, body_template,
and placeholders following the same structure as the core DEFAULT_TEMPLATES
in template_renderer.py.

Validates: Requirements 12.1, 12.2, 12.3, 12.4
"""

# ---------------------------------------------------------------------------
# Template: low_tank_autofill_alert
# Trigger: Tank level < reorder_point (from TankForecastingAgent)
# Channels: Email, SMS
# Validates: Requirement 12.1
# ---------------------------------------------------------------------------

LOW_TANK_AUTOFILL_ALERT_TEMPLATES: list[dict] = [
    {
        "event_type": "low_tank_autofill_alert",
        "channel": "email",
        "subject_template": "Low Tank Alert — Delivery Scheduled for {tank_location}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Your tank at {tank_location} has reached {current_level_percent}% capacity.\n\n"
            "Based on current usage, we estimate approximately {estimated_days_to_empty} days "
            "until empty.\n\n"
            "A delivery has been scheduled for {scheduled_delivery_date}.\n\n"
            "If you have questions or need to adjust the delivery date, "
            "please contact our dispatch team.\n\n"
            "Thank you for being an auto-fill customer."
        ),
        "placeholders": [
            "customer_name",
            "tank_location",
            "current_level_percent",
            "estimated_days_to_empty",
            "scheduled_delivery_date",
        ],
    },
    {
        "event_type": "low_tank_autofill_alert",
        "channel": "sms",
        "subject_template": "Low Tank Alert — {tank_location}",
        "body_template": (
            "Hi {customer_name}, your tank at {tank_location} is at "
            "{current_level_percent}% (~{estimated_days_to_empty} days to empty). "
            "Delivery scheduled for {scheduled_delivery_date}."
        ),
        "placeholders": [
            "customer_name",
            "tank_location",
            "current_level_percent",
            "estimated_days_to_empty",
            "scheduled_delivery_date",
        ],
    },
]

# ---------------------------------------------------------------------------
# Template: past_due_invoice
# Trigger: Invoice status → overdue
# Channels: Email
# Validates: Requirement 12.2
# ---------------------------------------------------------------------------

PAST_DUE_INVOICE_TEMPLATES: list[dict] = [
    {
        "event_type": "past_due_invoice",
        "channel": "email",
        "subject_template": "Past Due Notice — Invoice {invoice_number}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "This is a reminder that invoice {invoice_number} for "
            "${amount_due_dollars} is now {days_past_due} days past due.\n\n"
            "Please submit payment at your earliest convenience using the "
            "link below:\n"
            "{payment_link}\n\n"
            "If you have already submitted payment, please disregard this "
            "notice. For questions regarding this invoice, please contact "
            "our billing department.\n\n"
            "Thank you."
        ),
        "placeholders": [
            "customer_name",
            "invoice_number",
            "amount_due_dollars",
            "days_past_due",
            "payment_link",
        ],
    },
]

# ---------------------------------------------------------------------------
# Template: delivery_completed
# Trigger: POD confirmed
# Channels: Email, SMS
# Validates: Requirement 12.3
# ---------------------------------------------------------------------------

DELIVERY_COMPLETED_TEMPLATES: list[dict] = [
    {
        "event_type": "delivery_completed",
        "channel": "email",
        "subject_template": "Delivery Complete — {product_name} to {customer_name}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Your fuel delivery has been completed. Here are the details:\n\n"
            "Delivery Date: {delivery_date}\n"
            "Product: {product_name}\n"
            "Gross Gallons: {gross_gallons}\n"
            "Net Gallons: {net_gallons}\n"
            "Unit Price: ${unit_price}/gal\n"
            "Total Amount: ${total_amount}\n"
            "PO Number: {PO_number}\n"
            "Driver: {driver_name}\n\n"
            "If you have any questions about this delivery, please contact "
            "our office.\n\n"
            "Thank you for your business."
        ),
        "placeholders": [
            "customer_name",
            "delivery_date",
            "product_name",
            "gross_gallons",
            "net_gallons",
            "unit_price",
            "total_amount",
            "PO_number",
            "driver_name",
        ],
    },
    {
        "event_type": "delivery_completed",
        "channel": "sms",
        "subject_template": "Delivery Complete — {product_name}",
        "body_template": (
            "Hi {customer_name}, your {product_name} delivery is complete. "
            "{gross_gallons} gross / {net_gallons} net gal on {delivery_date}. "
            "${unit_price}/gal, total ${total_amount}. PO: {PO_number}. Driver: {driver_name}."
        ),
        "placeholders": [
            "customer_name",
            "delivery_date",
            "product_name",
            "gross_gallons",
            "net_gallons",
            "unit_price",
            "total_amount",
            "PO_number",
            "driver_name",
        ],
    },
]

# ---------------------------------------------------------------------------
# Template: e_bol_delivery
# Trigger: Signed BOL PDF generated
# Channels: Email (with attachment)
# Validates: Requirement 12.4
# ---------------------------------------------------------------------------

E_BOL_DELIVERY_TEMPLATES: list[dict] = [
    {
        "event_type": "e_bol_delivery",
        "channel": "email",
        "subject_template": "Electronic Bill of Lading — Load {load_number}",
        "body_template": (
            "Dear {customer_name},\n\n"
            "Please find attached the signed Bill of Lading for your recent "
            "delivery.\n\n"
            "Delivery Summary:\n"
            "Load Number: {load_number}\n"
            "Product: {product}\n"
            "Gross Gallons: {gross_gallons}\n"
            "Net Gallons: {net_gallons}\n"
            "Terminal: {terminal}\n"
            "Driver: {driver}\n\n"
            "The signed BOL PDF is attached to this email for your records.\n\n"
            "If you have any questions, please contact our dispatch team.\n\n"
            "Thank you."
        ),
        "placeholders": [
            "customer_name",
            "load_number",
            "product",
            "gross_gallons",
            "net_gallons",
            "terminal",
            "driver",
        ],
        "has_attachment": True,
        "attachment_type": "signed_bol_pdf",
    },
]

# ---------------------------------------------------------------------------
# Combined registry of all fuel notification templates
# ---------------------------------------------------------------------------

FUEL_NOTIFICATION_TEMPLATES: list[dict] = (
    LOW_TANK_AUTOFILL_ALERT_TEMPLATES
    + PAST_DUE_INVOICE_TEMPLATES
    + DELIVERY_COMPLETED_TEMPLATES
    + E_BOL_DELIVERY_TEMPLATES
)
"""All fuel-specific notification templates combined.

This list can be imported by template_renderer.py and added to the
TEMPLATE_REGISTRY, or used by seed_data.py to initialize fuel templates
for a tenant.

Contains templates for:
- low_tank_autofill_alert (Email, SMS) — Validates: 12.1
- past_due_invoice (Email) — Validates: 12.2
- delivery_completed (Email, SMS) — Validates: 12.3
- e_bol_delivery (Email with attachment) — Validates: 12.4
"""

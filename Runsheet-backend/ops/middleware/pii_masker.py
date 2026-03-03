"""
PII Masker for the Ops Intelligence Layer.

Redacts personally identifiable information (phone numbers, email addresses,
and customer names) from API responses and AI tool outputs. Supports
role-based unmasking via the ``has_pii_access`` permission.

Masking rules (Requirement 22.3):
- Phone numbers: ``+XX-XXXX-XX34`` retaining last 2 digits
- Email addresses: ``***@***.com``
- Name fields (customer_name, recipient_name, sender_name): ``***``

Validates: Requirements 22.1, 22.3, 22.4
"""

import copy
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Fields that contain customer names and should be masked
NAME_FIELDS: set[str] = {"customer_name", "recipient_name", "sender_name"}

# Patterns for detecting PII in string values
PHONE_PATTERN: re.Pattern = re.compile(r"\+?\d[\d\s\-]{7,}\d")
EMAIL_PATTERN: re.Pattern = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class PIIMasker:
    """Redacts PII from response data based on access permissions."""

    PHONE_PATTERN = PHONE_PATTERN
    EMAIL_PATTERN = EMAIL_PATTERN
    NAME_FIELDS = NAME_FIELDS

    def mask_phone(self, phone: str) -> str:
        """
        Mask a phone number, retaining only the last 2 digits.

        Example: ``+1-555-123-4567`` → ``+XX-XXXX-XX67``

        Validates: Requirement 22.3
        """
        # Extract only digits from the phone string
        digits = re.findall(r"\d", phone)
        if len(digits) < 2:
            return "***"
        last_two = "".join(digits[-2:])
        return f"+XX-XXXX-XX{last_two}"

    def mask_email(self, email: str) -> str:
        """
        Mask an email address, preserving only the TLD.

        Example: ``john@example.com`` → ``***@***.com``

        Validates: Requirement 22.3
        """
        parts = email.rsplit(".", 1)
        if len(parts) == 2:
            tld = parts[1]
            return f"***@***.{tld}"
        return "***@***.com"

    def mask_response(self, data: Any, has_pii_access: bool = False) -> Any:
        """
        Recursively mask PII fields in a response dict/list.

        If ``has_pii_access`` is True, the data is returned unmodified.

        Validates: Requirements 22.1, 22.4
        """
        if has_pii_access:
            return data
        return self._mask_value(copy.deepcopy(data))

    def _mask_value(self, value: Any) -> Any:
        """Recursively walk and mask PII in nested structures."""
        if isinstance(value, dict):
            return self._mask_dict(value)
        if isinstance(value, list):
            return [self._mask_value(item) for item in value]
        if isinstance(value, str):
            return self._mask_string_value(value)
        return value

    def _mask_dict(self, d: dict) -> dict:
        """Mask PII fields in a dictionary."""
        for key, val in d.items():
            if key in self.NAME_FIELDS and isinstance(val, str):
                d[key] = "***"
            elif isinstance(val, str):
                d[key] = self._mask_string_field(key, val)
            elif isinstance(val, dict):
                d[key] = self._mask_dict(val)
            elif isinstance(val, list):
                d[key] = [self._mask_value(item) for item in val]
        return d

    def _mask_string_field(self, key: str, value: str) -> str:
        """Mask a string field value, checking for phone/email patterns."""
        # Check if the entire value matches a phone pattern
        if self.PHONE_PATTERN.fullmatch(value.strip()):
            return self.mask_phone(value)
        # Check if the entire value matches an email pattern
        if self.EMAIL_PATTERN.fullmatch(value.strip()):
            return self.mask_email(value)
        return value

    def _mask_string_value(self, value: str) -> str:
        """Mask phone/email patterns found in a standalone string value."""
        if self.PHONE_PATTERN.fullmatch(value.strip()):
            return self.mask_phone(value)
        if self.EMAIL_PATTERN.fullmatch(value.strip()):
            return self.mask_email(value)
        return value


def log_pii_access(
    user_id: str,
    tenant_id: str,
    fields_accessed: list[str],
    endpoint: str,
) -> None:
    """
    Log a PII access event for compliance audit.

    Validates: Requirement 22.5
    """
    logger.info(
        "PII access event: user_id=%s tenant_id=%s fields=%s endpoint=%s",
        user_id,
        tenant_id,
        ",".join(fields_accessed) if fields_accessed else "none",
        endpoint,
    )

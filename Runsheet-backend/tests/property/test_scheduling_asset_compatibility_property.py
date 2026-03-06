"""
Property-based tests for Asset-Type Compatibility.

# Feature: logistics-scheduling, Property 2: Asset-Type Compatibility

**Validates: Requirements 2.4, 2.5, 3.3**

For any (job_type, asset_type) pair:
- If asset_type is in JOB_ASSET_COMPATIBILITY[job_type], the assignment
  should be ACCEPTED.
- If asset_type is NOT in JOB_ASSET_COMPATIBILITY[job_type], the assignment
  should be REJECTED.
"""

from hypothesis import given, settings
from hypothesis.strategies import sampled_from

from scheduling.models import JobType, JOB_ASSET_COMPATIBILITY


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
_all_job_types = sampled_from(list(JobType))
_all_asset_types = sampled_from(["vehicle", "vessel", "equipment", "container"])


# ---------------------------------------------------------------------------
# Property 1 – Compatibility acceptance matches JOB_ASSET_COMPATIBILITY
# ---------------------------------------------------------------------------
class TestAssetTypeCompatibility:
    """**Validates: Requirements 2.4, 2.5, 3.3**"""

    @given(job_type=_all_job_types, asset_type=_all_asset_types)
    @settings(max_examples=200)
    def test_compatibility_property(self, job_type: JobType, asset_type: str):
        """
        For any (job_type, asset_type) pair, the assignment is compatible iff
        asset_type is in JOB_ASSET_COMPATIBILITY[job_type].

        **Validates: Requirements 2.4, 2.5, 3.3**
        """
        compatible_types = JOB_ASSET_COMPATIBILITY[job_type]
        is_compatible = asset_type in compatible_types

        if is_compatible:
            assert asset_type in compatible_types, (
                f"Asset type '{asset_type}' should be accepted for "
                f"{job_type.value} but is not in "
                f"JOB_ASSET_COMPATIBILITY[{job_type.value}]"
            )
        else:
            assert asset_type not in compatible_types, (
                f"Asset type '{asset_type}' should be rejected for "
                f"{job_type.value} but is listed in "
                f"JOB_ASSET_COMPATIBILITY[{job_type.value}]"
            )


# ---------------------------------------------------------------------------
# Property 2 – Every JobType is a key in JOB_ASSET_COMPATIBILITY
# ---------------------------------------------------------------------------
class TestAllJobTypesCovered:
    """**Validates: Requirements 2.4, 2.5, 3.3**"""

    @given(job_type=_all_job_types)
    @settings(max_examples=200)
    def test_every_job_type_is_a_key(self, job_type: JobType):
        """
        Every JobType value must appear as a key in JOB_ASSET_COMPATIBILITY,
        ensuring the compatibility map is complete.

        **Validates: Requirements 2.4, 2.5, 3.3**
        """
        assert job_type in JOB_ASSET_COMPATIBILITY, (
            f"JobType.{job_type.name} ({job_type.value}) is not a key in "
            f"JOB_ASSET_COMPATIBILITY"
        )


# ---------------------------------------------------------------------------
# Property 3 – No job type accepts all asset types
# ---------------------------------------------------------------------------
class TestNoJobTypeAcceptsAll:
    """**Validates: Requirements 2.4, 2.5, 3.3**"""

    @given(job_type=_all_job_types)
    @settings(max_examples=200)
    def test_no_job_type_accepts_all_asset_types(self, job_type: JobType):
        """
        No job type should accept every possible asset type — there must
        always be at least one incompatible asset type.

        **Validates: Requirements 2.4, 2.5, 3.3**
        """
        all_asset_types = {"vehicle", "vessel", "equipment", "container"}
        compatible = set(JOB_ASSET_COMPATIBILITY[job_type])

        assert compatible < all_asset_types, (
            f"JobType.{job_type.name} accepts all asset types "
            f"{compatible}, but should have at least one incompatible type"
        )


# ---------------------------------------------------------------------------
# Property 4 – Every compatible asset_type is a valid string
# ---------------------------------------------------------------------------
class TestCompatibleTypesAreValid:
    """**Validates: Requirements 2.4, 2.5, 3.3**"""

    @given(job_type=_all_job_types)
    @settings(max_examples=200)
    def test_compatible_types_are_valid_strings(self, job_type: JobType):
        """
        Every asset_type listed in JOB_ASSET_COMPATIBILITY must be a
        non-empty string from the known set of asset types.

        **Validates: Requirements 2.4, 2.5, 3.3**
        """
        valid_asset_types = {"vehicle", "vessel", "equipment", "container"}
        compatible = JOB_ASSET_COMPATIBILITY[job_type]

        for asset_type in compatible:
            assert isinstance(asset_type, str), (
                f"Compatible asset type for {job_type.value} is not a string: "
                f"{asset_type!r}"
            )
            assert len(asset_type) > 0, (
                f"Compatible asset type for {job_type.value} is an empty string"
            )
            assert asset_type in valid_asset_types, (
                f"Compatible asset type '{asset_type}' for {job_type.value} "
                f"is not in the known set of asset types: {valid_asset_types}"
            )

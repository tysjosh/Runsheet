"""
Configuration management for Runsheet Backend.

This module provides centralized configuration loading and validation using Pydantic settings.
All secrets are loaded from environment variables or .env files.

Requirements:
- 1.1: Load all secrets from environment variables or secrets manager
- 1.3: Fail startup with descriptive error message listing missing values
- 1.4: Support environment-specific configuration files for development, staging, and production
- 1.5: Validate all required fields and their formats before accepting requests
"""

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional, List, Tuple

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Supported deployment environments."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


def _detect_environment() -> Environment:
    """
    Detect the current environment from the ENVIRONMENT variable.
    
    Returns:
        Environment: The detected environment, defaults to DEVELOPMENT if not set.
    """
    env_value = os.environ.get("ENVIRONMENT", "development").lower().strip()
    try:
        return Environment(env_value)
    except ValueError:
        # If invalid value, default to development
        return Environment.DEVELOPMENT


def _get_env_files(environment: Environment) -> Tuple[str, ...]:
    """
    Get the list of .env files to load for the given environment.
    
    Files are loaded in order, with later files overriding earlier ones.
    The base .env file is loaded first, then the environment-specific file.
    
    Args:
        environment: The target environment.
        
    Returns:
        Tuple of .env file paths to load.
    """
    env_file_map = {
        Environment.DEVELOPMENT: ".env.development",
        Environment.STAGING: ".env.staging",
        Environment.PRODUCTION: ".env.production",
    }
    
    # Base .env file is always loaded first (if it exists)
    # Then environment-specific file overrides
    env_specific_file = env_file_map.get(environment, ".env.development")
    
    return (".env", env_specific_file)


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    
    All required fields must be provided via environment variables or .env file.
    The application will fail to start if required fields are missing or invalid.
    
    Environment-specific configuration is supported through:
    - .env.development - Development environment settings
    - .env.staging - Staging environment settings  
    - .env.production - Production environment settings
    
    The ENVIRONMENT variable determines which file to load.
    """
    
    # Environment
    environment: Environment = Field(
        default=Environment.DEVELOPMENT,
        description="Deployment environment (development, staging, production)"
    )
    
    # Elasticsearch Configuration
    elastic_endpoint: str = Field(
        ...,
        description="Elasticsearch endpoint URL"
    )
    elastic_api_key: str = Field(
        ...,
        description="Elasticsearch API key for authentication"
    )
    
    # Google Cloud / Gemini Configuration
    google_cloud_project: str = Field(
        ...,
        description="Google Cloud Platform project ID"
    )
    google_cloud_location: str = Field(
        default="us-central1",
        description="Google Cloud region for Vertex AI"
    )
    google_application_credentials: Optional[str] = Field(
        default=None,
        description="Path to Google Cloud service account credentials file"
    )
    
    # Session Store Configuration
    session_store_type: str = Field(
        default="redis",
        description="Session store type: 'redis' or 'dynamodb'"
    )
    redis_url: Optional[str] = Field(
        default=None,
        description="Redis connection URL for session storage"
    )
    dynamodb_table: Optional[str] = Field(
        default=None,
        description="DynamoDB table name for session storage"
    )
    session_ttl_hours: int = Field(
        default=24,
        ge=1,
        le=168,  # Max 1 week
        description="Session time-to-live in hours"
    )
    
    # Rate Limiting Configuration
    rate_limit_requests_per_minute: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Maximum API requests per minute per IP"
    )
    rate_limit_ai_requests_per_minute: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Maximum AI chat requests per minute per IP"
    )
    
    # Observability Configuration
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )
    otel_endpoint: Optional[str] = Field(
        default=None,
        description="OpenTelemetry collector endpoint URL"
    )
    otel_service_name: str = Field(
        default="runsheet-backend",
        description="Service name for OpenTelemetry traces"
    )
    
    # CORS Configuration
    cors_origins: List[str] = Field(
        default=["http://localhost:3000"],
        description="Allowed CORS origins"
    )
    
    # Note: model_config is set dynamically via create_settings_for_environment()
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    
    @field_validator("elastic_endpoint")
    @classmethod
    def validate_elastic_endpoint(cls, v: str) -> str:
        """Validate that elastic_endpoint is not empty and is a valid URL format."""
        if not v or not v.strip():
            raise ValueError("elastic_endpoint cannot be empty")
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("elastic_endpoint must be a valid HTTP/HTTPS URL")
        return v
    
    @field_validator("elastic_api_key")
    @classmethod
    def validate_elastic_api_key(cls, v: str) -> str:
        """Validate that elastic_api_key is not empty."""
        if not v or not v.strip():
            raise ValueError("elastic_api_key cannot be empty")
        return v.strip()
    
    @field_validator("google_cloud_project")
    @classmethod
    def validate_google_cloud_project(cls, v: str) -> str:
        """Validate that google_cloud_project is not empty and follows GCP naming conventions."""
        if not v or not v.strip():
            raise ValueError("google_cloud_project cannot be empty")
        v = v.strip()
        # GCP project IDs must be 6-30 characters, lowercase letters, digits, and hyphens
        if len(v) < 6 or len(v) > 30:
            raise ValueError("google_cloud_project must be between 6 and 30 characters")
        return v
    
    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate that log_level is a valid logging level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.strip().upper()
        if v not in valid_levels:
            raise ValueError(f"log_level must be one of: {', '.join(valid_levels)}")
        return v
    
    @field_validator("session_store_type")
    @classmethod
    def validate_session_store_type(cls, v: str) -> str:
        """Validate that session_store_type is either 'redis' or 'dynamodb'."""
        v = v.strip().lower()
        if v not in {"redis", "dynamodb"}:
            raise ValueError("session_store_type must be 'redis' or 'dynamodb'")
        return v
    
    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, v: List[str]) -> List[str]:
        """
        Validate CORS origins format and reject wildcard patterns.
        
        Validates: Requirement 14.4 - CORS restrictions allowing only configured frontend domains
        """
        validated_origins = []
        for origin in v:
            origin = origin.strip()
            # Reject wildcard patterns
            if origin == "*" or "*" in origin:
                raise ValueError(
                    f"Wildcard patterns are not allowed in CORS origins: {origin}. "
                    "Specify exact frontend domains for security."
                )
            # Validate URL format
            if not (origin.startswith("http://") or origin.startswith("https://")):
                raise ValueError(
                    f"Invalid CORS origin format: {origin}. "
                    "Must start with http:// or https://"
                )
            validated_origins.append(origin)
        return validated_origins
    
    @model_validator(mode="after")
    def validate_session_store_config(self) -> "Settings":
        """Validate that the appropriate session store URL/table is provided."""
        if self.session_store_type == "redis" and not self.redis_url:
            # In development, redis_url is optional (can use in-memory fallback)
            if self.environment != Environment.DEVELOPMENT:
                raise ValueError(
                    "redis_url is required when session_store_type is 'redis' "
                    "in non-development environments"
                )
        elif self.session_store_type == "dynamodb" and not self.dynamodb_table:
            if self.environment != Environment.DEVELOPMENT:
                raise ValueError(
                    "dynamodb_table is required when session_store_type is 'dynamodb' "
                    "in non-development environments"
                )
        return self


class ConfigurationError(Exception):
    """Exception raised when configuration validation fails."""
    
    def __init__(self, message: str, missing_fields: Optional[List[str]] = None, 
                 invalid_fields: Optional[dict] = None):
        self.message = message
        self.missing_fields = missing_fields or []
        self.invalid_fields = invalid_fields or {}
        super().__init__(self.format_error_message())
    
    def format_error_message(self) -> str:
        """Format a descriptive error message listing all issues."""
        parts = [self.message]
        
        if self.missing_fields:
            parts.append(f"\nMissing required fields: {', '.join(self.missing_fields)}")
        
        if self.invalid_fields:
            invalid_parts = [f"  - {field}: {error}" for field, error in self.invalid_fields.items()]
            parts.append(f"\nInvalid field values:\n" + "\n".join(invalid_parts))
        
        return "".join(parts)


def create_settings_for_environment(environment: Optional[Environment] = None) -> Settings:
    """
    Factory function to create Settings for a specific environment.
    
    This function detects the environment from the ENVIRONMENT variable (if not provided)
    and loads the appropriate environment-specific .env file.
    
    Args:
        environment: Optional environment override. If not provided, detected from
                    ENVIRONMENT variable.
    
    Returns:
        Settings: Validated settings for the specified environment.
        
    Raises:
        ConfigurationError: If required settings are missing or invalid.
    """
    # Detect environment if not provided
    if environment is None:
        environment = _detect_environment()
    
    # Get the env files to load for this environment
    env_files = _get_env_files(environment)
    
    # Filter to only existing files
    existing_env_files = []
    for env_file in env_files:
        if Path(env_file).exists():
            existing_env_files.append(env_file)
    
    # If no env files exist, use the default tuple (pydantic will handle missing files)
    if not existing_env_files:
        existing_env_files = list(env_files)
    
    try:
        # Create a dynamic Settings class with the correct env_file configuration
        class EnvironmentSettings(Settings):
            model_config = SettingsConfigDict(
                env_file=tuple(existing_env_files),
                env_file_encoding="utf-8",
                case_sensitive=False,
                extra="ignore"
            )
        
        return EnvironmentSettings()
    except Exception as e:
        # Parse Pydantic validation errors to provide better error messages
        missing_fields = []
        invalid_fields = {}
        
        # Extract field-level errors from Pydantic ValidationError
        if hasattr(e, 'errors'):
            for error in e.errors():
                field_name = '.'.join(str(loc) for loc in error.get('loc', []))
                error_type = error.get('type', '')
                error_msg = error.get('msg', str(error))
                
                if error_type == 'missing':
                    missing_fields.append(field_name)
                else:
                    invalid_fields[field_name] = error_msg
        
        raise ConfigurationError(
            f"Failed to load configuration for environment '{environment.value}'",
            missing_fields=missing_fields,
            invalid_fields=invalid_fields
        ) from e


# Global settings cache
_settings_cache: Optional[Settings] = None


def get_settings() -> Settings:
    """
    Get the application settings singleton.
    
    Settings are loaded once and cached for subsequent calls.
    The environment is detected from the ENVIRONMENT variable.
    
    Returns:
        Settings: The validated application settings.
        
    Raises:
        ConfigurationError: If required settings are missing or invalid.
    """
    global _settings_cache
    
    if _settings_cache is None:
        _settings_cache = create_settings_for_environment()
    
    return _settings_cache


def clear_settings_cache() -> None:
    """
    Clear the settings cache.
    
    This is primarily useful for testing to allow reloading settings
    with different environment variables.
    """
    global _settings_cache
    _settings_cache = None


def validate_startup() -> None:
    """
    Validate all required settings at application startup.
    
    This function should be called during application initialization to ensure
    all required configuration is present and valid before accepting requests.
    
    Raises:
        ConfigurationError: If any required settings are missing or invalid.
    """
    settings = get_settings()
    
    # Additional startup validations
    validation_errors = {}
    
    # Validate Google Cloud credentials file exists if specified
    if settings.google_application_credentials:
        if not Path(settings.google_application_credentials).exists():
            validation_errors["google_application_credentials"] = (
                f"Credentials file not found: {settings.google_application_credentials}"
            )
    
    # Validate CORS origins format and ensure no wildcards in production
    # Validates: Requirement 14.4 - CORS restrictions allowing only configured frontend domains
    for i, origin in enumerate(settings.cors_origins):
        # Check for valid URL format
        if not (origin.startswith("http://") or origin.startswith("https://")):
            validation_errors[f"cors_origins[{i}]"] = (
                f"Invalid origin format: {origin}. Must start with http:// or https://"
            )
        # Reject wildcard patterns in any environment
        if origin == "*" or "*" in origin:
            validation_errors[f"cors_origins[{i}]"] = (
                f"Wildcard patterns are not allowed in CORS origins: {origin}. "
                "Specify exact frontend domains for security."
            )
    
    # In production, ensure CORS origins are explicitly configured (not just localhost)
    if settings.environment == Environment.PRODUCTION:
        localhost_only = all(
            "localhost" in origin or "127.0.0.1" in origin 
            for origin in settings.cors_origins
        )
        if localhost_only:
            validation_errors["cors_origins"] = (
                "Production environment requires non-localhost CORS origins. "
                "Configure your production frontend domain(s)."
            )
    
    if validation_errors:
        raise ConfigurationError(
            "Configuration validation failed during startup",
            invalid_fields=validation_errors
        )


def get_environment_info() -> dict:
    """
    Get information about the current environment configuration.
    
    Returns:
        dict: Information about the detected environment and loaded config files.
    """
    environment = _detect_environment()
    env_files = _get_env_files(environment)
    
    existing_files = [f for f in env_files if Path(f).exists()]
    
    return {
        "environment": environment.value,
        "env_files_checked": list(env_files),
        "env_files_loaded": existing_files,
    }

"""
Unit tests for the configuration settings module.

Tests cover:
- Valid configuration loading
- Missing required fields validation
- Invalid field format validation
- Environment-specific validation
"""

import os
import pytest
from unittest.mock import patch

from config.settings import (
    Settings,
    Environment,
    ConfigurationError,
    get_settings,
    validate_startup,
    clear_settings_cache,
)


class TestSettings:
    """Tests for the Settings class."""
    
    @pytest.fixture
    def valid_env_vars(self):
        """Provide valid environment variables for testing."""
        return {
            "ELASTIC_ENDPOINT": "https://elasticsearch.example.com:9200",
            "ELASTIC_API_KEY": "test-api-key-12345",
            "GOOGLE_CLOUD_PROJECT": "test-project-id",
            "ENVIRONMENT": "development",
        }
    
    def test_valid_configuration_loads_successfully(self, valid_env_vars):
        """Test that valid configuration loads without errors."""
        with patch.dict(os.environ, valid_env_vars, clear=True):
            settings = Settings()
            
            assert settings.elastic_endpoint == "https://elasticsearch.example.com:9200"
            assert settings.elastic_api_key == "test-api-key-12345"
            assert settings.google_cloud_project == "test-project-id"
            assert settings.environment == Environment.DEVELOPMENT
    
    def test_default_values_are_applied(self, valid_env_vars):
        """Test that default values are correctly applied."""
        with patch.dict(os.environ, valid_env_vars, clear=True):
            settings = Settings()
            
            assert settings.google_cloud_location == "us-central1"
            assert settings.session_store_type == "redis"
            assert settings.session_ttl_hours == 24
            assert settings.rate_limit_requests_per_minute == 100
            assert settings.rate_limit_ai_requests_per_minute == 10
            assert settings.log_level == "INFO"
            assert settings.otel_service_name == "runsheet-backend"
            assert settings.cors_origins == ["http://localhost:3000"]
    
    def test_missing_elastic_endpoint_raises_error(self, valid_env_vars):
        """Test that missing elastic_endpoint raises validation error."""
        env_vars = {k: v for k, v in valid_env_vars.items() if k != "ELASTIC_ENDPOINT"}
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            # Pydantic should raise a validation error for missing required field
            assert "elastic_endpoint" in str(exc_info.value).lower()
    
    def test_missing_elastic_api_key_raises_error(self, valid_env_vars):
        """Test that missing elastic_api_key raises validation error."""
        env_vars = {k: v for k, v in valid_env_vars.items() if k != "ELASTIC_API_KEY"}
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "elastic_api_key" in str(exc_info.value).lower()
    
    def test_empty_elastic_endpoint_raises_error(self, valid_env_vars):
        """Test that empty elastic_endpoint raises validation error."""
        env_vars = {**valid_env_vars, "ELASTIC_ENDPOINT": "   "}
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "elastic_endpoint" in str(exc_info.value).lower()
    
    def test_invalid_elastic_endpoint_url_raises_error(self, valid_env_vars):
        """Test that invalid elastic_endpoint URL format raises error."""
        env_vars = {**valid_env_vars, "ELASTIC_ENDPOINT": "not-a-valid-url"}
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "url" in str(exc_info.value).lower() or "http" in str(exc_info.value).lower()
    
    def test_invalid_log_level_raises_error(self, valid_env_vars):
        """Test that invalid log_level raises validation error."""
        env_vars = {**valid_env_vars, "LOG_LEVEL": "INVALID_LEVEL"}
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "log_level" in str(exc_info.value).lower()
    
    def test_valid_log_levels_accepted(self, valid_env_vars):
        """Test that all valid log levels are accepted."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        
        for level in valid_levels:
            env_vars = {**valid_env_vars, "LOG_LEVEL": level}
            with patch.dict(os.environ, env_vars, clear=True):
                settings = Settings()
                assert settings.log_level == level
    
    def test_invalid_session_store_type_raises_error(self, valid_env_vars):
        """Test that invalid session_store_type raises validation error."""
        env_vars = {**valid_env_vars, "SESSION_STORE_TYPE": "invalid_store"}
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "session_store_type" in str(exc_info.value).lower()
    
    def test_redis_session_store_requires_url_in_production(self, valid_env_vars):
        """Test that redis session store requires URL in production."""
        env_vars = {
            **valid_env_vars,
            "ENVIRONMENT": "production",
            "SESSION_STORE_TYPE": "redis",
            # No REDIS_URL provided
        }
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "redis_url" in str(exc_info.value).lower()
    
    def test_redis_session_store_optional_in_development(self, valid_env_vars):
        """Test that redis URL is optional in development."""
        env_vars = {
            **valid_env_vars,
            "ENVIRONMENT": "development",
            "SESSION_STORE_TYPE": "redis",
            # No REDIS_URL provided - should be OK in development
        }
        
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.session_store_type == "redis"
            assert settings.redis_url is None
    
    def test_environment_enum_values(self, valid_env_vars):
        """Test that all environment enum values are accepted."""
        for env in ["development", "staging", "production"]:
            env_vars = {**valid_env_vars, "ENVIRONMENT": env}
            if env != "development":
                env_vars["DATABASE_URL"] = (
                    "postgresql+psycopg://u:p@db.internal:5432/runsheet"
                )
            # For non-development environments, redis_url is required
            if env != "development":
                env_vars["REDIS_URL"] = "redis://localhost:6379"
                # auth_provider is 'supertokens' (the sole provider after the
                # hard cutover), so a non-development environment must supply
                # managed-core config to satisfy the fail-closed validator
                # (Req 10.3).
                env_vars["SUPERTOKENS_CONNECTION_URI"] = "https://core.supertokens.example.com"
                env_vars["SUPERTOKENS_API_KEY"] = "st-managed-core-api-key"
                # The agent stack needs a real LLM credential outside
                # development; GOOGLE_CLOUD_PROJECT does not satisfy the
                # default 'gemini' provider.
                env_vars["GEMINI_API_KEY"] = "test-gemini-key"
            if env == "production":
                env_vars["CORS_ORIGINS"] = '["https://app.runsheet.com"]'
            with patch.dict(os.environ, env_vars, clear=True):
                settings = Settings()
                assert settings.environment.value == env
    
    def test_cors_origins_list_parsing(self, valid_env_vars):
        """Test that CORS origins can be provided as a list."""
        env_vars = {
            **valid_env_vars,
            "CORS_ORIGINS": '["http://localhost:3000", "https://app.example.com"]',
        }
        
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert "http://localhost:3000" in settings.cors_origins
            assert "https://app.example.com" in settings.cors_origins
    
    def test_cors_origins_rejects_wildcard(self, valid_env_vars):
        """Test that CORS origins reject wildcard patterns (Requirement 14.4)."""
        env_vars = {
            **valid_env_vars,
            "CORS_ORIGINS": '["*"]',
        }
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "wildcard" in str(exc_info.value).lower()
    
    def test_cors_origins_rejects_wildcard_subdomain(self, valid_env_vars):
        """Test that CORS origins reject wildcard subdomain patterns (Requirement 14.4)."""
        env_vars = {
            **valid_env_vars,
            "CORS_ORIGINS": '["https://*.example.com"]',
        }
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "wildcard" in str(exc_info.value).lower()
    
    def test_cors_origins_rejects_invalid_url_format(self, valid_env_vars):
        """Test that CORS origins reject invalid URL formats."""
        env_vars = {
            **valid_env_vars,
            "CORS_ORIGINS": '["example.com"]',  # Missing http:// or https://
        }
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            
            assert "http" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()
    

class TestSuperTokensSettings:
    """Tests for the SuperTokens auth-migration settings (Task 1.2)."""

    @pytest.fixture
    def valid_env_vars(self):
        """Provide valid environment variables for testing."""
        return {
            "ELASTIC_ENDPOINT": "https://elasticsearch.example.com:9200",
            "ELASTIC_API_KEY": "test-api-key-12345",
            "GOOGLE_CLOUD_PROJECT": "test-project-id",
            "ENVIRONMENT": "development",
        }

    @pytest.fixture
    def non_dev_env_vars(self, valid_env_vars):
        """Base env vars for a non-development (staging) environment."""
        return {
            **valid_env_vars,
            "ENVIRONMENT": "staging",
            # Required outside development: without it the persistence layer is
            # dormant and invoices finalize with no invoice_number.
            "DATABASE_URL": "postgresql+psycopg://u:p@db.internal:5432/runsheet",
            "REDIS_URL": "redis://localhost:6379",
            # Required outside development: the agents authenticate to Google AI
            # Studio by API key, and an unset value became an empty key that
            # failed on every request instead of at startup.
            "GEMINI_API_KEY": "test-gemini-key",
        }

    def test_supertokens_defaults(self, valid_env_vars):
        """Test that SuperTokens fields use their documented defaults."""
        with patch.dict(os.environ, valid_env_vars, clear=True):
            settings = Settings()

            # SuperTokens is the sole supported provider after the hard cutover
            # (Req 3.4, 9.1).
            assert settings.auth_provider == "supertokens"
            assert settings.supertokens_connection_uri == ""
            assert settings.supertokens_api_key == ""
            assert settings.supertokens_api_domain == "http://localhost:8080"
            assert settings.supertokens_website_domain == "http://localhost:3000"
            assert settings.supertokens_cookie_domain is None
            assert settings.session_lifetime_seconds == 3600
            assert settings.password_min_length == 8

    def test_auth_provider_accepts_supertokens(self, valid_env_vars):
        """Test that auth_provider accepts the sole supported value."""
        env_vars = {**valid_env_vars, "AUTH_PROVIDER": "supertokens"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.auth_provider == "supertokens"

    def test_auth_provider_rejects_legacy_and_dual(self, valid_env_vars):
        """Test that the retired legacy/dual providers are rejected — only
        supertokens remains after the hard cutover (Req 3.4, 9.1)."""
        for value in ["legacy", "dual"]:
            env_vars = {**valid_env_vars, "AUTH_PROVIDER": value}
            with patch.dict(os.environ, env_vars, clear=True):
                with pytest.raises(Exception) as exc_info:
                    Settings()
                assert "auth_provider" in str(exc_info.value).lower()

    def test_auth_provider_rejects_invalid_value(self, valid_env_vars):
        """Test that an unknown auth_provider value is rejected."""
        env_vars = {**valid_env_vars, "AUTH_PROVIDER": "oauth"}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            assert "auth_provider" in str(exc_info.value).lower()

    def test_session_lifetime_seconds_rejects_below_minimum(self, valid_env_vars):
        """Test that session_lifetime_seconds enforces the ge=60 bound."""
        env_vars = {**valid_env_vars, "SESSION_LIFETIME_SECONDS": "30"}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            assert "session_lifetime_seconds" in str(exc_info.value).lower()

    def test_password_min_length_bounds(self, valid_env_vars):
        """Test that password_min_length enforces ge=8 and le=128 bounds."""
        for bad_value in ["4", "200"]:
            env_vars = {**valid_env_vars, "PASSWORD_MIN_LENGTH": bad_value}
            with patch.dict(os.environ, env_vars, clear=True):
                with pytest.raises(Exception) as exc_info:
                    Settings()
                assert "password_min_length" in str(exc_info.value).lower()

    def test_supertokens_config_optional_in_development(self, valid_env_vars):
        """Test that SuperTokens config is optional in development."""
        env_vars = {**valid_env_vars, "ENVIRONMENT": "development", "AUTH_PROVIDER": "supertokens"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.auth_provider == "supertokens"
            assert settings.supertokens_connection_uri == ""

    def test_supertokens_config_optional_in_test(self, valid_env_vars):
        """Test that SuperTokens config is optional in the test environment."""
        env_vars = {
            **valid_env_vars,
            "ENVIRONMENT": "test",
            "REDIS_URL": "redis://localhost:6379",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.auth_provider == "supertokens"

    def test_supertokens_provider_requires_config_in_non_dev(self, non_dev_env_vars):
        """Test fail-closed: supertokens mode in staging needs SuperTokens config (Req 10.3)."""
        env_vars = {**non_dev_env_vars}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            assert "supertokens" in str(exc_info.value).lower()

    def test_error_names_only_the_missing_supertokens_value(self, non_dev_env_vars):
        """Test that the error names only the missing value when one is present."""
        env_vars = {
            **non_dev_env_vars,
            "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com",
            # api_key intentionally omitted
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            message = str(exc_info.value)
            assert "supertokens_api_key" in message
            assert "supertokens_connection_uri" not in message

    def test_supertokens_config_present_passes_in_non_dev(self, non_dev_env_vars):
        """Test that providing both required values passes validation in staging."""
        env_vars = {
            **non_dev_env_vars,
            "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com",
            "SUPERTOKENS_API_KEY": "st-managed-core-api-key",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.auth_provider == "supertokens"
            assert settings.supertokens_connection_uri == "https://core.supertokens.example.com"
            assert settings.supertokens_api_key == "st-managed-core-api-key"


class TestConfigurationError:
    """Tests for the ConfigurationError class."""
    
    def test_error_message_with_missing_fields(self):
        """Test error message formatting with missing fields."""
        error = ConfigurationError(
            "Configuration failed",
            missing_fields=["field1", "field2"]
        )
        
        message = str(error)
        assert "Configuration failed" in message
        assert "field1" in message
        assert "field2" in message
        assert "Missing required fields" in message
    
    def test_error_message_with_invalid_fields(self):
        """Test error message formatting with invalid fields."""
        error = ConfigurationError(
            "Configuration failed",
            invalid_fields={"field1": "must be positive", "field2": "invalid format"}
        )
        
        message = str(error)
        assert "Configuration failed" in message
        assert "field1" in message
        assert "must be positive" in message
        assert "field2" in message
        assert "invalid format" in message
    
    def test_error_message_with_both_missing_and_invalid(self):
        """Test error message with both missing and invalid fields."""
        error = ConfigurationError(
            "Configuration failed",
            missing_fields=["required_field"],
            invalid_fields={"other_field": "bad value"}
        )
        
        message = str(error)
        assert "required_field" in message
        assert "other_field" in message
        assert "bad value" in message


class TestGetSettings:
    """Tests for the get_settings function."""
    
    @pytest.fixture
    def valid_env_vars(self):
        """Provide valid environment variables for testing."""
        return {
            "ELASTIC_ENDPOINT": "https://elasticsearch.example.com:9200",
            "ELASTIC_API_KEY": "test-api-key-12345",
            "GOOGLE_CLOUD_PROJECT": "test-project-id",
        }
    
    def test_get_settings_returns_settings_instance(self, valid_env_vars):
        """Test that get_settings returns a Settings instance."""
        # Clear the cache first
        clear_settings_cache()
        
        with patch.dict(os.environ, valid_env_vars, clear=True):
            settings = get_settings()
            assert isinstance(settings, Settings)
    
    def test_get_settings_raises_configuration_error_on_invalid_config(self):
        """Test that get_settings raises ConfigurationError on invalid config."""
        # Clear the cache first
        clear_settings_cache()
        
        # Use invalid values that will fail validation
        with patch.dict(os.environ, {
            "ELASTIC_ENDPOINT": "",  # Empty endpoint should fail
            "ELASTIC_API_KEY": "test-key",
            "GOOGLE_CLOUD_PROJECT": "test-project-id",
        }, clear=True):
            with pytest.raises(ConfigurationError):
                get_settings()


class TestValidateStartup:
    """Tests for the validate_startup function."""
    
    @pytest.fixture
    def valid_env_vars(self):
        """Provide valid environment variables for testing."""
        return {
            "ELASTIC_ENDPOINT": "https://elasticsearch.example.com:9200",
            "ELASTIC_API_KEY": "test-api-key-12345",
            "GOOGLE_CLOUD_PROJECT": "test-project-id",
        }
    
    def test_validate_startup_succeeds_with_valid_config(self, valid_env_vars):
        """Test that validate_startup succeeds with valid configuration."""
        # Clear the cache first
        clear_settings_cache()
        
        with patch.dict(os.environ, valid_env_vars, clear=True):
            # Should not raise
            validate_startup()
    
    def test_validate_startup_fails_with_invalid_credentials_path(self, valid_env_vars):
        """Test that validate_startup fails when credentials file doesn't exist."""
        # Clear the cache first
        clear_settings_cache()
        
        env_vars = {
            **valid_env_vars,
            "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/path/credentials.json"
        }
        
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(ConfigurationError) as exc_info:
                validate_startup()
            
            assert "credentials" in str(exc_info.value).lower()


class TestEnvironmentSpecificConfiguration:
    """Tests for environment-specific configuration loading (Requirement 1.4)."""
    
    @pytest.fixture
    def valid_env_vars(self):
        """Provide valid environment variables for testing."""
        return {
            "ELASTIC_ENDPOINT": "https://elasticsearch.example.com:9200",
            "ELASTIC_API_KEY": "test-api-key-12345",
            "GOOGLE_CLOUD_PROJECT": "test-project-id",
        }
    
    def test_detect_environment_from_env_var(self, valid_env_vars):
        """Test that environment is detected from ENVIRONMENT variable."""
        from config.settings import _detect_environment
        
        # Test development
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
            assert _detect_environment() == Environment.DEVELOPMENT
        
        # Test staging
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}, clear=True):
            assert _detect_environment() == Environment.STAGING
        
        # Test production
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=True):
            assert _detect_environment() == Environment.PRODUCTION
    
    def test_detect_environment_defaults_to_development(self):
        """Test that environment defaults to development when not set."""
        from config.settings import _detect_environment
        
        with patch.dict(os.environ, {}, clear=True):
            assert _detect_environment() == Environment.DEVELOPMENT
    
    def test_detect_environment_handles_invalid_value(self):
        """Test that invalid environment value defaults to development."""
        from config.settings import _detect_environment
        
        with patch.dict(os.environ, {"ENVIRONMENT": "invalid_env"}, clear=True):
            assert _detect_environment() == Environment.DEVELOPMENT
    
    def test_get_env_files_for_development(self):
        """Test that correct env files are returned for development."""
        from config.settings import _get_env_files
        
        env_files = _get_env_files(Environment.DEVELOPMENT)
        assert ".env" in env_files
        assert ".env.development" in env_files
    
    def test_get_env_files_for_staging(self):
        """Test that correct env files are returned for staging."""
        from config.settings import _get_env_files
        
        env_files = _get_env_files(Environment.STAGING)
        assert ".env" in env_files
        assert ".env.staging" in env_files
    
    def test_get_env_files_for_production(self):
        """Test that correct env files are returned for production."""
        from config.settings import _get_env_files
        
        env_files = _get_env_files(Environment.PRODUCTION)
        assert ".env" in env_files
        assert ".env.production" in env_files
    
    def test_create_settings_for_environment_development(self, valid_env_vars):
        """Test creating settings for development environment."""
        from config.settings import create_settings_for_environment
        
        with patch.dict(os.environ, valid_env_vars, clear=True):
            settings = create_settings_for_environment(Environment.DEVELOPMENT)
            assert isinstance(settings, Settings)
    
    def test_create_settings_for_environment_staging(self, valid_env_vars):
        """Test creating settings for staging environment."""
        from config.settings import create_settings_for_environment
        
        env_vars = {**valid_env_vars, "DATABASE_URL": "postgresql+psycopg://u:p@db.internal:5432/runsheet", "REDIS_URL": "redis://localhost:6379", "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com", "SUPERTOKENS_API_KEY": "st-managed-core-api-key", "GEMINI_API_KEY": "test-gemini-key"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = create_settings_for_environment(Environment.STAGING)
            assert isinstance(settings, Settings)
    
    def test_create_settings_for_environment_production(self, valid_env_vars):
        """Test creating settings for production environment."""
        from config.settings import create_settings_for_environment
        
        env_vars = {**valid_env_vars, "DATABASE_URL": "postgresql+psycopg://u:p@db.internal:5432/runsheet", "REDIS_URL": "redis://localhost:6379", "CORS_ORIGINS": '["https://app.runsheet.com"]', "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com", "SUPERTOKENS_API_KEY": "st-managed-core-api-key", "GEMINI_API_KEY": "test-gemini-key"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = create_settings_for_environment(Environment.PRODUCTION)
            assert isinstance(settings, Settings)
    
    def test_create_settings_auto_detects_environment(self, valid_env_vars):
        """Test that create_settings_for_environment auto-detects environment."""
        from config.settings import create_settings_for_environment
        
        env_vars = {**valid_env_vars, "ENVIRONMENT": "staging", "DATABASE_URL": "postgresql+psycopg://u:p@db.internal:5432/runsheet", "REDIS_URL": "redis://localhost:6379", "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com", "SUPERTOKENS_API_KEY": "st-managed-core-api-key", "GEMINI_API_KEY": "test-gemini-key"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = create_settings_for_environment()
            assert settings.environment == Environment.STAGING
    
    def test_get_environment_info(self, valid_env_vars):
        """Test get_environment_info returns correct information."""
        from config.settings import get_environment_info
        
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}, clear=True):
            info = get_environment_info()
            assert info["environment"] == "staging"
            assert ".env" in info["env_files_checked"]
            assert ".env.staging" in info["env_files_checked"]
    
    def test_clear_settings_cache_allows_reload(self, valid_env_vars):
        """Test that clearing cache allows settings to be reloaded."""
        clear_settings_cache()
        
        # Load settings with one set of values
        env_vars_1 = {**valid_env_vars, "LOG_LEVEL": "DEBUG"}
        with patch.dict(os.environ, env_vars_1, clear=True):
            settings1 = get_settings()
            assert settings1.log_level == "DEBUG"
        
        # Clear cache and reload with different values
        clear_settings_cache()
        
        env_vars_2 = {**valid_env_vars, "LOG_LEVEL": "ERROR"}
        with patch.dict(os.environ, env_vars_2, clear=True):
            settings2 = get_settings()
            assert settings2.log_level == "ERROR"


class TestSuperTokensMigrationValidators:
    """Unit tests for the SuperTokens migration settings validators (Task 1.3).

    Covers the fail-closed migration validator and field bounds:
      - missing SuperTokens config in production raises a descriptive error
        naming the missing values (Req 10.3, 6.7);
      - supertokens in development tolerates missing SuperTokens config
        (Req 10.3);
      - session_lifetime_seconds bounds (Req 2.7);
      - password_min_length bounds (Req 1.7).
    """

    @pytest.fixture
    def dev_env_vars(self):
        """Minimal valid environment variables for a development environment."""
        return {
            "ELASTIC_ENDPOINT": "https://elasticsearch.example.com:9200",
            "ELASTIC_API_KEY": "test-api-key-12345",
            "GOOGLE_CLOUD_PROJECT": "test-project-id",
            "ENVIRONMENT": "development",
        }

    @pytest.fixture
    def prod_env_vars(self, dev_env_vars):
        """Base env vars for a production environment.

        Production additionally requires redis_url, the Dinee webhook secret,
        and non-localhost CORS origins, so a Settings() construction reaches
        (and isolates) the SuperTokens migration validator rather than failing
        on an unrelated production check.
        """
        return {
            **dev_env_vars,
            "ENVIRONMENT": "production",
            # Required outside development — see the database_url validator.
            "DATABASE_URL": "postgresql+psycopg://u:p@db.internal:5432/runsheet",
            "REDIS_URL": "redis://redis.internal:6379",
            "GEMINI_API_KEY": "test-gemini-key",
            "CORS_ORIGINS": '["https://app.runsheet.com"]',
        }

    # ── Production fail-closed: missing ST config raises (Req 10.3, 6.7) ──

    def test_supertokens_in_production_missing_st_config_raises_naming_both(self, prod_env_vars):
        """auth_provider=supertokens in production with no ST config fails
        closed, naming both missing values."""
        env_vars = {**prod_env_vars, "AUTH_PROVIDER": "supertokens"}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            message = str(exc_info.value)
            assert "supertokens_connection_uri" in message
            assert "supertokens_api_key" in message

    def test_production_error_names_only_missing_value(self, prod_env_vars):
        """When only one ST value is missing, the error names only that one."""
        env_vars = {
            **prod_env_vars,
            "SUPERTOKENS_API_KEY": "prod-st-api-key",
            # connection_uri intentionally omitted
        }
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            message = str(exc_info.value)
            assert "supertokens_connection_uri" in message
            assert "supertokens_api_key" not in message

    def test_production_supertokens_config_present_passes(self, prod_env_vars):
        """Providing both required ST values lets production validation pass."""
        env_vars = {
            **prod_env_vars,
            "AUTH_PROVIDER": "supertokens",
            "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com",
            "SUPERTOKENS_API_KEY": "prod-st-api-key",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.environment == Environment.PRODUCTION
            assert settings.auth_provider == "supertokens"
            assert settings.supertokens_connection_uri == "https://core.supertokens.example.com"
            assert settings.supertokens_api_key == "prod-st-api-key"

    # ── Hard cutover: the legacy jwt_secret field no longer exists ─────────

    def test_jwt_secret_field_removed_from_settings(self, prod_env_vars):
        """The homegrown jwt_secret/jwt_algorithm fields were removed at the
        hard cutover; a stray JWT_SECRET env var is ignored (Req 10.2, 10.5)."""
        env_vars = {
            **prod_env_vars,
            "AUTH_PROVIDER": "supertokens",
            "SUPERTOKENS_CONNECTION_URI": "https://core.supertokens.example.com",
            "SUPERTOKENS_API_KEY": "prod-st-api-key",
            # A lingering legacy secret in the environment is simply ignored —
            # the field is gone, so it cannot resurrect a forgeable credential.
            "JWT_SECRET": "prod-jwt-secret-that-is-at-least-32-chars-long",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.auth_provider == "supertokens"
            assert not hasattr(settings, "jwt_secret")
            assert "jwt_secret" not in Settings.model_fields

    def test_supertokens_missing_st_config_takes_precedence_over_other_checks(self, prod_env_vars):
        """When ST config is missing under supertokens, the fail-closed
        missing-config error is raised (Req 10.3)."""
        env_vars = {**prod_env_vars, "AUTH_PROVIDER": "supertokens"}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            message = str(exc_info.value)
            assert "supertokens_connection_uri" in message
            assert "supertokens_api_key" in message

    # ── supertokens in development tolerates missing ST config (Req 10.3) ──

    def test_supertokens_in_development_tolerates_missing_st_config(self, dev_env_vars):
        """supertokens in development needs no ST config: the fail-closed check
        applies only to non-development/test envs."""
        env_vars = {**dev_env_vars, "AUTH_PROVIDER": "supertokens"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.environment == Environment.DEVELOPMENT
            assert settings.auth_provider == "supertokens"
            assert settings.supertokens_connection_uri == ""
            assert settings.supertokens_api_key == ""

    # ── session_lifetime_seconds bounds (Req 2.7) ─────────────────────────

    def test_session_lifetime_seconds_below_minimum_rejected(self, dev_env_vars):
        """session_lifetime_seconds < 60 violates the ge=60 bound."""
        env_vars = {**dev_env_vars, "SESSION_LIFETIME_SECONDS": "59"}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            assert "session_lifetime_seconds" in str(exc_info.value).lower()

    def test_session_lifetime_seconds_at_minimum_accepted(self, dev_env_vars):
        """session_lifetime_seconds == 60 (the lower bound) is accepted."""
        env_vars = {**dev_env_vars, "SESSION_LIFETIME_SECONDS": "60"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.session_lifetime_seconds == 60

    def test_session_lifetime_seconds_above_minimum_accepted(self, dev_env_vars):
        """A typical session_lifetime_seconds value above the bound is accepted."""
        env_vars = {**dev_env_vars, "SESSION_LIFETIME_SECONDS": "7200"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.session_lifetime_seconds == 7200

    # ── password_min_length bounds (Req 1.7) ──────────────────────────────

    def test_password_min_length_below_minimum_rejected(self, dev_env_vars):
        """password_min_length < 8 violates the ge=8 bound."""
        env_vars = {**dev_env_vars, "PASSWORD_MIN_LENGTH": "7"}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            assert "password_min_length" in str(exc_info.value).lower()

    def test_password_min_length_above_maximum_rejected(self, dev_env_vars):
        """password_min_length > 128 violates the le=128 bound."""
        env_vars = {**dev_env_vars, "PASSWORD_MIN_LENGTH": "129"}
        with patch.dict(os.environ, env_vars, clear=True):
            with pytest.raises(Exception) as exc_info:
                Settings()
            assert "password_min_length" in str(exc_info.value).lower()

    def test_password_min_length_at_lower_bound_accepted(self, dev_env_vars):
        """password_min_length == 8 (the lower bound) is accepted."""
        env_vars = {**dev_env_vars, "PASSWORD_MIN_LENGTH": "8"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.password_min_length == 8

    def test_password_min_length_at_upper_bound_accepted(self, dev_env_vars):
        """password_min_length == 128 (the upper bound) is accepted."""
        env_vars = {**dev_env_vars, "PASSWORD_MIN_LENGTH": "128"}
        with patch.dict(os.environ, env_vars, clear=True):
            settings = Settings()
            assert settings.password_min_length == 128

"""
Authentication package for the Runsheet backend (SuperTokens migration).

Houses the SuperTokens SDK integration, the shared Role_Authorizer, the
User_Provisioner, and the Test_Auth_Path. Submodules are imported directly
(e.g. ``from auth.test_auth import override_auth``) rather than re-exported
here so that importing this package never pulls in the SuperTokens SDK or
any test-only entry points implicitly.
"""

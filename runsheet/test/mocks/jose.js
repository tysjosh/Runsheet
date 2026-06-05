/**
 * Jest stub for the `jose` package.
 *
 * `jose` (v6+) ships as pure ESM with no CommonJS build. Under the
 * CommonJS Jest runtime, any test that transitively imports
 * `src/utils/auth.ts` (→ the API service clients) otherwise fails to
 * parse with "Unexpected token 'export'".
 *
 * No test depends on real JWT cryptography — the service clients only
 * use `getAuthToken()` to attach a bearer header, and the tests mock
 * `global.fetch`. So this stub provides just enough of the `SignJWT`
 * fluent API for `generateDevToken` to resolve to a dummy token.
 *
 * Wired via `moduleNameMapper` in jest.config.js.
 */

class SignJWT {
  constructor(payload) {
    this._payload = payload;
  }
  setProtectedHeader() {
    return this;
  }
  setIssuedAt() {
    return this;
  }
  setExpirationTime() {
    return this;
  }
  async sign() {
    return "test.jwt.token";
  }
}

async function jwtVerify() {
  return { payload: {}, protectedHeader: {} };
}

module.exports = { SignJWT, jwtVerify };

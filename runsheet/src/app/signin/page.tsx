"use client";

import { useRouter } from "next/navigation";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";
import SignIn from "../../components/SignIn";
import { throttledMessage } from "../../utils/authThrottle";

export default function SignInPage() {
  const router = useRouter();

  const handleSignIn = async (email: string, password: string) => {
    // Authenticate against SuperTokens via the EmailPassword recipe. On success
    // the SDK establishes a managed (cookie-backed) session — the browser never
    // mints its own token (SuperTokens Auth Migration Req 8.2, 8.3).
    let response: Awaited<ReturnType<typeof EmailPassword.signIn>>;
    try {
      response = await EmailPassword.signIn({
        formFields: [
          { id: "email", value: email },
          { id: "password", value: password },
        ],
      });
    } catch (err) {
      // A throttled attempt (F5) rejects with the raw 429 response.
      const throttled = await throttledMessage(err);
      if (throttled) throw new Error(throttled);
      throw err;
    }

    if (response.status === "FIELD_ERROR") {
      // Surface the first field-level validation error (e.g. invalid email).
      const message =
        response.formFields[0]?.error ?? "Please check your details.";
      throw new Error(message);
    }

    if (response.status !== "OK") {
      // WRONG_CREDENTIALS_ERROR or SIGN_IN_NOT_ALLOWED.
      throw new Error("Invalid credentials");
    }

    // Use replace instead of push to prevent back navigation to signin.
    router.replace("/dashboard");
  };

  return <SignIn onSignIn={handleSignIn} />;
}

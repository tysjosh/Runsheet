/**
 * Tests for the SuperTokens-backed sign-in page (:file:`signin/page.tsx`) and
 * the underlying :file:`components/SignIn.tsx` error surface.
 *
 * Covers the sign-in error-rendering contract of the SuperTokens migration:
 *
 *   - A wrong-credentials response (`status !== "OK"`) surfaces a visible
 *     "Invalid credentials" alert and does NOT navigate away.
 *   - A field-level validation error (`status === "FIELD_ERROR"`) surfaces the
 *     backend-provided field message.
 *   - A successful sign-in (`status === "OK"`) establishes the SDK session and
 *     redirects to the dashboard (no error rendered).
 *   - Client-side guards (empty fields, malformed email) render their own
 *     errors before the SuperTokens call is made.
 *
 * The SuperTokens EmailPassword recipe is mocked so the test never touches the
 * network; `useRouter` is mocked globally in jest.setup.js.
 *
 * Validates: Requirements 8.2.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";
import SignInPage from "./page";

jest.mock("supertokens-auth-react/recipe/emailpassword", () => ({
  __esModule: true,
  default: {
    signIn: jest.fn(),
  },
}));

// Override the global next/navigation mock with a STABLE router object so the
// `replace` spy captured here is the same instance the page calls (the default
// mock returns a fresh object per call).
const replaceMock = jest.fn();
jest.mock("next/navigation", () => ({
  __esModule: true,
  useRouter: () => ({ replace: replaceMock, push: jest.fn() }),
}));

const signInMock = EmailPassword.signIn as jest.MockedFunction<
  typeof EmailPassword.signIn
>;

type SignInResult = Awaited<ReturnType<typeof EmailPassword.signIn>>;

// Build a minimal recipe response without `any` — the recipe's union return
// type is wide, so we narrow through `unknown` for the few fields the page reads.
function signInResult(partial: Record<string, unknown>): SignInResult {
  return partial as unknown as SignInResult;
}

function fillCredentials(email: string, password: string) {
  fireEvent.change(screen.getByLabelText(/email address/i), {
    target: { value: email },
  });
  fireEvent.change(screen.getByLabelText(/^password/i), {
    target: { value: password },
  });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
}

beforeEach(() => {
  signInMock.mockReset();
  replaceMock.mockReset();
});

describe("SignInPage", () => {
  it("renders an error alert when SuperTokens rejects the credentials", async () => {
    signInMock.mockResolvedValue(
      signInResult({ status: "WRONG_CREDENTIALS_ERROR" }),
    );

    render(<SignInPage />);
    fillCredentials("admin@runsheet.com", "wrong-password");
    submit();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/invalid credentials/i);
    // A failed sign-in must not navigate the user onward.
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("surfaces the field-level error message on FIELD_ERROR", async () => {
    signInMock.mockResolvedValue(
      signInResult({
        status: "FIELD_ERROR",
        formFields: [{ id: "email", error: "Email is not valid" }],
      }),
    );

    render(<SignInPage />);
    fillCredentials("admin@runsheet.com", "demo1234");
    submit();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/email is not valid/i);
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("establishes the session and redirects to the dashboard on success", async () => {
    signInMock.mockResolvedValue(
      signInResult({ status: "OK", user: { id: "st-user-1" } }),
    );

    render(<SignInPage />);
    fillCredentials("admin@runsheet.com", "demo1234");
    submit();

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/dashboard"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("validates required fields before calling SuperTokens", async () => {
    render(<SignInPage />);
    submit();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /fill in all fields/i,
    );
    expect(signInMock).not.toHaveBeenCalled();
  });

  it("rejects a malformed email before calling SuperTokens", async () => {
    render(<SignInPage />);
    fillCredentials("not-an-email", "demo1234");
    submit();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /valid email address/i,
    );
    expect(signInMock).not.toHaveBeenCalled();
  });
});

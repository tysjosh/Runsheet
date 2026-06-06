/**
 * Tests for the self-serve password reset/set page.
 *
 * Covers the three phases that close the SuperTokens migration OQ6 gap:
 *
 *   - With no `?token` on the URL, the page shows the "reset link required"
 *     hint instead of a broken form.
 *   - With a token, submitting a matching password calls
 *     `EmailPassword.submitNewPassword` and shows the success state.
 *   - Mismatched passwords are rejected client-side before the SDK is called.
 *   - An invalid/expired token surfaces an error and falls back to the hint.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";
import ResetPasswordPage from "./page";

jest.mock("supertokens-auth-react/recipe/emailpassword", () => ({
  __esModule: true,
  default: { submitNewPassword: jest.fn() },
}));

const submitMock = EmailPassword.submitNewPassword as jest.MockedFunction<
  typeof EmailPassword.submitNewPassword
>;

type SubmitResult = Awaited<ReturnType<typeof EmailPassword.submitNewPassword>>;

function submitResult(partial: Record<string, unknown>): SubmitResult {
  return partial as unknown as SubmitResult;
}

function setUrl(search: string) {
  window.history.pushState({}, "", `/auth/reset-password${search}`);
}

function fillPasswords(pw: string, confirm: string) {
  fireEvent.change(screen.getByLabelText(/new password/i), {
    target: { value: pw },
  });
  fireEvent.change(screen.getByLabelText(/confirm password/i), {
    target: { value: confirm },
  });
}

beforeEach(() => {
  submitMock.mockReset();
});

describe("ResetPasswordPage", () => {
  it("shows the 'reset link required' hint when no token is present", () => {
    setUrl("");
    render(<ResetPasswordPage />);
    expect(screen.getByText(/reset link required/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/new password/i)).not.toBeInTheDocument();
  });

  it("sets the password and shows success on a valid token submit", async () => {
    setUrl("?token=abc&tenantId=public");
    submitMock.mockResolvedValue(submitResult({ status: "OK" }));

    render(<ResetPasswordPage />);
    fillPasswords("Demo1234!", "Demo1234!");
    fireEvent.click(screen.getByRole("button", { name: /set password/i }));

    expect(await screen.findByText(/password updated/i)).toBeInTheDocument();
    expect(submitMock).toHaveBeenCalledWith({
      formFields: [{ id: "password", value: "Demo1234!" }],
    });
  });

  it("rejects mismatched passwords before calling the SDK", async () => {
    setUrl("?token=abc");
    render(<ResetPasswordPage />);
    fillPasswords("Demo1234!", "Different1!");
    fireEvent.click(screen.getByRole("button", { name: /set password/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /do not match/i,
    );
    expect(submitMock).not.toHaveBeenCalled();
  });

  it("surfaces an invalid-token error and falls back to the hint", async () => {
    setUrl("?token=expired");
    submitMock.mockResolvedValue(
      submitResult({ status: "RESET_PASSWORD_INVALID_TOKEN_ERROR" }),
    );

    render(<ResetPasswordPage />);
    fillPasswords("Demo1234!", "Demo1234!");
    fireEvent.click(screen.getByRole("button", { name: /set password/i }));

    await waitFor(() =>
      expect(screen.getByText(/reset link required/i)).toBeInTheDocument(),
    );
  });
});

/**
 * Tests for the forgot-password page's throttled-request message (staging
 * finding F5). The SuperTokens web SDK rejects a 429 with the raw response;
 * the page must show how long to wait instead of a generic error.
 *
 * The EmailPassword recipe is mocked so the test never touches the network.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import EmailPassword from "supertokens-auth-react/recipe/emailpassword";
import ForgotPasswordPage from "./page";

jest.mock("supertokens-auth-react/recipe/emailpassword", () => ({
  __esModule: true,
  default: {
    sendPasswordResetEmail: jest.fn(),
  },
}));

jest.mock("next/navigation", () => ({
  __esModule: true,
  useRouter: () => ({ replace: jest.fn(), push: jest.fn() }),
}));

const sendMock = EmailPassword.sendPasswordResetEmail as jest.MockedFunction<
  typeof EmailPassword.sendPasswordResetEmail
>;

type SendResult = Awaited<
  ReturnType<typeof EmailPassword.sendPasswordResetEmail>
>;

function submitEmail(email: string) {
  fireEvent.change(screen.getByLabelText(/email address/i), {
    target: { value: email },
  });
  fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));
}

beforeEach(() => {
  sendMock.mockReset();
});

describe("ForgotPasswordPage", () => {
  it("shows the retry wait when the reset request is throttled (429)", async () => {
    const res = {
      status: 429,
      headers: { get: () => "30" },
      clone() {
        return res;
      },
      json: async () => ({ details: { retry_after_seconds: 30 } }),
    };
    sendMock.mockRejectedValue(res);
    render(<ForgotPasswordPage />);
    submitEmail("someone@example.com");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many attempts, try again in 30 seconds",
    );
  });

  it("shows the confirmation on OK", async () => {
    sendMock.mockResolvedValue({ status: "OK" } as unknown as SendResult);
    render(<ForgotPasswordPage />);
    submitEmail("someone@example.com");
    expect(await screen.findByText(/check your email/i)).toBeInTheDocument();
  });
});

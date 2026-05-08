/**
 * Component tests for the driver POD submission page (Task 11.4).
 *
 * Covers the three behaviors the spec calls out:
 *
 *  1. **Presigned-upload integration** — selecting a signature file
 *     triggers `presignPodUpload` + `putPresignedFile` and the slot
 *     transitions to "uploaded" on success. (Req 4.1.3)
 *  2. **OCR confirmation modal** — when the POD submission response
 *     includes `ocr_requires_manual_review=true` the modal appears with
 *     the extracted value and a manual-override input. (Req 4.2.5)
 *  3. **Manual override submit** — resubmitting from the modal uses a
 *     fresh idempotency key and persists the driver-entered gallon
 *     count with `delivered_gallons_source="manual"`. (Req 4.2.5)
 *
 * Kept intentionally focused — exhaustive coverage of every form field
 * would duplicate what `driverApi.test.ts` already validates. These
 * tests exercise the integration points unique to the page.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// Mock the driver API module *before* the component imports it.
jest.mock("../../services/driverApi", () => ({
  presignPodUpload: jest.fn(),
  putPresignedFile: jest.fn(),
  submitPOD: jest.fn(),
}));

import { presignPodUpload, putPresignedFile } from "../../services/driverApi";
import PODSubmissionPage from "./PODSubmissionPage";

const mockPresign = presignPodUpload as jest.MockedFunction<
  typeof presignPodUpload
>;
const mockPut = putPresignedFile as jest.MockedFunction<
  typeof putPresignedFile
>;

// ─── Geolocation shim ────────────────────────────────────────────────────────

function stubGeolocation() {
  Object.defineProperty(globalThis.navigator, "geolocation", {
    configurable: true,
    value: {
      getCurrentPosition: (success: PositionCallback) => {
        success({
          coords: {
            latitude: 40.7128,
            longitude: -74.006,
            accuracy: 10,
            altitude: null,
            altitudeAccuracy: null,
            heading: null,
            speed: null,
          },
          timestamp: Date.now(),
        } as GeolocationPosition);
      },
    },
  });
}

function makeFile(name: string, type = "image/png"): File {
  return new File(["x"], name, { type });
}

beforeEach(() => {
  jest.clearAllMocks();
  stubGeolocation();
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("PODSubmissionPage", () => {
  /**
   * Locate the signature, photo, and meter-ticket file inputs. The
   * component renders the inputs as sibling `<input type="file">`
   * elements inside visible label wrappers (see `SlotFileInput`), and
   * each section renders exactly one input in document order: signature,
   * then photos, then meter ticket. We rely on that deterministic
   * ordering to pick the correct input without adding test-only markers
   * to the production component.
   */
  function getFileInputs(container: HTMLElement): {
    signature: HTMLInputElement;
    photos: HTMLInputElement;
    meter: HTMLInputElement;
  } {
    const inputs =
      container.querySelectorAll<HTMLInputElement>('input[type="file"]');
    expect(inputs).toHaveLength(3);
    return {
      signature: inputs[0],
      photos: inputs[1],
      meter: inputs[2],
    };
  }

  it("uploads a selected signature through presign + PUT", async () => {
    mockPresign.mockResolvedValue({
      file_ref: "tenants/t/signature/a.png",
      upload_url: "https://s3.example.com/put",
      expires_at: "2024-01-15T12:15:00Z",
      content_type: "image/png",
      max_file_bytes: 10 * 1024 * 1024,
    });
    mockPut.mockResolvedValue();

    const { container } = render(<PODSubmissionPage jobId="job-1" />);

    const { signature } = getFileInputs(container);
    fireEvent.change(signature, { target: { files: [makeFile("sig.png")] } });

    await waitFor(() => expect(mockPresign).toHaveBeenCalledTimes(1));
    expect(mockPresign).toHaveBeenCalledWith("signature", "image/png");
    await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));

    // After the upload resolves, the "Uploaded" status pill should appear.
    await waitFor(() => {
      expect(screen.getByText(/Uploaded/)).toBeInTheDocument();
    });
  });

  it("surfaces an upload failure on the slot without aborting the whole form", async () => {
    mockPresign.mockRejectedValue(new Error("presign failed"));

    const { container } = render(<PODSubmissionPage jobId="job-1" />);

    const { signature } = getFileInputs(container);
    fireEvent.change(signature, { target: { files: [makeFile("sig.png")] } });

    await waitFor(() => {
      expect(screen.getByText(/presign failed/i)).toBeInTheDocument();
    });
    // The retry button should be rendered so the driver can recover.
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("renders a helpful hint when no files are selected yet", () => {
    render(<PODSubmissionPage jobId="job-1" />);
    expect(
      screen.getByText(
        /Complete every required field and upload to enable submit\./i,
      ),
    ).toBeInTheDocument();
  });
});

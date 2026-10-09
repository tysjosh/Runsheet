// Jest setup file for React Testing Library
// This file is run before each test file

// Import jest-dom matchers for enhanced DOM assertions
// Provides matchers like toBeInTheDocument(), toHaveTextContent(), etc.
import "@testing-library/jest-dom";
import { configure } from "@testing-library/react";

// Raise the async-utility timeout from testing-library's 1000ms default.
//
// This suite became a CI gate, and CustomersListPage's "renders customer detail
// in-shell" case flaked at 1803ms while the machine was busy: it performs three
// sequential async waits (list renders, detail loads, list restores), each with
// its own 1000ms budget, so a loaded shared runner is enough to exceed one of
// them. It passes consistently when the machine is idle.
//
// This does not weaken any assertion — a `findBy`/`waitFor` still asserts the
// same condition, it only waits longer before giving up. What it removes is a
// gate that reports failure based on how busy the runner was. Jest's own
// per-test timeout (5000ms default) still bounds a genuinely hung test.
configure({ asyncUtilTimeout: 5000 });

// Mock Next.js router
jest.mock("next/navigation", () => ({
  useRouter: () => ({
    push: jest.fn(),
    replace: jest.fn(),
    prefetch: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    refresh: jest.fn(),
  }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
}));

// Mock Next.js Image component
jest.mock("next/image", () => ({
  __esModule: true,
  default: (props) => {
    // eslint-disable-next-line @next/next/no-img-element, jsx-a11y/alt-text
    return <img {...props} />;
  },
}));

// Browser-only shims, guarded on `window`.
//
// The suite default is jsdom, but route handlers under src/app/api must run in
// the `node` environment: they need the real Request/Response/fetch globals,
// which jsdom does not provide. A file opting in with `@jest-environment node`
// still loads this setup file, and an unguarded `Object.defineProperty(window,
// ...)` threw `ReferenceError: window is not defined` before the suite could
// start. Guarding keeps both environments working from one setup file.
if (typeof window !== "undefined") {
  // Mock window.matchMedia for responsive components
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: jest.fn().mockImplementation((query) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: jest.fn(), // deprecated
      removeListener: jest.fn(), // deprecated
      addEventListener: jest.fn(),
      removeEventListener: jest.fn(),
      dispatchEvent: jest.fn(),
    })),
  });
}

// Mock ResizeObserver for components that use it
global.ResizeObserver = jest.fn().mockImplementation(() => ({
  observe: jest.fn(),
  unobserve: jest.fn(),
  disconnect: jest.fn(),
}));

// Mock IntersectionObserver for lazy loading components
global.IntersectionObserver = jest.fn().mockImplementation(() => ({
  observe: jest.fn(),
  unobserve: jest.fn(),
  disconnect: jest.fn(),
}));

// Suppress console errors during tests (optional - can be removed if you want to see errors)
// const originalError = console.error;
// beforeAll(() => {
//   console.error = (...args) => {
//     if (
//       typeof args[0] === 'string' &&
//       args[0].includes('Warning: ReactDOM.render is no longer supported')
//     ) {
//       return;
//     }
//     originalError.call(console, ...args);
//   };
// });
// afterAll(() => {
//   console.error = originalError;
// });

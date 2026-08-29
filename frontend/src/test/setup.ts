import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import React from "react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { server } from "../../tests/mocks/server";

// Start MSW server before all tests
beforeAll(() => server.listen({ onUnhandledRequest: "warn" }));

// Mock framer-motion to avoid animation issues in tests
vi.mock("framer-motion", () => {
  const createMotionComponent = (tag: string) => {
    return React.forwardRef(function MotionComponent(
      {
        children,
        initial,
        animate,
        exit,
        transition,
        layoutId,
        ...props
      }: React.PropsWithChildren<Record<string, unknown>>,
      ref: React.Ref<HTMLElement>
    ) {
      // Filter out framer-motion specific props that are not valid HTML attributes
      void initial;
      void animate;
      void exit;
      void transition;
      void layoutId;
      return React.createElement(tag, { ...props, ref }, children);
    });
  };

  return {
    motion: {
      div: createMotionComponent("div"),
      span: createMotionComponent("span"),
      button: createMotionComponent("button"),
    },
    AnimatePresence: ({ children }: { children: React.ReactNode }) => children,
  };
});

// Cleanup after each test
afterEach(() => {
  cleanup();
  server.resetHandlers();
});

// Mock Next.js router
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
    back: vi.fn(),
    pathname: "/",
    query: {},
    asPath: "/",
  }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
}));

// Mock useAuth hook
vi.mock("@/hooks/use-auth", () => ({
  useAuth: () => ({
    user: { username: "User", email: "user@example.com", id: "1" },
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    logout: vi.fn(),
    register: vi.fn(),
    loginWithToken: vi.fn(),
  }),
  AuthProvider: ({ children }: { children: React.ReactNode }) => children,
}));

// Mock localStorage
const localStorageMock: Storage = (() => {
  let store: Record<string, string> = {};

  return {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = value.toString();
    },
    removeItem: (key: string) => {
      delete store[key];
    },
    clear: () => {
      store = {};
    },
    key: (index: number) => {
      const keys = Object.keys(store);
      return keys[index] ?? null;
    },
    get length() {
      return Object.keys(store).length;
    },
  };
})();

Object.defineProperty(window, "localStorage", {
  value: localStorageMock,
});

// Mock window.location
delete (window as { location?: Location }).location;
(window as { location: Location }).location = {
  href: "http://localhost:3000",
  origin: "http://localhost:3000",
  protocol: "http:",
  host: "localhost:3000",
  hostname: "localhost",
  port: "3000",
  pathname: "/",
  search: "",
  hash: "",
  reload: vi.fn() as unknown as () => void,
  replace: vi.fn() as unknown as (url: string | URL) => void,
  assign: vi.fn() as unknown as (url: string | URL) => void,
  ancestorOrigins: {} as DOMStringList,
} as Location;

// Suppress console errors during tests (optional)
const originalError = console.error;
beforeAll(() => {
  console.error = (...args: unknown[]) => {
    if (
      typeof args[0] === "string" &&
      (args[0].includes("Warning: ReactDOM.render") ||
        args[0].includes("Not implemented: HTMLFormElement.prototype.submit"))
    ) {
      return;
    }
    originalError.call(console, ...args);
  };
});

afterAll(() => {
  console.error = originalError;
  server.close();
});

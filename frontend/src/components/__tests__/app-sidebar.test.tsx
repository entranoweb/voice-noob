import { describe, it, expect, beforeEach, afterEach } from "vitest";
import userEvent from "@testing-library/user-event";
import { render, screen } from "@/test/test-utils";
import { useSidebarStore } from "@/lib/sidebar-store";
import { AppSidebar } from "../app-sidebar";

// Helper to render sidebar
const renderSidebar = () => {
  return render(<AppSidebar />);
};

describe("AppSidebar", () => {
  it("renders component without crashing", () => {
    const { container } = renderSidebar();
    expect(container).toBeTruthy();
  });

  it("contains application branding", () => {
    renderSidebar();
    expect(screen.getByText("Synthiq Voice")).toBeInTheDocument();
  });

  it("renders navigation items", () => {
    renderSidebar();

    // Check for key navigation items (Settings is in dropdown menu, not main nav)
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Voice Agents")).toBeInTheDocument();
    expect(screen.getByText("CRM")).toBeInTheDocument();
    expect(screen.getByText("Integrations")).toBeInTheDocument();
    expect(screen.getByText("Appointments")).toBeInTheDocument();
  });

  it("renders user profile information", () => {
    renderSidebar();

    expect(screen.getByText("User")).toBeInTheDocument();
    expect(screen.getByText("user@example.com")).toBeInTheDocument();
  });

  it("has navigation links with correct structure", () => {
    const { container } = renderSidebar();

    // Check for link elements
    const links = container.querySelectorAll("a");
    expect(links.length).toBeGreaterThan(5); // Should have multiple nav links
  });

  it("renders icons for navigation items", () => {
    const { container } = renderSidebar();

    // lucide-react icons render as SVG elements
    const svgIcons = container.querySelectorAll("svg");
    expect(svgIcons.length).toBeGreaterThan(0);
  });
});

describe("AppSidebar on small and large screens", () => {
  const originalWidth = window.innerWidth;

  const setWidth = (width: number) => {
    Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: () => ({
        matches: width < 768,
        addEventListener: () => {},
        removeEventListener: () => {},
      }),
    });
  };

  beforeEach(() => {
    localStorage.clear();
    useSidebarStore.setState({ sidebarOpen: true, hasChosen: false });
  });

  afterEach(() => {
    setWidth(originalWidth);
    localStorage.clear();
  });

  it("starts collapsed at phone width, so the page keeps its room", () => {
    setWidth(390);
    renderSidebar();

    expect(screen.queryByText("Voice Agents")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show sidebar" })).toHaveAttribute(
      "aria-expanded",
      "false"
    );
    // Still reachable by name for keyboard and screen-reader users.
    expect(screen.getByRole("link", { name: "Voice Agents" })).toBeInTheDocument();
  });

  it("starts expanded at desktop width", () => {
    setWidth(1280);
    renderSidebar();

    expect(screen.getByText("Voice Agents")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide sidebar" })).toBeInTheDocument();
  });

  it("keeps a saved choice to have it open on a phone", () => {
    setWidth(390);
    useSidebarStore.setState({ sidebarOpen: true, hasChosen: true });
    renderSidebar();

    expect(screen.getByText("Voice Agents")).toBeInTheDocument();
  });

  it("keeps a saved choice to have it closed on a desktop", () => {
    setWidth(1280);
    useSidebarStore.setState({ sidebarOpen: false, hasChosen: true });
    renderSidebar();

    expect(screen.queryByText("Voice Agents")).not.toBeInTheDocument();
  });

  it("opens from the keyboard on a phone and remembers that choice", async () => {
    setWidth(390);
    const user = userEvent.setup();
    renderSidebar();

    screen.getByRole("button", { name: "Show sidebar" }).focus();
    await user.keyboard("{Enter}");

    expect(await screen.findByText("Voice Agents")).toBeInTheDocument();
    expect(useSidebarStore.getState()).toMatchObject({ sidebarOpen: true, hasChosen: true });
  });
});

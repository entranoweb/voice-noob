import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import { AppSidebar } from "../app-sidebar";
import { SidebarProvider } from "@/components/ui/sidebar";

// Helper to render with required providers
const renderSidebar = () => {
  return render(
    <SidebarProvider>
      <AppSidebar />
    </SidebarProvider>
  );
};

describe("AppSidebar", () => {
  it("renders component without crashing", () => {
    const { container } = renderSidebar();
    expect(container).toBeTruthy();
  });

  it("contains application branding", () => {
    renderSidebar();
    // Test basic rendering - shadcn/ui components may have complex DOM structure
    expect(screen.getByText("Voice Noob")).toBeInTheDocument();
    expect(screen.getByText("AI Voice Platform")).toBeInTheDocument();
  });

  it("renders navigation items", () => {
    renderSidebar();

    // Check for key navigation items
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Voice Agents")).toBeInTheDocument();
    expect(screen.getByText("CRM")).toBeInTheDocument();
    expect(screen.getByText("Integrations")).toBeInTheDocument();
    expect(screen.getByText("Settings")).toBeInTheDocument();
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

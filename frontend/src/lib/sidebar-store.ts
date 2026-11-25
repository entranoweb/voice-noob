import { create } from "zustand";
import { persist } from "zustand/middleware";

interface SidebarStore {
  /** The person's own choice. Only meaningful once `hasChosen` is true. */
  sidebarOpen: boolean;
  /** False until the person opens or closes the sidebar themselves. */
  hasChosen: boolean;
  setSidebarOpen: (open: boolean) => void;
  toggleSidebar: () => void;
}

export const useSidebarStore = create<SidebarStore>()(
  persist(
    (set) => ({
      sidebarOpen: true,
      hasChosen: false,
      setSidebarOpen: (open) => set({ sidebarOpen: open, hasChosen: true }),
      toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen, hasChosen: true })),
    }),
    {
      name: "sidebar-storage",
      version: 1,
      // Version 0 only wrote its state when someone toggled the sidebar, so a saved
      // value there is a real choice and must keep winning over the screen-size default.
      migrate: (persisted) => ({ ...(persisted as object), hasChosen: true }) as SidebarStore,
    }
  )
);

/**
 * Whether the sidebar is open: the person's saved choice if they made one,
 * otherwise open on wide screens and collapsed on small ones.
 */
export function resolveSidebarOpen(
  state: Pick<SidebarStore, "sidebarOpen" | "hasChosen">,
  isMobile: boolean
): boolean {
  return state.hasChosen ? state.sidebarOpen : !isMobile;
}

"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import {
  RENEW_JITTER_MS,
  RENEW_MARGIN_MS,
  accessTokenExpiry,
  clearSession,
  endSessionOnServer,
  getAccessToken,
  getRefreshToken,
  refreshSession,
  storeSession,
} from "@/lib/session";

interface User {
  id: number;
  email: string;
  username: string;
}

interface AuthContextType {
  user: User | null;
  token: string | null;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const router = useRouter();
  const pathname = usePathname();

  // Check for existing token on mount
  useEffect(() => {
    const storedToken = getAccessToken();
    if (storedToken) {
      setToken(storedToken);
      // Validate token by fetching user info
      void fetchUser(storedToken);
    } else {
      setIsLoading(false);
    }
  }, []);

  // Keep the access token fresh. Most of the API clients read it straight from
  // storage and know nothing about refreshing, so it is renewed here, shortly
  // before it runs out, and again when a sleeping tab wakes up.
  useEffect(() => {
    if (!token) return;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const renew = async () => {
      const renewed = await refreshSession();
      if (renewed) setToken(renewed);
    };
    const schedule = () => {
      const expiry = accessTokenExpiry(getAccessToken() ?? token);
      if (expiry === null) return;
      // Jitter so tabs opened together do not all wake in the same millisecond.
      const jitter = Math.random() * RENEW_JITTER_MS;
      const wait = Math.max(expiry - RENEW_MARGIN_MS - Date.now() - jitter, 0);
      timer = setTimeout(() => void renew(), wait);
    };
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      const expiry = accessTokenExpiry(getAccessToken() ?? token);
      if (expiry !== null && expiry - RENEW_MARGIN_MS <= Date.now()) void renew();
    };

    schedule();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [token]);

  // Redirect logic
  useEffect(() => {
    if (isLoading) return;

    const isAuthPage = pathname === "/login" || pathname === "/register";
    // Embed pages and the password-reset pages are public, no sign-in required.
    const isPublicPage =
      pathname.startsWith("/embed") ||
      pathname === "/forgot-password" ||
      pathname === "/reset-password";

    if (!token && !isAuthPage && !isPublicPage) {
      router.push("/login");
    } else if (token && isAuthPage) {
      router.push("/dashboard");
    }
  }, [token, isLoading, pathname, router]);

  const fetchUser = async (accessToken: string) => {
    try {
      let lostRace = false;
      let response = await fetch(`${API_BASE}/api/v1/auth/me`, {
        headers: {
          Authorization: `Bearer ${accessToken}`,
        },
      });

      if (response.status === 401) {
        // An expired access token is routine: renew it silently and ask again.
        const renewed = await refreshSession();
        if (renewed) {
          setToken(renewed);
          response = await fetch(`${API_BASE}/api/v1/auth/me`, {
            headers: { Authorization: `Bearer ${renewed}` },
          });
        } else {
          // A 401 from /refresh has already emptied storage. A session still
          // stored means the renewal lost a race (409): that never signs out.
          lostRace = getRefreshToken() !== null;
        }
      }

      if (response.ok) {
        const userData = await response.json();
        setUser(userData);
      } else if (lostRace) {
        // Leave the stored session for the next request to renew.
      } else {
        // Token invalid, clear it
        clearSession();
        setToken(null);
      }
    } catch {
      clearSession();
      setToken(null);
    } finally {
      setIsLoading(false);
    }
  };

  const login = async (email: string, password: string) => {
    const formData = new URLSearchParams();
    formData.append("username", email);
    formData.append("password", password);

    const response = await fetch(`${API_BASE}/api/v1/auth/login`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: "Login failed" }));
      throw new Error(error.detail ?? "Login failed");
    }

    const data = await response.json();
    storeSession(data);
    setToken(data.access_token);
    await fetchUser(data.access_token);
    router.push("/dashboard");
  };

  const register = async (email: string, username: string, password: string) => {
    const response = await fetch(`${API_BASE}/api/v1/auth/register`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ email, username, password }),
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: "Registration failed" }));
      throw new Error(error.detail ?? "Registration failed");
    }

    // Auto-login after registration
    await login(email, password);
  };

  const logout = () => {
    // End the sign-in on the server first (it needs the refresh token), then forget it here.
    void endSessionOnServer(); // reads the refresh token before it is cleared below
    clearSession();
    setToken(null);
    setUser(null);
    router.push("/login");
  };

  return (
    <AuthContext.Provider value={{ user, token, isLoading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}

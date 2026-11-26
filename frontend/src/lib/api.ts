import axios, { type InternalAxiosRequestConfig } from "axios";
import {
  clearSession,
  endSessionOnServer,
  getAccessToken,
  getRefreshToken,
  refreshSession,
} from "@/lib/session";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const api = axios.create({
  baseURL: API_URL,
  headers: {
    "Content-Type": "application/json",
  },
  withCredentials: true,
});

// Logout function - ends the sign-in on the server, clears the session and redirects to login
export function logout(): void {
  void endSessionOnServer().finally(() => {
    clearSession();
    try {
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- a full page load is the point: it drops every piece of client state along with the token. router.push() would keep a logged-out SPA alive.
      window.location.href = "/login";
    } catch (navError) {
      console.error("Failed to redirect to login:", navError);
    }
  });
}

// Request interceptor for adding auth token
api.interceptors.request.use(
  (config) => {
    const token = getAccessToken();
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// Response interceptor for handling errors
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config as
      | (InternalAxiosRequestConfig & { _retried?: boolean })
      | undefined;
    if (error.response?.status === 401) {
      // The access token is short-lived. Renew it once and replay the request;
      // only when that fails is the person signed out.
      if (original && !original._retried && getRefreshToken()) {
        original._retried = true;
        const renewed = await refreshSession();
        if (renewed) {
          original.headers.Authorization = `Bearer ${renewed}`;
          return api.request(original);
        }
      }
      console.error("Authentication failed - redirecting to login", {
        endpoint: error.config?.url,
        status: error.response.status,
      });
      clearSession();
      // Safely navigate to login
      try {
        // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- a full page load is the point: it drops every piece of client state along with the token. router.push() would keep a logged-out SPA alive.
        window.location.href = "/login";
      } catch (navError) {
        console.error("Failed to redirect to login:", navError);
      }
    } else if (error.response) {
      // Log API errors with details
      console.error("API error:", {
        endpoint: error.config?.url,
        method: error.config?.method?.toUpperCase(),
        status: error.response.status,
        message: error.response.data?.message ?? error.message,
      });
    } else if (error.request) {
      // Log network errors
      console.error("Network error - no response received:", {
        endpoint: error.config?.url,
        method: error.config?.method?.toUpperCase(),
      });
    } else {
      // Log request setup errors
      console.error("Request error:", error.message);
    }
    return Promise.reject(error);
  }
);

// Integration types
export interface IntegrationResponse {
  id: string;
  integration_id: string;
  integration_name: string;
  workspace_id: string | null;
  is_active: boolean;
  is_connected: boolean;
  connected_at: string | null;
  last_used_at: string | null;
  has_credentials: boolean;
  credential_fields: string[];
}

export interface IntegrationListResponse {
  integrations: IntegrationResponse[];
  total: number;
}

export interface ConnectIntegrationRequest {
  integration_id: string;
  integration_name: string;
  workspace_id?: string | null;
  credentials: Record<string, string>;
  metadata?: Record<string, unknown>;
}

export interface UpdateIntegrationRequest {
  credentials?: Record<string, string>;
  metadata?: Record<string, unknown>;
  is_active?: boolean;
}

// Integration API functions
export const integrationsApi = {
  // List all connected integrations for the user (optionally filtered by workspace)
  list: async (workspaceId?: string): Promise<IntegrationListResponse> => {
    const params = workspaceId ? { workspace_id: workspaceId } : {};
    const response = await api.get<IntegrationListResponse>("/api/v1/integrations", { params });
    return response.data;
  },

  // Get a specific integration's connection status
  get: async (integrationId: string, workspaceId?: string): Promise<IntegrationResponse> => {
    const params = workspaceId ? { workspace_id: workspaceId } : {};
    const response = await api.get<IntegrationResponse>(`/api/v1/integrations/${integrationId}`, {
      params,
    });
    return response.data;
  },

  // Connect a new integration
  connect: async (request: ConnectIntegrationRequest): Promise<IntegrationResponse> => {
    const response = await api.post<IntegrationResponse>("/api/v1/integrations", request);
    return response.data;
  },

  // Update an integration's credentials or settings
  update: async (
    integrationId: string,
    request: UpdateIntegrationRequest,
    workspaceId?: string
  ): Promise<IntegrationResponse> => {
    const params = workspaceId ? { workspace_id: workspaceId } : {};
    const response = await api.put<IntegrationResponse>(
      `/api/v1/integrations/${integrationId}`,
      request,
      { params }
    );
    return response.data;
  },

  // Disconnect an integration
  disconnect: async (integrationId: string, workspaceId?: string): Promise<void> => {
    const params = workspaceId ? { workspace_id: workspaceId } : {};
    await api.delete(`/api/v1/integrations/${integrationId}`, { params });
  },
};

export default api;

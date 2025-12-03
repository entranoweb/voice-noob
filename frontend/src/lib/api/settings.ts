/**
 * API client for user settings
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function getAuthHeaders(): HeadersInit {
  if (typeof window === "undefined") return {};
  const token = localStorage.getItem("access_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Fetch with timeout to prevent hanging requests
 */
async function fetchWithTimeout(
  url: string,
  options: RequestInit = {},
  timeoutMs = 10000
): Promise<Response> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  // Merge auth headers with any provided headers
  const headers = {
    ...getAuthHeaders(),
    ...(options.headers ?? {}),
  };

  try {
    const response = await fetch(url, {
      ...options,
      headers,
      signal: controller.signal,
    });
    return response;
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out - please check if the backend is running");
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

export interface SettingsResponse {
  openai_api_key_set: boolean;
  deepgram_api_key_set: boolean;
  elevenlabs_api_key_set: boolean;
  telnyx_api_key_set: boolean;
  twilio_account_sid_set: boolean;
  // Azure OpenAI fields
  azure_openai_endpoint_set: boolean;
  azure_openai_api_key_set: boolean;
  azure_openai_deployment_name_set: boolean;
  openai_provider: string;
  workspace_id: string | null;
}

export interface UpdateSettingsRequest {
  openai_api_key?: string;
  deepgram_api_key?: string;
  elevenlabs_api_key?: string;
  telnyx_api_key?: string;
  telnyx_public_key?: string;
  twilio_account_sid?: string;
  twilio_auth_token?: string;
  // Azure OpenAI fields
  azure_openai_endpoint?: string;
  azure_openai_api_key?: string;
  azure_openai_deployment_name?: string;
  openai_provider?: string;
}

export async function fetchSettings(workspaceId?: string): Promise<SettingsResponse> {
  const params = workspaceId ? `?workspace_id=${workspaceId}` : "";
  const response = await fetchWithTimeout(`${API_BASE}/api/v1/settings${params}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch settings: ${response.statusText}`);
  }
  return response.json();
}

export async function updateSettings(
  request: UpdateSettingsRequest,
  workspaceId?: string
): Promise<{ message: string }> {
  const params = workspaceId ? `?workspace_id=${workspaceId}` : "";
  const response = await fetchWithTimeout(`${API_BASE}/api/v1/settings${params}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to update settings");
  }

  return response.json();
}

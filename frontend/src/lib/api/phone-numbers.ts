/**
 * API client for phone numbers
 */

import { authedFetch } from "@/lib/session";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function getAuthHeaders(): HeadersInit {
  if (typeof window === "undefined") return {};
  const token = localStorage.getItem("access_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface PhoneNumber {
  id: string;
  phone_number: string;
  friendly_name: string | null;
  provider: string;
  provider_id: string;
  workspace_id: string | null;
  workspace_name: string | null;
  assigned_agent_id: string | null;
  assigned_agent_name: string | null;
  can_receive_calls: boolean;
  can_make_calls: boolean;
  can_receive_sms: boolean;
  can_send_sms: boolean;
  status: string;
  notes: string | null;
  purchased_at: string | null;
  created_at: string;
}

export interface PhoneNumberListResponse {
  phone_numbers: PhoneNumber[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface ListPhoneNumbersParams {
  page?: number;
  page_size?: number;
  workspace_id?: string;
  status?: string;
}

export interface CreatePhoneNumberRequest {
  phone_number: string;
  friendly_name?: string;
  provider?: string;
  provider_id: string;
  workspace_id?: string;
  can_receive_calls?: boolean;
  can_make_calls?: boolean;
  can_receive_sms?: boolean;
  can_send_sms?: boolean;
  notes?: string;
}

export interface UpdatePhoneNumberRequest {
  friendly_name?: string;
  workspace_id?: string | null;
  assigned_agent_id?: string | null;
  status?: string;
  notes?: string;
}

/**
 * List phone numbers with pagination and filtering
 */
export async function listPhoneNumbers(
  params: ListPhoneNumbersParams = {}
): Promise<PhoneNumberListResponse> {
  const searchParams = new URLSearchParams();
  if (params.page) searchParams.set("page", params.page.toString());
  if (params.page_size) searchParams.set("page_size", params.page_size.toString());
  if (params.workspace_id) searchParams.set("workspace_id", params.workspace_id);
  if (params.status) searchParams.set("status", params.status);

  const response = await authedFetch(
    `${API_BASE}/api/v1/phone-numbers?${searchParams.toString()}`,
    {
      headers: getAuthHeaders(),
    }
  );

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch phone numbers");
  }

  return response.json();
}

/**
 * Get a specific phone number
 */
export async function getPhoneNumber(phoneNumberId: string): Promise<PhoneNumber> {
  const response = await authedFetch(`${API_BASE}/api/v1/phone-numbers/${phoneNumberId}`, {
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to fetch phone number");
  }

  return response.json();
}

/**
 * Create a new phone number
 */
export async function createPhoneNumber(data: CreatePhoneNumberRequest): Promise<PhoneNumber> {
  const response = await authedFetch(`${API_BASE}/api/v1/phone-numbers`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeaders(),
    },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to create phone number");
  }

  return response.json();
}

/**
 * Update a phone number
 */
export async function updatePhoneNumber(
  phoneNumberId: string,
  data: UpdatePhoneNumberRequest
): Promise<PhoneNumber> {
  const response = await authedFetch(`${API_BASE}/api/v1/phone-numbers/${phoneNumberId}`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeaders(),
    },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to update phone number");
  }

  return response.json();
}

/**
 * Delete a phone number
 */
export async function deletePhoneNumber(phoneNumberId: string): Promise<void> {
  const response = await authedFetch(`${API_BASE}/api/v1/phone-numbers/${phoneNumberId}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail ?? "Failed to delete phone number");
  }
}

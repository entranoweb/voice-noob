/**
 * Personal API tokens, for `synthiq-caller upload` and the CI jobs that run it.
 * The secret is in the create response and nowhere else.
 */

import { api } from "@/lib/api";

export interface ApiTokenSummary {
  id: string;
  name: string;
  token_prefix: string;
  scope: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface ApiTokenCreated extends ApiTokenSummary {
  token: string;
}

export const apiTokensApi = {
  list: async (): Promise<ApiTokenSummary[]> => {
    const response = await api.get<ApiTokenSummary[]>("/api/v1/auth/api-tokens");
    return response.data;
  },

  create: async (name: string, expiresInDays?: number): Promise<ApiTokenCreated> => {
    const response = await api.post<ApiTokenCreated>("/api/v1/auth/api-tokens", {
      name,
      ...(expiresInDays ? { expires_in_days: expiresInDays } : {}),
    });
    return response.data;
  },

  revoke: async (id: string): Promise<void> => {
    await api.delete(`/api/v1/auth/api-tokens/${id}`);
  },
};

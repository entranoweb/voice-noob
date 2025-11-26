import axios from "axios";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const api = axios.create({
  baseURL: API_URL,
  headers: {
    "Content-Type": "application/json",
  },
  withCredentials: true,
});

// Safely get/set localStorage with error handling
function safeGetItem(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch (error) {
    console.warn(`Failed to access localStorage for key "${key}":`, error);
    return null;
  }
}

function safeRemoveItem(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch (error) {
    console.warn(`Failed to remove localStorage key "${key}":`, error);
  }
}

// Logout function - clears auth token and redirects to login
export function logout(): void {
  safeRemoveItem("access_token");
  try {
    window.location.href = "/login";
  } catch (navError) {
    console.error("Failed to redirect to login:", navError);
  }
}

// Request interceptor for adding auth token
api.interceptors.request.use(
  (config) => {
    const token = safeGetItem("access_token");
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
    // Log authentication failures
    if (error.response?.status === 401) {
      console.error("Authentication failed - redirecting to login", {
        endpoint: error.config?.url,
        status: error.response.status,
      });
      safeRemoveItem("access_token");
      // Safely navigate to login
      try {
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

export default api;

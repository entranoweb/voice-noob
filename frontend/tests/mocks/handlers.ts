import { http, HttpResponse } from "msw";
import {
  mockContacts,
  mockCRMStats,
  mockEvaluations,
  mockDashboardMetrics,
  mockQAStatus,
  mockFailureReasons,
  mockTrendData,
} from "./data";

const API_URL = "http://localhost:8000";

export const handlers = [
  // Health check
  http.get(`${API_URL}/api/health`, () => {
    return HttpResponse.json({ status: "ok" });
  }),

  // List contacts
  http.get(`${API_URL}/crm/contacts`, () => {
    return HttpResponse.json(mockContacts);
  }),

  // Get single contact
  http.get(`${API_URL}/crm/contacts/:id`, ({ params }) => {
    const contact = mockContacts.find((c) => c.id === Number(params.id));

    if (!contact) {
      return HttpResponse.json(
        { detail: "Contact not found" },
        { status: 404 }
      );
    }

    return HttpResponse.json(contact);
  }),

  // Create contact
  http.post(`${API_URL}/crm/contacts`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;

    return HttpResponse.json(
      {
        id: mockContacts.length + 1,
        user_id: 1,
        ...body,
      },
      { status: 201 }
    );
  }),

  // CRM stats
  http.get(`${API_URL}/crm/stats`, () => {
    return HttpResponse.json(mockCRMStats);
  }),

  // Error scenario: 404
  http.get(`${API_URL}/crm/contacts/999`, () => {
    return HttpResponse.json({ detail: "Contact not found" }, { status: 404 });
  }),

  // Error scenario: 401 Unauthorized
  http.get(`${API_URL}/api/unauthorized`, () => {
    return HttpResponse.json(
      { detail: "Not authenticated" },
      { status: 401 }
    );
  }),

  // QA Status
  http.get(`${API_URL}/api/v1/qa/status`, () => {
    return HttpResponse.json(mockQAStatus);
  }),

  // QA Evaluations
  http.get(`${API_URL}/api/v1/qa/evaluations`, () => {
    return HttpResponse.json({
      evaluations: mockEvaluations,
      total: mockEvaluations.length,
      page: 1,
      page_size: 20,
      total_pages: 1,
    });
  }),

  http.get(`${API_URL}/api/v1/qa/evaluations/:id`, ({ params }) => {
    const evaluation = mockEvaluations.find((e) => e.id === params.id);
    if (!evaluation) {
      return HttpResponse.json({ detail: "Evaluation not found" }, { status: 404 });
    }
    return HttpResponse.json(evaluation);
  }),

  http.post(`${API_URL}/api/v1/qa/evaluate`, () => {
    return HttpResponse.json({
      message: "Evaluation completed successfully",
      evaluation_id: mockEvaluations[0]?.id ?? "eval-1",
      queued: false,
    });
  }),

  // QA Dashboard
  http.get(`${API_URL}/api/v1/qa/dashboard/metrics`, () => {
    return HttpResponse.json(mockDashboardMetrics);
  }),

  http.get(`${API_URL}/api/v1/qa/dashboard/trends`, () => {
    return HttpResponse.json(mockTrendData);
  }),

  http.get(`${API_URL}/api/v1/qa/dashboard/failure-reasons`, () => {
    return HttpResponse.json(mockFailureReasons);
  }),

  // QA Alerts
  http.get(`${API_URL}/api/v1/qa/alerts`, () => {
    return HttpResponse.json([]);
  }),
];

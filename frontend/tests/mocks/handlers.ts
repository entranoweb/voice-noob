import { http, HttpResponse } from "msw";
import {
  mockContacts,
  mockCRMStats,
  mockEvaluations,
  mockDashboardMetrics,
  mockQAStatus,
  mockFailureReasons,
  mockTrendData,
  mockWorkspaceQASettings,
  mockTestScenarios,
  mockTestRun,
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

  // Agents
  http.get(`${API_URL}/api/v1/agents`, () => {
    return HttpResponse.json([
      { id: "agent-1", name: "Test Agent 1" },
      { id: "agent-2", name: "Test Agent 2" },
    ]);
  }),

  // Workspaces
  http.get(`${API_URL}/api/v1/workspaces`, () => {
    return HttpResponse.json([
      { id: "ws-1", name: "Default Workspace", description: null, is_default: true },
    ]);
  }),

  // Workspace QA Settings
  http.get(`${API_URL}/api/v1/qa/workspace/:workspaceId/settings`, () => {
    return HttpResponse.json(mockWorkspaceQASettings);
  }),

  http.put(`${API_URL}/api/v1/qa/workspace/:workspaceId/settings`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({
      settings: {
        ...mockWorkspaceQASettings.settings,
        ...body,
      },
      effective_settings: {
        ...mockWorkspaceQASettings.effective_settings,
        ...body,
      },
    });
  }),

  // Test Scenarios
  http.get(`${API_URL}/api/v1/testing/scenarios`, () => {
    return HttpResponse.json(mockTestScenarios);
  }),

  http.post(`${API_URL}/api/v1/testing/scenarios`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json(
      {
        id: "scenario-new",
        ...body,
        is_built_in: false,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      { status: 201 }
    );
  }),

  http.put(`${API_URL}/api/v1/testing/scenarios/:id`, async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>;
    const scenario = mockTestScenarios.find((s) => s.id === params.id);
    if (!scenario) {
      return HttpResponse.json({ detail: "Scenario not found" }, { status: 404 });
    }
    return HttpResponse.json({
      ...scenario,
      ...body,
      updated_at: new Date().toISOString(),
    });
  }),

  http.delete(`${API_URL}/api/v1/testing/scenarios/:id`, ({ params }) => {
    const scenario = mockTestScenarios.find((s) => s.id === params.id);
    if (!scenario) {
      return HttpResponse.json({ detail: "Scenario not found" }, { status: 404 });
    }
    return HttpResponse.json({ message: "Scenario deleted" });
  }),

  http.post(`${API_URL}/api/v1/testing/scenarios/:id/duplicate`, ({ params }) => {
    const scenario = mockTestScenarios.find((s) => s.id === params.id);
    if (!scenario) {
      return HttpResponse.json({ detail: "Scenario not found" }, { status: 404 });
    }
    return HttpResponse.json({
      ...scenario,
      id: `${scenario.id}-copy`,
      name: `${scenario.name} (Copy)`,
      is_built_in: false,
    });
  }),

  http.post(`${API_URL}/api/v1/testing/scenarios/seed`, () => {
    return HttpResponse.json({
      message: "Scenarios seeded",
      scenarios_created: 5,
    });
  }),

  // Test Runs
  http.post(`${API_URL}/api/v1/testing/runs`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({
      id: "run-new",
      agent_id: body.agent_id,
      status: "pending",
      total_scenarios: (body.scenario_ids as string[])?.length ?? 0,
      passed_scenarios: 0,
      failed_scenarios: 0,
      results: [],
      created_at: new Date().toISOString(),
    });
  }),

  http.get(`${API_URL}/api/v1/testing/runs/:id`, ({ params }) => {
    if (params.id === "run-new") {
      return HttpResponse.json(mockTestRun);
    }
    return HttpResponse.json(mockTestRun);
  }),
];

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
  mockScenarioRuns,
} from "./data";

const API_URL = "http://localhost:8000";

export const handlers = [
  // Telephony phone numbers. The CRM page loads these on mount; without a
  // handler every request fell through MSW to a real backend on :8000, the
  // page never finished loading, and its assertions failed on missing text.
  http.get(`${API_URL}/api/v1/telephony/phone-numbers`, () => {
    return HttpResponse.json([]);
  }),

  // List contacts
  http.get(`${API_URL}/api/v1/crm/contacts`, () => {
    return HttpResponse.json(mockContacts);
  }),

  // Get single contact
  http.get(`${API_URL}/api/v1/crm/contacts/:id`, ({ params }) => {
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
  http.post(`${API_URL}/api/v1/crm/contacts`, async ({ request }) => {
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
  http.get(`${API_URL}/api/v1/crm/stats`, () => {
    return HttpResponse.json(mockCRMStats);
  }),

  // Error scenario: 404
  http.get(`${API_URL}/api/v1/crm/contacts/999`, () => {
    return HttpResponse.json({ detail: "Contact not found" }, { status: 404 });
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
    // The backend answers 204 with no body.
    return new HttpResponse(null, { status: 204 });
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

  // Test runs. The backend runs ONE scenario per request and stores one run
  // per scenario; there is no route that starts a group of scenarios.
  http.post(`${API_URL}/api/v1/testing/run`, async ({ request }) => {
    const body = (await request.json()) as { scenario_id: string };
    return HttpResponse.json({
      message: "Test completed",
      test_run_id: `run-${body.scenario_id}`,
      status: mockScenarioRuns[body.scenario_id]?.status ?? "passed",
    });
  }),

  http.get(`${API_URL}/api/v1/testing/runs/:id`, ({ params }) => {
    const scenarioId = String(params.id).replace(/^run-/, "");
    const run = mockScenarioRuns[scenarioId];
    if (!run) {
      return HttpResponse.json({ detail: "Test run not found" }, { status: 404 });
    }
    return HttpResponse.json(run);
  }),
];

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect } from "vitest";
import { AVAILABLE_INTEGRATIONS } from "../integrations";
import type { Integration, IntegrationType, AuthType } from "../integrations";

describe("AVAILABLE_INTEGRATIONS", () => {
  it("exports an array of integrations", () => {
    expect(Array.isArray(AVAILABLE_INTEGRATIONS)).toBe(true);
    expect(AVAILABLE_INTEGRATIONS.length).toBeGreaterThan(0);
  });

  it("contains valid integration objects", () => {
    AVAILABLE_INTEGRATIONS.forEach((integration: Integration) => {
      expect(integration).toHaveProperty("id");
      expect(integration).toHaveProperty("name");
      expect(integration).toHaveProperty("slug");
      expect(integration).toHaveProperty("description");
      expect(integration).toHaveProperty("category");
      expect(integration).toHaveProperty("authType");
      expect(integration).toHaveProperty("icon");
      expect(integration).toHaveProperty("enabled");
    });
  });

  it("has unique integration IDs", () => {
    const ids = AVAILABLE_INTEGRATIONS.map((i) => i.id);
    const uniqueIds = new Set(ids);
    expect(uniqueIds.size).toBe(ids.length);
  });

  it("has unique integration slugs", () => {
    const slugs = AVAILABLE_INTEGRATIONS.map((i) => i.slug);
    const uniqueSlugs = new Set(slugs);
    expect(uniqueSlugs.size).toBe(slugs.length);
  });

  it("all integrations are enabled", () => {
    AVAILABLE_INTEGRATIONS.forEach((integration) => {
      expect(integration.enabled).toBe(true);
    });
  });

  it("contains popular integrations", () => {
    const popularIntegrations = AVAILABLE_INTEGRATIONS.filter((i) => i.isPopular);
    expect(popularIntegrations.length).toBeGreaterThan(0);
  });

  it("has valid category values", () => {
    const validCategories: IntegrationType[] = [
      "crm",
      "calendar",
      "database",
      "productivity",
      "communication",
      "other",
    ];

    AVAILABLE_INTEGRATIONS.forEach((integration) => {
      expect(validCategories).toContain(integration.category);
    });
  });

  it("has valid auth types", () => {
    const validAuthTypes: AuthType[] = ["oauth", "api_key", "basic", "none"];

    AVAILABLE_INTEGRATIONS.forEach((integration) => {
      expect(validAuthTypes).toContain(integration.authType);
    });
  });
});

describe("CRM Integrations", () => {
  const crmIntegrations = AVAILABLE_INTEGRATIONS.filter((i) => i.category === "crm");

  it("contains CRM integrations", () => {
    expect(crmIntegrations.length).toBeGreaterThan(0);
  });

  it("includes Salesforce", () => {
    const salesforce = crmIntegrations.find((i) => i.id === "salesforce");
    expect(salesforce).toBeDefined();
    expect(salesforce?.name).toBe("Salesforce");
    expect(salesforce?.authType).toBe("api_key");
    expect(salesforce?.isPopular).toBe(true);
  });

  it("includes HubSpot", () => {
    const hubspot = crmIntegrations.find((i) => i.id === "hubspot");
    expect(hubspot).toBeDefined();
    expect(hubspot?.name).toBe("HubSpot");
    expect(hubspot?.authType).toBe("api_key");
    expect(hubspot?.isPopular).toBe(true);
  });

  it("includes Pipedrive", () => {
    const pipedrive = crmIntegrations.find((i) => i.id === "pipedrive");
    expect(pipedrive).toBeDefined();
    expect(pipedrive?.name).toBe("Pipedrive");
    expect(pipedrive?.authType).toBe("api_key");
  });

  it("Pipedrive has required fields", () => {
    const pipedrive = crmIntegrations.find((i) => i.id === "pipedrive");
    expect(pipedrive?.fields).toBeDefined();
    expect(pipedrive?.fields?.length).toBeGreaterThan(0);
  });
});

describe("Calendar Integrations", () => {
  const calendarIntegrations = AVAILABLE_INTEGRATIONS.filter((i) => i.category === "calendar");

  it("contains calendar integrations", () => {
    expect(calendarIntegrations.length).toBeGreaterThan(0);
  });

  it("includes Google Calendar", () => {
    const googleCalendar = calendarIntegrations.find((i) => i.id === "google-calendar");
    expect(googleCalendar).toBeDefined();
    expect(googleCalendar?.authType).toBe("api_key");
    expect(googleCalendar?.isPopular).toBe(true);
  });

  it("includes Microsoft Calendar", () => {
    const msCalendar = calendarIntegrations.find((i) => i.id === "microsoft-calendar");
    expect(msCalendar).toBeDefined();
    expect(msCalendar?.authType).toBe("api_key");
    expect(msCalendar?.isPopular).toBe(true);
  });

  it("includes Cal.com", () => {
    const calCom = calendarIntegrations.find((i) => i.id === "cal-com");
    expect(calCom).toBeDefined();
    expect(calCom?.authType).toBe("api_key");
    expect(calCom?.fields).toBeDefined();
  });
});

describe("Database Integrations", () => {
  const databaseIntegrations = AVAILABLE_INTEGRATIONS.filter((i) => i.category === "database");

  it("contains database integrations", () => {
    expect(databaseIntegrations.length).toBeGreaterThan(0);
  });

  it("includes Airtable", () => {
    const airtable = databaseIntegrations.find((i) => i.id === "airtable");
    expect(airtable).toBeDefined();
    expect(airtable?.authType).toBe("api_key");
  });

  it("includes Notion", () => {
    const notion = databaseIntegrations.find((i) => i.id === "notion");
    expect(notion).toBeDefined();
    expect(notion?.authType).toBe("api_key");
    expect(notion?.isPopular).toBe(true);
  });

  it("includes Google Sheets", () => {
    const sheets = databaseIntegrations.find((i) => i.id === "google-sheets");
    expect(sheets).toBeDefined();
    expect(sheets?.authType).toBe("api_key");
    expect(sheets?.isPopular).toBe(true);
  });
});

describe("Communication Integrations", () => {
  const communicationIntegrations = AVAILABLE_INTEGRATIONS.filter(
    (i) => i.category === "communication"
  );

  it("contains communication integrations", () => {
    expect(communicationIntegrations.length).toBeGreaterThan(0);
  });

  it("includes Slack", () => {
    const slack = communicationIntegrations.find((i) => i.id === "slack");
    expect(slack).toBeDefined();
    expect(slack?.authType).toBe("api_key");
    expect(slack?.isPopular).toBe(true);
  });

  it("includes Gmail", () => {
    const gmail = communicationIntegrations.find((i) => i.id === "gmail");
    expect(gmail).toBeDefined();
    expect(gmail?.authType).toBe("api_key");
    expect(gmail?.isPopular).toBe(true);
  });

  it("includes SendGrid", () => {
    const sendgrid = communicationIntegrations.find((i) => i.id === "sendgrid");
    expect(sendgrid).toBeDefined();
    expect(sendgrid?.authType).toBe("api_key");
  });
});

describe("OAuth Integrations", () => {
  const oauthIntegrations = AVAILABLE_INTEGRATIONS.filter((i) => i.authType === "oauth");

  // Note: Currently all integrations use api_key auth type
  it("OAuth integrations have scopes when applicable", () => {
    oauthIntegrations.forEach((integration) => {
      // Not all OAuth integrations require scopes in config
      if (integration.scopes) {
        expect(Array.isArray(integration.scopes)).toBe(true);
        expect(integration.scopes.length).toBeGreaterThan(0);
      }
    });
  });
});

describe("API Key Integrations", () => {
  const apiKeyIntegrations = AVAILABLE_INTEGRATIONS.filter((i) => i.authType === "api_key");

  it("contains API key integrations", () => {
    expect(apiKeyIntegrations.length).toBeGreaterThan(0);
  });

  it("API key integrations have fields", () => {
    apiKeyIntegrations.forEach((integration) => {
      expect(integration.fields).toBeDefined();
      expect(Array.isArray(integration.fields)).toBe(true);
      if (integration.fields) {
        expect(integration.fields.length).toBeGreaterThan(0);
      }
    });
  });

  it("API key fields have required properties", () => {
    apiKeyIntegrations.forEach((integration) => {
      integration.fields?.forEach((field) => {
        expect(field).toHaveProperty("name");
        expect(field).toHaveProperty("label");
        expect(field).toHaveProperty("type");
        expect(field).toHaveProperty("required");
      });
    });
  });

  it("Pipedrive has API token field", () => {
    const pipedrive = apiKeyIntegrations.find((i) => i.id === "pipedrive");
    const apiTokenField = pipedrive?.fields?.find((f) => f.name === "api_token");
    expect(apiTokenField).toBeDefined();
    expect(apiTokenField?.required).toBe(true);
    expect(apiTokenField?.type).toBe("password");
  });

  it("Stripe has API key field", () => {
    const stripe = apiKeyIntegrations.find((i) => i.id === "stripe");
    const apiKeyField = stripe?.fields?.find((f) => f.name === "api_key");
    expect(apiKeyField).toBeDefined();
    expect(apiKeyField?.required).toBe(true);
  });
});

describe("Integration Icons", () => {
  it("all integrations have icon URLs", () => {
    AVAILABLE_INTEGRATIONS.forEach((integration) => {
      expect(integration.icon).toBeDefined();
      expect(typeof integration.icon).toBe("string");
      expect(integration.icon.length).toBeGreaterThan(0);
    });
  });

  it("icon URLs use simpleicons CDN", () => {
    AVAILABLE_INTEGRATIONS.forEach((integration) => {
      expect(integration.icon).toContain("simpleicons.org");
    });
  });
});

describe("Integration Descriptions", () => {
  it("all integrations have descriptions", () => {
    AVAILABLE_INTEGRATIONS.forEach((integration) => {
      expect(integration.description).toBeDefined();
      expect(typeof integration.description).toBe("string");
      expect(integration.description.length).toBeGreaterThan(0);
    });
  });

  it("descriptions are concise (under 100 characters)", () => {
    AVAILABLE_INTEGRATIONS.forEach((integration) => {
      expect(integration.description.length).toBeLessThan(100);
    });
  });
});

describe("Popular Integrations", () => {
  const popularIntegrations = AVAILABLE_INTEGRATIONS.filter((i) => i.isPopular);

  it("contains popular integrations", () => {
    expect(popularIntegrations.length).toBeGreaterThan(5);
  });

  it("popular integrations include major platforms", () => {
    const popularIds = popularIntegrations.map((i) => i.id);
    expect(popularIds).toContain("salesforce");
    expect(popularIds).toContain("hubspot");
    expect(popularIds).toContain("google-calendar");
    expect(popularIds).toContain("notion");
    expect(popularIds).toContain("stripe");
  });
});

describe("Integration Categories Distribution", () => {
  it("has integrations in CRM category", () => {
    const crm = AVAILABLE_INTEGRATIONS.filter((i) => i.category === "crm");
    expect(crm.length).toBeGreaterThanOrEqual(4);
  });

  it("has integrations in calendar category", () => {
    const calendar = AVAILABLE_INTEGRATIONS.filter((i) => i.category === "calendar");
    expect(calendar.length).toBeGreaterThanOrEqual(3);
  });

  it("has integrations in database category", () => {
    const database = AVAILABLE_INTEGRATIONS.filter((i) => i.category === "database");
    expect(database.length).toBeGreaterThanOrEqual(3);
  });

  it("has integrations in communication category", () => {
    const communication = AVAILABLE_INTEGRATIONS.filter((i) => i.category === "communication");
    expect(communication.length).toBeGreaterThanOrEqual(4);
  });

  it("offers only call control tools the backend can actually perform", () => {
    // transfer_call and send_dtmf reported success and did nothing on a call.
    // The backend no longer defines them; the UI must not offer them.
    const callControl = AVAILABLE_INTEGRATIONS.find((i) => i.id === "call_control");
    const toolIds = (callControl?.tools ?? []).map((t) => t.id);
    expect(toolIds).toEqual(["end_call"]);
  });

  it("has integrations in productivity category", () => {
    const productivity = AVAILABLE_INTEGRATIONS.filter((i) => i.category === "productivity");
    expect(productivity.length).toBeGreaterThanOrEqual(2);
  });
});

describe("agent prompt templates", () => {
  // The templates are module constants inside Next page files, so they are read
  // as source. With transfer_call gone, an agent told to transfer would promise a
  // caller something that never happens.
  const pages = [
    "src/app/dashboard/agents/create-agent/page.tsx",
    "src/app/dashboard/agents/[id]/page.tsx",
  ];

  it.each(pages)("%s does not tell the agent to transfer a call", (page) => {
    const source = readFileSync(resolve(process.cwd(), page), "utf8");
    expect(source).not.toMatch(/transfer (you|to a human)/i);
    expect(source).toMatch(/take a message/i);
  });
});

# VoiceNoob QA Dashboard - 2025 Production Patterns Analysis

**Date:** December 22, 2025
**Purpose:** Reference for future QA dashboard improvements based on 2025 industry standards

---

## 1. Production-Grade Systems Analyzed

### LLM Evaluation Platforms

| System | Stars | Key Patterns |
|--------|-------|--------------|
| **DeepEval** | 12.2k+ | G-Eval metrics, hallucination detection, pytest-like interface |
| **Langfuse** | 18.3k+ | LLM observability, prompt management, OpenTelemetry integration |
| **Arize Phoenix** | 15.7k+ | Real-time tracing, anomaly detection, RAG debugging |
| **JudgeLM** | ICLR 2025 | LLM-as-judge scoring, side-by-side comparison |

### React Dashboard Frameworks (2025)

| Framework | Stack | Notable Features |
|-----------|-------|------------------|
| Berry Dashboard | Material-UI | Heavy data visualization |
| TailAdmin | Tailwind CSS | Minimalist metrics cards |
| Horizon UI | Chakra UI | Trend-focused visualizations |
| Admiral | React | Back office framework |

---

## 2. Current Implementation Status

### What We Have ✅

```
frontend/src/
├── app/dashboard/qa/
│   ├── page.tsx              ✅ Main dashboard (437 lines)
│   ├── loading.tsx           ✅ Loading skeleton (84 lines)
│   └── __tests__/page.test.tsx ✅ 6 tests
├── components/qa/
│   ├── evaluation-list.tsx   ✅ Table with pass/fail (118 lines)
│   ├── qa-metrics-chart.tsx  ✅ Trend + breakdown (124 lines)
│   ├── test-runner.tsx       ✅ Run scenarios (220 lines)
│   └── __tests__/evaluation-list.test.tsx ✅ 8 tests
└── lib/api/
    └── qa.ts                 ✅ 15+ API functions (535 lines)
```

**Features Implemented:**
- Pass rate, average score, total evaluations, failed count
- Score breakdown (intent, tool usage, compliance, response quality)
- Top failure reasons with counts
- Recent evaluations list with pass/fail badges
- Time range filters (7d, 14d, 30d, 90d)
- Agent filter dropdown
- Basic trend visualization (simple bar chart)
- Color-coded scores (green ≥80, yellow ≥60, red <60)
- QA disabled state handling
- Loading skeletons
- 21 frontend tests

---

## 3. What We're Missing (vs 2025 Standards)

### Priority 1 - High Impact

#### 3.1 Interactive Visualizations
**Current:** Simple bar chart with div elements
```tsx
// Current implementation
<div className="h-32 flex items-end gap-1">
  {trends.values.map((value, index) => (
    <div style={{ height: `${value}%` }} />
  ))}
</div>
```

**2025 Standard:** Recharts/Chart.js with interactivity
```tsx
// Production pattern
<ResponsiveContainer width="100%" height={300}>
  <LineChart data={trendData}>
    <CartesianGrid strokeDasharray="3 3" />
    <XAxis dataKey="date" />
    <YAxis domain={[0, 100]} />
    <Tooltip />
    <Legend />
    <Line type="monotone" dataKey="score" stroke="#8884d8" />
  </LineChart>
</ResponsiveContainer>
```

**Action:** Install `recharts` and replace simple bars

---

#### 3.2 Evaluation Detail Page
**Current:** Missing `/dashboard/qa/[evaluationId]/page.tsx`

**2025 Standard:** Drill-down detail view showing:
- Full score breakdown (all 8 metrics)
- Transcript alongside evaluation
- Turn-by-turn analysis
- Failure reasons with explanations
- Recommendations
- Objectives detected vs completed
- Sentiment progression chart
- Cost and latency metrics

**Action:** Create `[evaluationId]/page.tsx` with full evaluation display

---

#### 3.3 Quality Metrics Display
**Current:** Only showing 4 metrics (intent, tools, compliance, response quality)

**2025 Standard:** Full promptflow-style metrics:
- Coherence (0-100)
- Relevance (0-100)
- Groundedness (0-100)
- Fluency (0-100)
- Sentiment distribution (pie/donut chart)
- Escalation risk indicator

**Action:** Add quality metrics section to dashboard

---

### Priority 2 - Enhanced UX

#### 3.4 Agent Comparison View
**Current:** No comparison feature (API exists: `getAgentComparison`)

**2025 Standard:**
- Side-by-side agent performance table
- Comparative bar charts
- Trend comparison over time
- Statistical significance indicators

---

#### 3.5 Advanced Filtering
**Current:** Time range presets + agent filter only

**2025 Standard:**
- Date range picker (react-day-picker)
- Pass/fail toggle filter
- Workspace filter
- Search by call ID
- Score range filter
- Category filter

---

#### 3.6 Export Capabilities
**Current:** None

**2025 Standard:**
- CSV export for metrics
- JSON export for evaluations
- PDF report generation
- Scheduled email reports

---

#### 3.7 Latency Visualization
**Current:** Not displayed (data exists in API)

**2025 Standard:**
- P50/P90/P95 latency display
- Latency trend chart
- Evaluation cost tracking
- Cost per evaluation breakdown

---

### Priority 3 - Production Features

#### 3.8 Real-time Updates
**Current:** Static data, manual refresh

**2025 Standard:**
- WebSocket for live evaluation updates
- "Evaluating..." status badges
- Auto-refresh metrics (configurable interval)
- Toast notifications for completed evaluations

---

#### 3.9 Settings Configuration UI
**Current:** Missing `qa-settings-dialog.tsx` (settings via .env only)

**2025 Standard:**
- Threshold configuration sliders
- Alert settings (webhook URLs, Slack)
- Auto-evaluation toggle
- Evaluation model selection
- Cost budget limits

---

#### 3.10 Collaboration Features
**Current:** None

**2025 Standard:**
- Add comments to evaluations
- Share evaluation links
- Manual review workflow
- Annotation tools
- Team activity feed

---

#### 3.11 Test Scenario Management
**Current:** TestRunner can run scenarios, but no management UI

**2025 Standard:**
- Scenario list with categories
- Scenario builder/editor
- Custom scenario creation
- Scenario performance history
- A/B test configuration

---

## 4. Implementation Roadmap

### Phase 1: Quick Wins (1-2 days)
- [ ] Install recharts: `npm install recharts`
- [ ] Replace simple bar chart with LineChart
- [ ] Add quality metrics section (coherence, relevance, groundedness, fluency)
- [ ] Display latency P50/P90/P95

### Phase 2: Detail View (2-3 days)
- [ ] Create `[evaluationId]/page.tsx`
- [ ] Full evaluation breakdown display
- [ ] Transcript integration
- [ ] Turn-by-turn analysis view

### Phase 3: Enhanced Filtering (1-2 days)
- [ ] Add date range picker
- [ ] Add pass/fail filter toggle
- [ ] Add search by call ID
- [ ] Add workspace filter

### Phase 4: Agent Comparison (1-2 days)
- [ ] Create agent comparison component
- [ ] Side-by-side metrics table
- [ ] Comparative charts

### Phase 5: Export & Settings (2-3 days)
- [ ] CSV export button
- [ ] PDF report generation
- [ ] Settings dialog component
- [ ] Threshold configuration UI

### Phase 6: Real-time & Collaboration (3-5 days)
- [ ] WebSocket integration
- [ ] Live status indicators
- [ ] Comment system
- [ ] Share links

---

## 5. Dependencies to Add

```json
{
  "recharts": "^2.12.0",
  "react-day-picker": "^8.10.0",
  "date-fns": "^3.3.0",
  "jspdf": "^2.5.1",
  "papaparse": "^5.4.1"
}
```

---

## 6. Reference Implementations

### DeepEval Dashboard
- GitHub: https://github.com/confident-ai/deepeval
- Features: Metric cards, test results table, pytest integration

### Langfuse UI
- GitHub: https://github.com/langfuse/langfuse
- Features: Traces view, prompt playground, cost tracking

### Arize Phoenix
- GitHub: https://github.com/Arize-ai/phoenix
- Features: Real-time tracing, anomaly charts, RAG debugging

### OpenAI Testing Agent Demo
- GitHub: https://github.com/openai/openai-testing-agent-demo
- Features: Test configuration forms, real-time execution status

---

## 7. Key Takeaways

1. **Interactivity is expected** - 2025 dashboards use proper charting libraries with tooltips, zoom, and drill-down
2. **Detail views are essential** - Users expect to click and see full breakdowns
3. **Real-time is standard** - WebSocket updates are common in production systems
4. **Export is required** - CSV/PDF export is baseline functionality
5. **Cost tracking matters** - LLM evaluation costs should be visible
6. **Collaboration is growing** - Comments, sharing, and team features are emerging

---

*Document created: December 22, 2025*
*Last updated: December 22, 2025*

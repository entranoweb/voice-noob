# VoiceNoob QA Framework - End-to-End Test Checklist

**Date:** December 20, 2025
**Purpose:** Manual verification steps for QA Framework functionality

---

## Prerequisites

1. PostgreSQL and Redis running (via Docker or local install)
2. Backend server running on `localhost:8000`
3. Frontend running on `localhost:3000`
4. Valid Anthropic API key

---

## Configuration Setup

### 1. Set Environment Variables

Create or update `backend/.env` with:

```env
# QA Framework Configuration
QA_ENABLED=true
QA_AUTO_EVALUATE=true
QA_DEFAULT_THRESHOLD=70
ANTHROPIC_API_KEY=sk-ant-your-actual-key-here
```

### 2. Verify Configuration

```bash
curl http://localhost:8000/api/v1/qa/status
```

Expected response:
```json
{
  "enabled": true,
  "auto_evaluate": true,
  "evaluation_model": "claude-sonnet-4-20250514",
  "default_threshold": 70,
  "api_key_configured": true
}
```

---

## Test Scenarios

### Test 1: Seed Built-in Scenarios

**Steps:**
1. Login to the dashboard
2. Call the seed endpoint:
   ```bash
   curl -X POST http://localhost:8000/api/v1/testing/scenarios/seed \
     -H "Authorization: Bearer $TOKEN"
   ```

**Expected Result:**
- Response shows `scenarios_created: 15` (or 0 if already seeded)
- GET `/api/v1/testing/scenarios` returns all 15 built-in scenarios

---

### Test 2: Create Custom Scenario

**Steps:**
```bash
curl -X POST http://localhost:8000/api/v1/testing/scenarios \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Custom Test Scenario",
    "description": "Test scenario created via API",
    "category": "happy_path",
    "difficulty": "easy",
    "caller_persona": {"name": "Test User", "mood": "friendly"},
    "conversation_flow": [{"speaker": "user", "message": "Hello"}],
    "expected_behaviors": ["Greet the caller"],
    "success_criteria": {"min_score": 70},
    "tags": ["custom", "test"]
  }'
```

**Expected Result:**
- Returns 201 with created scenario
- `is_built_in: false`
- `user_id` matches current user

---

### Test 3: Update Custom Scenario

**Steps:**
```bash
curl -X PUT http://localhost:8000/api/v1/testing/scenarios/{scenario_id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Updated Scenario Name"}'
```

**Expected Result:**
- Returns 200 with updated scenario
- Name is changed

---

### Test 4: Delete Custom Scenario

**Steps:**
```bash
curl -X DELETE http://localhost:8000/api/v1/testing/scenarios/{scenario_id} \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Result:**
- Returns 204 No Content
- Scenario no longer appears in list

---

### Test 5: Cannot Modify Built-in Scenarios

**Steps:**
```bash
# Get a built-in scenario ID
curl http://localhost:8000/api/v1/testing/scenarios?built_in_only=true

# Try to update it
curl -X PUT http://localhost:8000/api/v1/testing/scenarios/{built_in_id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "Hacked Name"}'
```

**Expected Result:**
- Returns 403 Forbidden: "Cannot modify built-in scenarios"

---

### Test 6: Post-Call Evaluation Flow

**Steps:**
1. Create an agent in the dashboard
2. Make a test call through the platform
3. Wait for call to complete (status: `completed`)
4. Check for automatic evaluation:
   ```bash
   curl http://localhost:8000/api/v1/qa/calls/{call_id}/evaluation \
     -H "Authorization: Bearer $TOKEN"
   ```

**Expected Result (if QA_AUTO_EVALUATE=true):**
- Evaluation exists with scores
- `passed` field reflects score vs threshold

---

### Test 7: Manual Evaluation Trigger

**Steps:**
```bash
# For a call that hasn't been evaluated yet
curl -X POST http://localhost:8000/api/v1/qa/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"call_id": "{call_uuid}"}'
```

**Expected Result:**
- Returns evaluation with all scores
- `evaluation_latency_ms` and `evaluation_cost_cents` populated

---

### Test 8: Batch Evaluation

**Steps:**
```bash
curl -X POST http://localhost:8000/api/v1/qa/evaluate/batch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "call_ids": ["{call_id_1}", "{call_id_2}", "{call_id_3}"],
    "max_concurrent": 3
  }'
```

**Expected Result:**
- Returns `{"queued": 3, "message": "Batch evaluation queued for 3 calls"}`
- After some time, all calls have evaluations

---

### Test 9: QA Dashboard UI

**Steps:**
1. Navigate to `/dashboard/qa` in the browser
2. Verify the following elements appear:
   - [ ] "QA Dashboard" heading
   - [ ] Pass Rate metric card
   - [ ] Average Score metric card
   - [ ] Total Evaluations count
   - [ ] Failed Calls count
   - [ ] Top Failure Reasons section
   - [ ] Score Breakdown section

**Expected Result:**
- All UI elements render correctly
- Data matches API responses

---

### Test 10: QA Disabled State

**Steps:**
1. Set `QA_ENABLED=false` in environment
2. Restart backend
3. Navigate to `/dashboard/qa`

**Expected Result:**
- Shows "QA Testing Disabled" message
- No metrics are fetched

---

### Test 11: New Scenario Categories

**Steps:**
```bash
curl http://localhost:8000/api/v1/testing/categories \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Result:**
- Categories include: `happy_path`, `stress` (new additions)
- Full list: `greeting`, `booking`, `objection`, `support`, `compliance`, `edge_case`, `transfer`, `information`, `happy_path`, `stress`

---

### Test 12: Filter Scenarios by New Categories

**Steps:**
```bash
# Filter by happy_path
curl "http://localhost:8000/api/v1/testing/scenarios?category=happy_path" \
  -H "Authorization: Bearer $TOKEN"

# Filter by stress
curl "http://localhost:8000/api/v1/testing/scenarios?category=stress" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Result:**
- happy_path returns 1 scenario: "Simple Appointment Booking"
- stress returns 2 scenarios: "Rapid-Fire Questions", "Long Silence Handler"

---

## Failure Alerts

### Test 13: Alert on Low Score

**Steps:**
1. Trigger an evaluation that results in a score below threshold
2. Check alerts:
   ```bash
   curl "http://localhost:8000/api/v1/qa/alerts?workspace_id={workspace_id}" \
     -H "Authorization: Bearer $TOKEN"
   ```

**Expected Result:**
- Alert exists with type `qa_failure`
- Contains evaluation details in metadata

---

## Cleanup

After testing, reset to default configuration if needed:

```env
QA_ENABLED=false
```

---

## Test Results Summary

| Test | Status | Notes |
|------|--------|-------|
| 1. Seed Scenarios | [ ] | |
| 2. Create Custom | [ ] | |
| 3. Update Custom | [ ] | |
| 4. Delete Custom | [ ] | |
| 5. Built-in Protection | [ ] | |
| 6. Auto Evaluation | [ ] | |
| 7. Manual Evaluation | [ ] | |
| 8. Batch Evaluation | [ ] | |
| 9. Dashboard UI | [ ] | |
| 10. Disabled State | [ ] | |
| 11. New Categories | [ ] | |
| 12. Category Filters | [ ] | |
| 13. Failure Alerts | [ ] | |

---

*Document Version: 1.0*
*Last Updated: December 20, 2025*

"""Pre-built test scenarios for QA Testing Framework.

12 comprehensive test scenarios covering common voice agent use cases.
"""

from typing import Any

# Built-in test scenarios
BUILT_IN_SCENARIOS: list[dict[str, Any]] = [
    # ==========================================================================
    # GREETING SCENARIOS
    # ==========================================================================
    {
        "name": "Basic Greeting - Friendly Caller",
        "description": "Test agent's initial greeting and ability to establish rapport with a friendly caller",
        "category": "greeting",
        "difficulty": "easy",
        "caller_persona": {
            "name": "Sarah",
            "mood": "friendly",
            "speaking_style": "casual",
            "context": "First-time caller, curious about services",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "Hi there!"},
            {"speaker": "user", "message": "I was just wondering what you guys do?"},
        ],
        "expected_behaviors": [
            "Greet the caller warmly",
            "Introduce the company or service",
            "Ask how they can help",
            "Maintain friendly tone throughout",
        ],
        "expected_tool_calls": None,
        "success_criteria": {
            "min_score": 70,
            "must_include": ["greeting", "introduction"],
            "must_not_include": ["rude language", "abrupt ending"],
        },
        "tags": ["greeting", "intro", "friendly"],
    },
    {
        "name": "Basic Greeting - Impatient Caller",
        "description": "Test agent's ability to handle an impatient caller who wants quick answers",
        "category": "greeting",
        "difficulty": "medium",
        "caller_persona": {
            "name": "Mike",
            "mood": "impatient",
            "speaking_style": "direct",
            "context": "Busy professional, limited time",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "Yeah hi, I need quick help."},
            {
                "speaker": "user",
                "message": "Can you just tell me your hours? I don't have much time.",
            },
        ],
        "expected_behaviors": [
            "Acknowledge urgency",
            "Provide concise response",
            "Avoid lengthy explanations",
            "Offer to help quickly",
        ],
        "expected_tool_calls": None,
        "success_criteria": {
            "min_score": 70,
            "must_include": ["hours", "availability"],
            "response_time_limit_seconds": 5,
        },
        "tags": ["greeting", "impatient", "time-sensitive"],
    },
    # ==========================================================================
    # BOOKING SCENARIOS
    # ==========================================================================
    {
        "name": "Appointment Booking - Standard",
        "description": "Test agent's ability to book an appointment with all required information",
        "category": "booking",
        "difficulty": "medium",
        "caller_persona": {
            "name": "Jennifer",
            "mood": "neutral",
            "speaking_style": "polite",
            "context": "Wants to book a consultation",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "Hi, I'd like to book an appointment please."},
            {"speaker": "user", "message": "I'm available next Tuesday afternoon."},
            {
                "speaker": "user",
                "message": "My name is Jennifer Smith and my number is 555-123-4567.",
            },
        ],
        "expected_behaviors": [
            "Ask for preferred date/time",
            "Collect contact information",
            "Confirm appointment details",
            "Provide confirmation",
        ],
        "expected_tool_calls": [
            {"tool": "book_appointment", "required_args": ["date", "time", "name"]},
        ],
        "success_criteria": {
            "min_score": 75,
            "must_invoke_tools": ["book_appointment"],
            "must_collect": ["name", "phone", "preferred_time"],
        },
        "tags": ["booking", "appointment", "standard"],
    },
    {
        "name": "Appointment Booking - Conflicting Schedule",
        "description": "Test agent's ability to handle scheduling conflicts and offer alternatives",
        "category": "booking",
        "difficulty": "hard",
        "caller_persona": {
            "name": "Robert",
            "mood": "flexible",
            "speaking_style": "conversational",
            "context": "Wants a specific time that's unavailable",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "I need an appointment for Monday at 2pm."},
            {
                "speaker": "user",
                "message": "That's the only time I can do Monday. What about Tuesday?",
            },
            {"speaker": "user", "message": "Tuesday 3pm works. My name is Robert Chen."},
        ],
        "expected_behaviors": [
            "Check availability",
            "Explain conflict politely",
            "Offer alternative times",
            "Confirm alternative booking",
        ],
        "expected_tool_calls": [
            {"tool": "check_availability", "required_args": ["date"]},
            {"tool": "book_appointment", "required_args": ["date", "time"]},
        ],
        "success_criteria": {
            "min_score": 75,
            "must_handle": ["scheduling_conflict"],
            "must_offer": ["alternatives"],
        },
        "tags": ["booking", "conflict", "alternatives"],
    },
    # ==========================================================================
    # OBJECTION HANDLING SCENARIOS
    # ==========================================================================
    {
        "name": "Price Objection - Too Expensive",
        "description": "Test agent's ability to handle price objections professionally",
        "category": "objection",
        "difficulty": "hard",
        "caller_persona": {
            "name": "David",
            "mood": "skeptical",
            "speaking_style": "direct",
            "context": "Interested but concerned about cost",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "How much does your service cost?"},
            {
                "speaker": "user",
                "message": "That's way too expensive! Your competitor charges half that.",
            },
            {"speaker": "user", "message": "What makes you worth the extra money?"},
        ],
        "expected_behaviors": [
            "Provide clear pricing",
            "Acknowledge concern without being defensive",
            "Explain value proposition",
            "Focus on benefits, not just features",
        ],
        "expected_tool_calls": None,
        "success_criteria": {
            "min_score": 70,
            "must_not_include": ["defensive language", "dismissive tone"],
            "must_include": ["value", "benefits"],
        },
        "tags": ["objection", "pricing", "value"],
    },
    {
        "name": "Trust Objection - Skeptical Caller",
        "description": "Test agent's ability to build trust with a skeptical caller",
        "category": "objection",
        "difficulty": "hard",
        "caller_persona": {
            "name": "Linda",
            "mood": "skeptical",
            "speaking_style": "questioning",
            "context": "Had bad experience with similar service before",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "I've been burned before by companies like yours."},
            {"speaker": "user", "message": "How do I know you're actually going to deliver?"},
            {"speaker": "user", "message": "Do you have any guarantees?"},
        ],
        "expected_behaviors": [
            "Acknowledge past negative experience",
            "Express empathy",
            "Provide credibility indicators",
            "Explain guarantees or policies",
        ],
        "expected_tool_calls": None,
        "success_criteria": {
            "min_score": 70,
            "must_include": ["empathy", "credibility"],
            "sentiment_should_improve": True,
        },
        "tags": ["objection", "trust", "skeptical"],
    },
    # ==========================================================================
    # SUPPORT SCENARIOS
    # ==========================================================================
    {
        "name": "Technical Support - Simple Issue",
        "description": "Test agent's ability to troubleshoot a simple technical issue",
        "category": "support",
        "difficulty": "medium",
        "caller_persona": {
            "name": "Tom",
            "mood": "frustrated",
            "speaking_style": "straightforward",
            "context": "Having trouble logging in",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "I can't log into my account!"},
            {
                "speaker": "user",
                "message": "I've tried my password three times and it's not working.",
            },
            {"speaker": "user", "message": "My email is tom@example.com"},
        ],
        "expected_behaviors": [
            "Express understanding of frustration",
            "Ask clarifying questions",
            "Provide step-by-step guidance",
            "Offer password reset if needed",
        ],
        "expected_tool_calls": [
            {"tool": "search_customer", "required_args": ["email"]},
        ],
        "success_criteria": {
            "min_score": 75,
            "must_provide": ["solution", "next_steps"],
            "must_show": ["empathy"],
        },
        "tags": ["support", "technical", "login"],
    },
    {
        "name": "Billing Support - Dispute",
        "description": "Test agent's ability to handle a billing dispute professionally",
        "category": "support",
        "difficulty": "hard",
        "caller_persona": {
            "name": "Amanda",
            "mood": "angry",
            "speaking_style": "assertive",
            "context": "Charged incorrectly, wants refund",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "I was charged twice for my subscription!"},
            {"speaker": "user", "message": "This is ridiculous. I want a refund right now."},
            {"speaker": "user", "message": "My account email is amanda@example.com"},
        ],
        "expected_behaviors": [
            "Apologize for inconvenience",
            "Verify account information",
            "Investigate the issue",
            "Explain resolution process",
        ],
        "expected_tool_calls": [
            {"tool": "search_customer", "required_args": ["email"]},
        ],
        "success_criteria": {
            "min_score": 70,
            "must_include": ["apology", "investigation"],
            "must_not_escalate_anger": True,
        },
        "tags": ["support", "billing", "dispute"],
    },
    # ==========================================================================
    # COMPLIANCE SCENARIOS
    # ==========================================================================
    {
        "name": "Privacy Request - Data Inquiry",
        "description": "Test agent's ability to handle GDPR/privacy-related requests",
        "category": "compliance",
        "difficulty": "medium",
        "caller_persona": {
            "name": "Emma",
            "mood": "concerned",
            "speaking_style": "formal",
            "context": "Wants to know what data is stored",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "I want to know what personal data you have about me."},
            {"speaker": "user", "message": "Is my data being shared with third parties?"},
        ],
        "expected_behaviors": [
            "Acknowledge privacy concern",
            "Explain data handling policies",
            "Provide information on data access",
            "Offer to escalate to privacy team",
        ],
        "expected_tool_calls": None,
        "success_criteria": {
            "min_score": 80,
            "must_include": ["privacy_policy", "data_rights"],
            "compliance_required": True,
        },
        "tags": ["compliance", "privacy", "gdpr"],
    },
    {
        "name": "Do Not Call Request",
        "description": "Test agent's ability to handle opt-out requests properly",
        "category": "compliance",
        "difficulty": "easy",
        "caller_persona": {
            "name": "Frank",
            "mood": "annoyed",
            "speaking_style": "curt",
            "context": "Wants to stop receiving calls",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "Stop calling me! Take me off your list."},
            {"speaker": "user", "message": "I don't want any more calls from you people."},
        ],
        "expected_behaviors": [
            "Acknowledge request immediately",
            "Apologize for inconvenience",
            "Confirm removal from list",
            "Provide timeframe for removal",
        ],
        "expected_tool_calls": None,
        "success_criteria": {
            "min_score": 90,
            "must_comply_with": ["opt_out_request"],
            "must_not": ["argue", "try_to_retain"],
        },
        "tags": ["compliance", "dnc", "opt-out"],
    },
    # ==========================================================================
    # EDGE CASE SCENARIOS
    # ==========================================================================
    {
        "name": "Unclear Intent - Confused Caller",
        "description": "Test agent's ability to clarify unclear or confused caller requests",
        "category": "edge_case",
        "difficulty": "hard",
        "caller_persona": {
            "name": "George",
            "mood": "confused",
            "speaking_style": "rambling",
            "context": "Elderly caller, not sure what they need",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "Um, hello? Is this the... the place for the thing?"},
            {
                "speaker": "user",
                "message": "My daughter told me to call about... something with my account maybe?",
            },
            {"speaker": "user", "message": "I'm not sure, I have this paper here somewhere..."},
        ],
        "expected_behaviors": [
            "Be patient and understanding",
            "Ask gentle clarifying questions",
            "Help narrow down the issue",
            "Offer to help find the right department",
        ],
        "expected_tool_calls": None,
        "success_criteria": {
            "min_score": 70,
            "must_show": ["patience", "clarity"],
            "must_not": ["rush", "frustration"],
        },
        "tags": ["edge_case", "confused", "patience"],
    },
    {
        "name": "Language Barrier - Non-Native Speaker",
        "description": "Test agent's ability to communicate with non-native English speakers",
        "category": "edge_case",
        "difficulty": "hard",
        "caller_persona": {
            "name": "Maria",
            "mood": "nervous",
            "speaking_style": "broken_english",
            "context": "ESL speaker needing assistance",
        },
        "conversation_flow": [
            {"speaker": "user", "message": "Hello, I am need help please. My English not so good."},
            {"speaker": "user", "message": "I want... how you say... make appointment?"},
            {"speaker": "user", "message": "For next week, yes? Tuesday maybe?"},
        ],
        "expected_behaviors": [
            "Speak clearly and slowly",
            "Use simple language",
            "Confirm understanding frequently",
            "Be patient and encouraging",
        ],
        "expected_tool_calls": [
            {"tool": "book_appointment", "required_args": ["date"]},
        ],
        "success_criteria": {
            "min_score": 70,
            "must_show": ["patience", "clarity", "simplicity"],
            "must_complete": ["appointment_booking"],
        },
        "tags": ["edge_case", "language", "esl"],
    },
]


def get_built_in_scenarios() -> list[dict[str, Any]]:
    """Get all built-in test scenarios.

    Returns:
        List of scenario dictionaries with is_built_in=True
    """
    return [{**scenario, "is_built_in": True, "is_active": True} for scenario in BUILT_IN_SCENARIOS]


def get_scenarios_by_category(category: str) -> list[dict[str, Any]]:
    """Get built-in scenarios filtered by category.

    Args:
        category: Scenario category to filter by

    Returns:
        List of matching scenarios
    """
    return [s for s in BUILT_IN_SCENARIOS if s["category"] == category]


def get_scenarios_by_difficulty(difficulty: str) -> list[dict[str, Any]]:
    """Get built-in scenarios filtered by difficulty.

    Args:
        difficulty: Difficulty level (easy, medium, hard)

    Returns:
        List of matching scenarios
    """
    return [s for s in BUILT_IN_SCENARIOS if s["difficulty"] == difficulty]

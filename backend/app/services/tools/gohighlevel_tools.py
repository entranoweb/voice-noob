"""GoHighLevel CRM tools for voice agents.

Provides tools for:
- Searching and managing contacts
- Calendar/appointment booking
- Opportunity management
- Tagging contacts

API Docs: https://marketplace.gohighlevel.com/docs/
Base URL: https://services.leadconnectorhq.com/
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from http import HTTPStatus
from typing import Any

import httpx
import structlog

from app.core.log_redaction import describe_text, log_exception
from app.core.safe_errors import tool_failure, upstream_failure
from app.services.tools.path_ids import encode_path_id, invalid_id_result

# Type alias for tool handler functions
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

logger = structlog.get_logger()

# GoHighLevel API base URL
GHL_BASE_URL = "https://services.leadconnectorhq.com"


class GoHighLevelTools:
    """GoHighLevel CRM tools for voice agents.

    Provides tools for:
    - Looking up contacts by phone/email/name
    - Creating new contacts
    - Checking calendar availability
    - Booking appointments
    - Creating opportunities
    - Adding tags to contacts
    """

    def __init__(self, access_token: str, location_id: str) -> None:
        """Initialize GoHighLevel tools.

        Args:
            access_token: GHL API key or Private Integration Token
            location_id: GHL sub-account/location ID
        """
        self.access_token = access_token
        self.location_id = location_id
        self.logger = logger.bind(component="gohighlevel_tools", location_id=location_id)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=GHL_BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                    "Version": "2021-07-28",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        """Get OpenAI function calling tool definitions.

        Returns:
            List of tool definitions for GPT Realtime API
        """
        return [
            {
                "type": "function",
                "name": "ghl_search_contact",
                "description": "Search for a contact in GoHighLevel by phone number, email, or name",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Phone number, email, or name to search for",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "type": "function",
                "name": "ghl_get_contact",
                "description": "Get full details of a contact by their GoHighLevel contact ID",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "GoHighLevel contact ID",
                        },
                    },
                    "required": ["contact_id"],
                },
            },
            {
                "type": "function",
                "name": "ghl_create_contact",
                "description": "Create a new contact in GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "first_name": {"type": "string", "description": "First name"},
                        "last_name": {"type": "string", "description": "Last name"},
                        "phone": {"type": "string", "description": "Phone number"},
                        "email": {"type": "string", "description": "Email address"},
                        "company_name": {"type": "string", "description": "Company name"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags to add to the contact",
                        },
                    },
                    "required": ["first_name", "phone"],
                },
            },
            {
                "type": "function",
                "name": "ghl_update_contact",
                "description": "Update an existing contact in GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string", "description": "Contact ID to update"},
                        "first_name": {"type": "string", "description": "First name"},
                        "last_name": {"type": "string", "description": "Last name"},
                        "phone": {"type": "string", "description": "Phone number"},
                        "email": {"type": "string", "description": "Email address"},
                        "company_name": {"type": "string", "description": "Company name"},
                    },
                    "required": ["contact_id"],
                },
            },
            {
                "type": "function",
                "name": "ghl_add_contact_tags",
                "description": "Add tags to a contact in GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string", "description": "Contact ID"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags to add",
                        },
                    },
                    "required": ["contact_id", "tags"],
                },
            },
            {
                "type": "function",
                "name": "ghl_get_calendars",
                "description": "Get list of available calendars in GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            {
                "type": "function",
                "name": "ghl_get_calendar_slots",
                "description": "Get available appointment slots for a calendar on a specific date",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "calendar_id": {"type": "string", "description": "Calendar ID"},
                        "start_date": {
                            "type": "string",
                            "description": "Start date in YYYY-MM-DD format",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "End date in YYYY-MM-DD format (defaults to start_date)",
                        },
                        "timezone": {
                            "type": "string",
                            "description": "Timezone (e.g., America/New_York). Defaults to UTC.",
                        },
                    },
                    "required": ["calendar_id", "start_date"],
                },
            },
            {
                "type": "function",
                "name": "ghl_book_appointment",
                "description": "Book an appointment in GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "calendar_id": {"type": "string", "description": "Calendar ID"},
                        "contact_id": {"type": "string", "description": "Contact ID"},
                        "start_time": {
                            "type": "string",
                            "description": "Start time in ISO 8601 format (e.g., 2024-01-15T10:00:00)",
                        },
                        "end_time": {
                            "type": "string",
                            "description": "End time in ISO 8601 format",
                        },
                        "title": {"type": "string", "description": "Appointment title"},
                        "notes": {"type": "string", "description": "Additional notes"},
                    },
                    "required": ["calendar_id", "contact_id", "start_time", "end_time"],
                },
            },
            {
                "type": "function",
                "name": "ghl_get_appointments",
                "description": "Get appointments for a contact from GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string", "description": "Contact ID"},
                    },
                    "required": ["contact_id"],
                },
            },
            {
                "type": "function",
                "name": "ghl_cancel_appointment",
                "description": "Cancel an appointment in GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "Appointment/event ID"},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "type": "function",
                "name": "ghl_create_opportunity",
                "description": "Create a new opportunity/deal in GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {"type": "string", "description": "Contact ID"},
                        "pipeline_id": {"type": "string", "description": "Pipeline ID"},
                        "stage_id": {"type": "string", "description": "Stage ID in the pipeline"},
                        "name": {"type": "string", "description": "Opportunity name"},
                        "monetary_value": {
                            "type": "number",
                            "description": "Deal value in dollars",
                        },
                    },
                    "required": ["contact_id", "pipeline_id", "stage_id", "name"],
                },
            },
            {
                "type": "function",
                "name": "ghl_get_pipelines",
                "description": "Get list of sales pipelines from GoHighLevel",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        ]

    async def ghl_search_contact(self, query: str) -> dict[str, Any]:
        """Search for a contact by phone, email, or name.

        Args:
            query: Search query

        Returns:
            Contact information or error
        """
        try:
            client = await self._get_client()

            # GHL search endpoint
            response = await client.get(
                "/contacts/",
                params={
                    "locationId": self.location_id,
                    "query": query,
                    "limit": 5,
                },
            )

            if response.status_code != HTTPStatus.OK:
                self.logger.warning(
                    "ghl_search_contact_failed",
                    status_code=response.status_code,
                    response=describe_text(response.text),
                )
                return {"success": False, "error": f"API error: {response.status_code}"}

            data = response.json()
            contacts = data.get("contacts", [])

            if not contacts:
                return {
                    "success": True,
                    "found": False,
                    "message": f"No contact found matching '{query}'",
                }

            # Format results
            contact_list = [
                {
                    "id": c.get("id"),
                    "name": f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
                    "phone": c.get("phone"),
                    "email": c.get("email"),
                    "company": c.get("companyName"),
                    "tags": c.get("tags", []),
                }
                for c in contacts[:3]
            ]

            return {
                "success": True,
                "found": True,
                "count": len(contact_list),
                "contacts": contact_list,
            }

        except Exception as e:
            log_exception(self.logger, "ghl_search_contact_error", e, query=describe_text(query))
            return tool_failure(e, "CRM")

    async def ghl_get_contact(self, contact_id: str) -> dict[str, Any]:
        """Get full contact details.

        Args:
            contact_id: GHL contact ID

        Returns:
            Contact details or error
        """
        safe_contact_id = encode_path_id(contact_id)
        if safe_contact_id is None:
            return invalid_id_result("contact_id")
        try:
            client = await self._get_client()
            response = await client.get(f"/contacts/{safe_contact_id}")

            if response.status_code != HTTPStatus.OK:
                return {"success": False, "error": f"Contact not found: {response.status_code}"}

            data = response.json()
            contact = data.get("contact", {})

            return {
                "success": True,
                "contact": {
                    "id": contact.get("id"),
                    "first_name": contact.get("firstName"),
                    "last_name": contact.get("lastName"),
                    "name": f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip(),
                    "phone": contact.get("phone"),
                    "email": contact.get("email"),
                    "company": contact.get("companyName"),
                    "tags": contact.get("tags", []),
                    "source": contact.get("source"),
                    "created_at": contact.get("dateAdded"),
                },
            }

        except Exception as e:
            log_exception(self.logger, "ghl_get_contact_error", e, contact_id=contact_id)
            return tool_failure(e, "CRM")

    async def ghl_create_contact(
        self,
        first_name: str,
        phone: str,
        last_name: str | None = None,
        email: str | None = None,
        company_name: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new contact.

        Args:
            first_name: First name
            phone: Phone number
            last_name: Last name
            email: Email address
            company_name: Company name
            tags: Tags to add

        Returns:
            Created contact info or error
        """
        try:
            client = await self._get_client()

            payload: dict[str, Any] = {
                "locationId": self.location_id,
                "firstName": first_name,
                "phone": phone,
            }

            if last_name:
                payload["lastName"] = last_name
            if email:
                payload["email"] = email
            if company_name:
                payload["companyName"] = company_name
            if tags:
                payload["tags"] = tags

            response = await client.post("/contacts/", json=payload)

            if response.status_code not in (HTTPStatus.OK, HTTPStatus.CREATED):
                self.logger.warning(
                    "ghl_create_contact_failed",
                    status_code=response.status_code,
                    response=describe_text(response.text),
                )
                return upstream_failure("CRM", response.status_code)

            data = response.json()
            contact = data.get("contact", {})

            return {
                "success": True,
                "contact_id": contact.get("id"),
                "message": f"Created contact for {first_name} {last_name or ''}".strip(),
            }

        except Exception as e:
            log_exception(self.logger, "ghl_create_contact_error", e)
            return tool_failure(e, "CRM")

    async def ghl_update_contact(
        self,
        contact_id: str,
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        company_name: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing contact.

        Args:
            contact_id: Contact ID
            first_name: First name
            last_name: Last name
            phone: Phone number
            email: Email address
            company_name: Company name

        Returns:
            Update result
        """
        safe_contact_id = encode_path_id(contact_id)
        if safe_contact_id is None:
            return invalid_id_result("contact_id")
        try:
            client = await self._get_client()

            payload: dict[str, Any] = {}
            if first_name:
                payload["firstName"] = first_name
            if last_name:
                payload["lastName"] = last_name
            if phone:
                payload["phone"] = phone
            if email:
                payload["email"] = email
            if company_name:
                payload["companyName"] = company_name

            if not payload:
                return {"success": False, "error": "No fields to update"}

            response = await client.put(f"/contacts/{safe_contact_id}", json=payload)

            if response.status_code != HTTPStatus.OK:
                return upstream_failure("CRM", response.status_code)

            return {
                "success": True,
                "contact_id": contact_id,
                "message": "Contact updated successfully",
            }

        except Exception as e:
            log_exception(self.logger, "ghl_update_contact_error", e, contact_id=contact_id)
            return tool_failure(e, "CRM")

    async def ghl_add_contact_tags(self, contact_id: str, tags: list[str]) -> dict[str, Any]:
        """Add tags to a contact.

        Args:
            contact_id: Contact ID
            tags: Tags to add

        Returns:
            Result
        """
        safe_contact_id = encode_path_id(contact_id)
        if safe_contact_id is None:
            return invalid_id_result("contact_id")
        try:
            client = await self._get_client()

            response = await client.post(
                f"/contacts/{safe_contact_id}/tags",
                json={"tags": tags},
            )

            if response.status_code != HTTPStatus.OK:
                return upstream_failure("CRM", response.status_code)

            return {
                "success": True,
                "contact_id": contact_id,
                "tags_added": tags,
                "message": f"Added {len(tags)} tag(s) to contact",
            }

        except Exception as e:
            log_exception(self.logger, "ghl_add_contact_tags_error", e, contact_id=contact_id)
            return tool_failure(e, "CRM")

    async def ghl_get_calendars(self) -> dict[str, Any]:
        """Get list of available calendars.

        Returns:
            List of calendars
        """
        try:
            client = await self._get_client()
            response = await client.get(
                "/calendars/",
                params={"locationId": self.location_id},
            )

            if response.status_code != HTTPStatus.OK:
                return {
                    "success": False,
                    "error": f"Failed to get calendars: {response.status_code}",
                }

            data = response.json()
            calendars = data.get("calendars", [])

            return {
                "success": True,
                "calendars": [
                    {
                        "id": cal.get("id"),
                        "name": cal.get("name"),
                        "description": cal.get("description"),
                    }
                    for cal in calendars
                ],
            }

        except Exception as e:
            log_exception(self.logger, "ghl_get_calendars_error", e)
            return tool_failure(e, "CRM")

    async def ghl_get_calendar_slots(
        self,
        calendar_id: str,
        start_date: str,
        end_date: str | None = None,
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        """Get available appointment slots.

        Args:
            calendar_id: Calendar ID
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD), defaults to start_date
            timezone: Timezone

        Returns:
            Available slots
        """
        safe_calendar_id = encode_path_id(calendar_id)
        if safe_calendar_id is None:
            return invalid_id_result("calendar_id")
        try:
            client = await self._get_client()

            if not end_date:
                end_date = start_date

            # Convert dates to Unix timestamps (milliseconds)
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms = int(end_dt.timestamp() * 1000)

            response = await client.get(
                f"/calendars/{safe_calendar_id}/free-slots",
                params={
                    "startDate": start_ms,
                    "endDate": end_ms,
                    "timezone": timezone,
                },
            )

            if response.status_code != HTTPStatus.OK:
                return {"success": False, "error": f"Failed to get slots: {response.status_code}"}

            data = response.json()

            # Format slots for easier reading
            slots = []
            for date_key, slot_list in data.items():
                if isinstance(slot_list, list):
                    for slot in slot_list:
                        slots.append(
                            {
                                "date": date_key,
                                "start": slot.get("startTime"),
                                "end": slot.get("endTime"),
                            }
                        )

            return {
                "success": True,
                "date": start_date,
                "total_available": len(slots),
                "slots": slots[:10],  # Limit to 10 slots
            }

        except Exception as e:
            log_exception(self.logger, "ghl_get_calendar_slots_error", e, calendar_id=calendar_id)
            return tool_failure(e, "CRM")

    async def ghl_book_appointment(
        self,
        calendar_id: str,
        contact_id: str,
        start_time: str,
        end_time: str,
        title: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Book an appointment.

        Args:
            calendar_id: Calendar ID
            contact_id: Contact ID
            start_time: Start time (ISO 8601)
            end_time: End time (ISO 8601)
            title: Appointment title
            notes: Additional notes

        Returns:
            Booking confirmation
        """
        try:
            client = await self._get_client()

            payload: dict[str, Any] = {
                "calendarId": calendar_id,
                "contactId": contact_id,
                "startTime": start_time,
                "endTime": end_time,
                "locationId": self.location_id,
            }

            if title:
                payload["title"] = title
            if notes:
                payload["notes"] = notes

            response = await client.post("/calendars/events/appointments", json=payload)

            if response.status_code not in (HTTPStatus.OK, HTTPStatus.CREATED):
                self.logger.warning(
                    "ghl_book_appointment_failed",
                    status_code=response.status_code,
                    response=describe_text(response.text),
                )
                return upstream_failure("CRM", response.status_code)

            data = response.json()
            event = data.get("event", data)

            return {
                "success": True,
                "event_id": event.get("id"),
                "start_time": start_time,
                "end_time": end_time,
                "message": f"Appointment booked for {start_time}",
            }

        except Exception as e:
            log_exception(self.logger, "ghl_book_appointment_error", e)
            return tool_failure(e, "CRM")

    async def ghl_get_appointments(self, contact_id: str) -> dict[str, Any]:
        """Get appointments for a contact.

        Args:
            contact_id: Contact ID

        Returns:
            List of appointments
        """
        safe_contact_id = encode_path_id(contact_id)
        if safe_contact_id is None:
            return invalid_id_result("contact_id")
        try:
            client = await self._get_client()
            response = await client.get(f"/contacts/{safe_contact_id}/appointments")

            if response.status_code != HTTPStatus.OK:
                return {
                    "success": False,
                    "error": f"Failed to get appointments: {response.status_code}",
                }

            data = response.json()
            events = data.get("events", [])

            return {
                "success": True,
                "total": len(events),
                "appointments": [
                    {
                        "id": evt.get("id"),
                        "title": evt.get("title"),
                        "start_time": evt.get("startTime"),
                        "end_time": evt.get("endTime"),
                        "status": evt.get("status"),
                    }
                    for evt in events
                ],
            }

        except Exception as e:
            log_exception(self.logger, "ghl_get_appointments_error", e, contact_id=contact_id)
            return tool_failure(e, "CRM")

    async def ghl_cancel_appointment(self, event_id: str) -> dict[str, Any]:
        """Cancel an appointment.

        Args:
            event_id: Event/appointment ID

        Returns:
            Cancellation result
        """
        safe_event_id = encode_path_id(event_id)
        if safe_event_id is None:
            return invalid_id_result("event_id")
        try:
            client = await self._get_client()
            response = await client.delete(f"/calendars/events/{safe_event_id}")

            if response.status_code not in (HTTPStatus.OK, HTTPStatus.NO_CONTENT):
                return {"success": False, "error": f"Failed to cancel: {response.status_code}"}

            return {
                "success": True,
                "event_id": event_id,
                "message": "Appointment cancelled successfully",
            }

        except Exception as e:
            log_exception(self.logger, "ghl_cancel_appointment_error", e, event_id=event_id)
            return tool_failure(e, "CRM")

    async def ghl_get_pipelines(self) -> dict[str, Any]:
        """Get list of sales pipelines.

        Returns:
            List of pipelines with stages
        """
        try:
            client = await self._get_client()
            response = await client.get(
                "/opportunities/pipelines",
                params={"locationId": self.location_id},
            )

            if response.status_code != HTTPStatus.OK:
                return {
                    "success": False,
                    "error": f"Failed to get pipelines: {response.status_code}",
                }

            data = response.json()
            pipelines = data.get("pipelines", [])

            return {
                "success": True,
                "pipelines": [
                    {
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "stages": [
                            {"id": s.get("id"), "name": s.get("name")} for s in p.get("stages", [])
                        ],
                    }
                    for p in pipelines
                ],
            }

        except Exception as e:
            log_exception(self.logger, "ghl_get_pipelines_error", e)
            return tool_failure(e, "CRM")

    async def ghl_create_opportunity(
        self,
        contact_id: str,
        pipeline_id: str,
        stage_id: str,
        name: str,
        monetary_value: float | None = None,
    ) -> dict[str, Any]:
        """Create a new opportunity/deal.

        Args:
            contact_id: Contact ID
            pipeline_id: Pipeline ID
            stage_id: Stage ID
            name: Opportunity name
            monetary_value: Deal value

        Returns:
            Created opportunity info
        """
        try:
            client = await self._get_client()

            payload: dict[str, Any] = {
                "locationId": self.location_id,
                "contactId": contact_id,
                "pipelineId": pipeline_id,
                "pipelineStageId": stage_id,
                "name": name,
            }

            if monetary_value is not None:
                payload["monetaryValue"] = monetary_value

            response = await client.post("/opportunities/", json=payload)

            if response.status_code not in (HTTPStatus.OK, HTTPStatus.CREATED):
                return upstream_failure("CRM", response.status_code)

            data = response.json()
            opp = data.get("opportunity", data)

            return {
                "success": True,
                "opportunity_id": opp.get("id"),
                "name": name,
                "value": monetary_value,
                "message": f"Created opportunity '{name}'",
            }

        except Exception as e:
            log_exception(self.logger, "ghl_create_opportunity_error", e)
            return tool_failure(e, "CRM")

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a GoHighLevel tool by name.

        Args:
            tool_name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result
        """
        tool_map: dict[str, ToolHandler] = {
            "ghl_search_contact": self.ghl_search_contact,
            "ghl_get_contact": self.ghl_get_contact,
            "ghl_create_contact": self.ghl_create_contact,
            "ghl_update_contact": self.ghl_update_contact,
            "ghl_add_contact_tags": self.ghl_add_contact_tags,
            "ghl_get_calendars": self.ghl_get_calendars,
            "ghl_get_calendar_slots": self.ghl_get_calendar_slots,
            "ghl_book_appointment": self.ghl_book_appointment,
            "ghl_get_appointments": self.ghl_get_appointments,
            "ghl_cancel_appointment": self.ghl_cancel_appointment,
            "ghl_get_pipelines": self.ghl_get_pipelines,
            "ghl_create_opportunity": self.ghl_create_opportunity,
        }

        handler = tool_map.get(tool_name)
        if not handler:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}

        result: dict[str, Any] = await handler(**arguments)
        return result

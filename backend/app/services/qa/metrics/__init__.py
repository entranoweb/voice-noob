"""Voice agent evaluation metrics.

Importing this package registers every metric. Metric modules are imported for
their registration side effect, so a consumer only needs the registry.

Phase 1 is deterministic only: nothing here calls a model. That is deliberate —
a deterministic metric needs no calibration set and carries no judge variance,
so it can be trusted the day it ships. Judged metrics arrive once there is a
human-labelled set to measure their agreement against.
"""

from app.services.qa.metrics import registry

# Imported for their registration side effect. Kept at the bottom so the public
# names above are importable even if a metric module is being edited.
from app.services.qa.metrics.accuracy import task_completion as _task_completion
from app.services.qa.metrics.base import (
    BaseMetric,
    MetricCategory,
    MetricContext,
    MetricKind,
    MetricScore,
    ToolCallData,
    TurnData,
)
from app.services.qa.metrics.diagnostic import response_speed as _response_speed
from app.services.qa.metrics.diagnostic import (
    tool_call_validity as _tool_call_validity,
)
from app.services.qa.metrics.experience import (
    transcription_accuracy as _transcription_accuracy,
)
from app.services.qa.metrics.experience import (
    turn_taking as _turn_taking,
)
from app.services.qa.metrics.runner import (
    MetricResults,
    MetricRunner,
    RunOutcome,
    evaluate,
)
from app.services.qa.metrics.validation import (
    conversation_valid_end as _conversation_valid_end,
)
from app.services.qa.metrics.validation import (
    state_restored as _state_restored,
)

__all__ = [
    "BaseMetric",
    "MetricCategory",
    "MetricContext",
    "MetricKind",
    "MetricResults",
    "MetricRunner",
    "MetricScore",
    "RunOutcome",
    "ToolCallData",
    "TurnData",
    "evaluate",
    "registry",
]

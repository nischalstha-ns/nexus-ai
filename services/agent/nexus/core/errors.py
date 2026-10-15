"""Error hierarchy for the NEXUS agent service."""


class NexusError(Exception):
    """Base class; every error carries a machine-readable code."""


class ConfigError(NexusError):
    pass


class BodyTooLarge(NexusError):
    """Request body exceeded the endpoint's size limit (HTTP 413)."""


class ProviderError(NexusError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class ProviderAuthError(ProviderError):
    pass


class MemoryError(NexusError):
    pass


class ToolError(NexusError):
    pass


class PermissionDenied(ToolError):
    pass


class ValidationError(ToolError):
    pass


class PlannerError(NexusError):
    pass


class Cancelled(PlannerError):
    pass


class BudgetExceeded(PlannerError):
    pass

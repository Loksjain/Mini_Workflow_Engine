"""Custom exceptions for the workflow engine."""


class ToolNotFoundError(KeyError):
    """Raised when a referenced tool is not registered."""

    pass

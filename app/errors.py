"""Stable error type carried from validation/parsing through to the HTTP layer."""


class ApiError(Exception):
    """An error with a stable machine-readable type and an HTTP status.

    ``err_type`` strings are part of the API contract and must stay stable;
    clients may switch on them. ``message`` is human-readable detail and may
    vary.
    """

    def __init__(self, err_type: str, message: str, status: int = 400):
        super().__init__(message)
        self.err_type = err_type
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": {"type": self.err_type, "message": self.message}}

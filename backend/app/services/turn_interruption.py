"""Execution loss is distinct from an explicit STOP or a business failure."""


class TurnInterrupted(BaseException):
    """Unfinished durable work will continue through the shared recovery executor."""

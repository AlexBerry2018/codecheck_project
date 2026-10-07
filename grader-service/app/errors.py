class CoursesUnavailable(Exception):
    """courses-service did not answer or answered with a server error. Retry the message."""


class AssignmentNotFound(Exception):
    """The assignment does not exist (any more). Retrying will not help."""

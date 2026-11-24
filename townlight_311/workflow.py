from .actions import ActionMixin
from .attachments import AttachmentMixin
from .commands import CommandMixin
from .discussion import DiscussionMixin
from .exports import ExportMixin
from .intake import IntakeMixin
from .recovery import RecoveryMixin
from .store import Conflict, InvalidInput, NotFound, RepositoryBase, Unauthorized


class ServiceRequestRepository(
    CommandMixin,
    IntakeMixin,
    ActionMixin,
    DiscussionMixin,
    AttachmentMixin,
    ExportMixin,
    RecoveryMixin,
    RepositoryBase,
):
    """Repository for service requests.

    Internal library staff access requires an authenticated Suite adapter.
    """


__all__ = [
    "Conflict",
    "InvalidInput",
    "NotFound",
    "ServiceRequestRepository",
    "Unauthorized",
]

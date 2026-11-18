from .core import CoreStore, jdump, jload
from .evidence import EvidenceMixin, SEV
from .incidents import IncidentMixin, OPEN_STATUSES, SEVERITIES, STAGES, STATUSES, CLASSIFICATIONS


class Store(CoreStore, EvidenceMixin, IncidentMixin):
    """Single entry point for all persistence."""


__all__ = ["Store", "jdump", "jload", "SEV", "OPEN_STATUSES", "SEVERITIES", "STAGES", "STATUSES", "CLASSIFICATIONS"]

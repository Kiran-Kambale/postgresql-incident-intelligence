from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .recorder import Recorder
from .store import Store
from .timeutil import iso


@dataclass
class AppContext:
    settings: Settings
    store: Store
    recorder: Recorder
    started_at: str = field(default_factory=iso)

    @property
    def demo(self) -> bool:
        return self.settings.demo_mode

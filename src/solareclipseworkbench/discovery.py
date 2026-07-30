"""Shared shape for "here is where a piece of hardware might be".

Mounts and relay boards are found in the same way — walk the USB serial ports,
ask each driver whether anything there looks like one of its devices — so they
report their findings through one type and the console can list them together.
"""

from dataclasses import dataclass, field


@dataclass
class Candidate:
    """A place a driver thinks one of its devices might be.

    Being listed is a hint, never a promise: a USB serial adapter is a reason to
    probe, not evidence that the device behind it is the one claimed.
    """

    #: What sort of device this is — "mount", "relay", and so on.
    kind: str
    #: The driver or backend that produced this candidate.
    driver: str
    #: Where it is: a device path, a host, a USB identifier.
    target: str
    description: str = ""
    #: Keyword arguments that would connect to it.
    config: dict = field(default_factory=dict)

    def __str__(self) -> str:
        suffix = f" ({self.description})" if self.description else ""
        return f"{self.driver}: {self.target}{suffix}"

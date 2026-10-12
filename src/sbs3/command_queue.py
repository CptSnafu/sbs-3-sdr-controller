"""Single-file command sequencing for SBS-3 user-interface actions."""

from dataclasses import dataclass
import queue
import threading
from typing import Callable, Optional


@dataclass(frozen=True)
class QueuedCommand:
    """One operation which receives its sequence only when it starts."""

    label: str
    operation: Callable[[int], bool]


class CommandQueue:
    """Run USB operations one at a time and advance only after confirmation."""

    def __init__(self, initial_sequence: int, completed: Callable[[str, int, bool], None]) -> None:
        self._sequence = initial_sequence & 0xFF
        self._completed = completed
        self._items: queue.Queue[Optional[QueuedCommand]] = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    @property
    def next_sequence(self) -> int:
        return self._sequence

    def submit(self, command: QueuedCommand) -> None:
        self._items.put(command)

    def _run(self) -> None:
        while True:
            command = self._items.get()
            if command is None:
                return
            sequence = self._sequence
            ok = command.operation(sequence)
            if ok:
                self._sequence = (sequence + 1) & 0xFF
            self._completed(command.label, sequence, ok)

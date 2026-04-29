from dataclasses import dataclass


@dataclass
class Budget:
    max_iterations: int
    max_tool_calls: int
    iterations_used: int = 0
    tool_calls_used: int = 0

    def tick_iteration(self) -> None:
        self.iterations_used += 1

    def tick_tool_call(self) -> None:
        self.tool_calls_used += 1

    def iterations_exhausted(self) -> bool:
        return self.iterations_used >= self.max_iterations

    def tool_calls_exhausted(self) -> bool:
        return self.tool_calls_used >= self.max_tool_calls

from agent_skeleton import AgentHandler, FileInput

class MyAgentHandler(AgentHandler):
    """Describe what your agent does here."""

    async def handle_structured(
        self,
        user_input: str,
        files: list[FileInput] = []
    ) -> dict:
        # Access uploaded files:
        #   with files[0].as_tempfile() as path:
        #       content = path.read_text()
        #
        # "answer" is required — it becomes the human-readable response.
        # All other keys are returned to the planner as structured data.

        return {
            "answer": "Your response here",
            # "key": value,
        }

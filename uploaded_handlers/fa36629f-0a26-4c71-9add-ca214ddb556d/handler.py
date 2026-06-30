from agent_skeleton import AgentHandler, FileInput

class PaperReviewerHandler(AgentHandler):
    async def handle_structured(self, user_input: str, files: list = []) -> dict:
        return {"answer": "reviewed", "verdict": "accept"}

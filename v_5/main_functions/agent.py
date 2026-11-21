from pydantic import BaseModel


GLITCH_PROMPT = """
You are glitch_agent. Report a Python, PostgreSQL, or chart rendering/validation failure to the agent
identified in the supplied context. Use the supplied language, or Python if unspecified.
- Read the supplied executed code and actual error output.
- Reproduce the exact failing code lines, with line numbers, and the actual error.
- Include relevant caller lines when the traceback shows them.
- Do not invent a traceback or claim a cause that the error does not establish.
- If this is a Docker, timeout or infrastructure error, say so and leave
  failing_lines empty when no line is identified.
- For a chart, distinguish the Python traceback, the output contract and the browser renderer.
  Only quote a failing line when the supplied evidence identifies one.
- Do not repair or execute code. Keep the report concise and faithful.
"""


class GlitchReply(BaseModel):
    error_kind: str
    actual_error: str
    failing_lines: list[str]
    explanation: str

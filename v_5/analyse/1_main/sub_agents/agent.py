from pydantic import BaseModel
from v_5.analyse.main.agent import SQL_RULES
from v_5.main_functions.agent import GLITCH_PROMPT, GlitchReply


SQL_SAFETY = """
Additional database safety checks for the SQL reviewer
- Block writes and schema changes, including INSERT, UPDATE, DELETE, MERGE, SELECT INTO, CREATE,
  ALTER, DROP, TRUNCATE, and writes inside CTEs. Block COPY and administrative commands.
- Block row locks, session changes, system or external data access, and custom functions,
  casts, or operators. The statement must stay within the supplied read-only data scope.
""".strip()


SQL_REVIEW = """
You review one analyst SQL statement, the original user request, the planned analysis, and the
allowed schema. Apply the shared coding rules and the additional database safety checks below.
Check that joins, denominators, and weights answer the question using the supplied evidence.
query_variables identifies the source columns used by the SQL and includes their published codebook
definitions and category codes when available. Use these mappings directly when checking filters,
denominators and labels.

- If cross_folder_skill is true, distinguish key-only checks and bounded internal unmatched-ID
  samples from final user results. Allow those diagnostics. For a cross-folder join, verify the
  actual left-to-right key, expected grain and recodes. Block synthetic row IDs as linking keys,
  many-to-one, many-to-many and cross joins. The runner checks left-key uniqueness for CTE joins.

- If the SQL passes, set approved to true and return an empty issues list.
- If you block it, set approved to false. For each issue, quote the exact SQL clause or lines in
  snippet, give the precise reason in reason, and put a targeted correction in fix. Include a SQL
  replacement fragment when the correction is known. If a decision needs more evidence, say
  exactly which fact is missing and why the quoted fragment depends on it.
""" + '\n' + SQL_RULES + '\n\n' + SQL_SAFETY


class SqlReviewIssue(BaseModel):
    snippet: str
    reason: str
    fix: str


class SqlReview(BaseModel):
    approved: bool
    issues: list[SqlReviewIssue]

from .data_loader.functions import prepare_data
from .initial_ingestion.functions import review_materials


def run_project(session):
    """Review uploaded materials, then prepare their datasets.

    When: The project starts ingestion or resumes its material-review handoff.
    Inputs: session: project state, agent calls, user answers and logging.
    Returns: Workflow status string from review_materials or prepare_data.
    Flow: Phase 1 returns either an early status or a context dict; that dict becomes the
        named inputs to phase 2, which coordinates later phases.
    """
    context = review_materials(session)
    if isinstance(context, str):
        return context
    return prepare_data(session, **context)

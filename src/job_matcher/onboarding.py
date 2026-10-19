"""A local interview plan for the calling agent; no paid inference during setup."""

from job_matcher.models import STRICTNESS

STRICTNESS_QUESTION = (
    "How selective should recommendations be? Someone employed and only open to "
    "standout roles might choose selective or strict; someone actively searching might "
    "choose balanced or generous. Confirmed dealbreakers are skipped at every setting."
)


def strictness_options():
    return [{"strictness": name, "description": v["description"]} for name, v in STRICTNESS.items()]


QUESTIONS = {
    "engineering_tenure": (
        "How many years have you worked in software engineering? Clarify any "
        "dates or overlapping roles in the resume."
    ),
    "management_tenure": (
        "How many years have you managed engineers? Distinguish formal people "
        "management from technical leadership."
    ),
    "leadership_scope": (
        "What teams, engineers and managers have you led? Describe hiring, "
        "coaching, performance management and organizational responsibilities."
    ),
    "hands_on": (
        "How recently have you written code, reviewed PRs or designed systems? "
        "What hands-on work would you like in your next role?"
    ),
    "technical_scope": (
        "What frontend, backend, mobile, cloud, data or infrastructure work have "
        "you personally done or led? Distinguish personal depth from team "
        "exposure."
    ),
    "developer_tooling": (
        "What experience do you have with build systems, CI/CD, testing, "
        "developer environments or improving engineering productivity? Give "
        "concrete examples."
    ),
    "ai_development": (
        "How have you used AI-assisted development personally or introduced it to"
        " a team? Describe tools, workflow, review practices and examples."
    ),
    "ai_products": (
        "Have you built or led production AI/ML features? Describe your "
        "responsibility and what shipped; distinguish this from using AI coding "
        "tools."
    ),
    "domains": (
        "What relevant domain experience or achievements are missing from your "
        "resume? Include context and your actual contribution."
    ),
}


def interview(profile):
    answers = {a.topic: a for a in profile.experience_answers}
    return {
        "complete": len(answers) == len(QUESTIONS),
        "answered_topics": sorted(answers),
        "pending_questions": [
            {"topic": topic, "question": question}
            for topic, question in QUESTIONS.items()
            if topic not in answers
        ],
        "revisit_topics": [a.topic for a in answers.values() if a.status == "skipped"],
        "strictness": {
            "current": profile.policy.strictness,
            "question": STRICTNESS_QUESTION,
            "options": strictness_options(),
            "save_with": "set_strictness",
        },
        "instructions": [
            (
                "Read the resume first. Summarize existing evidence and ask the user to "
                "correct or add to it; do not silently turn resume inferences into "
                "interview answers."
            ),
            (
                "Ask one or two focused questions at a time, prioritizing gaps relevant "
                "to the target roles. The user can explicitly skip any topic."
            ),
            (
                "Save a concise factual summary of what the user did (scope, numbers, "
                "responsibilities, recency), shown to and confirmed by the user. Do not "
                "save conversational hedging or self-deprecation verbatim: the evaluator "
                "reads hedges as weaker evidence. Record genuine limits plainly as facts."
            ),
            (
                "Use save_experience_answers to merge confirmed answers. Do not invent "
                "technologies, tenure, outcomes or missing examples."
            ),
            (
                "Interview completion means every topic was addressed or explicitly "
                "skipped, not that the candidate has every skill or that evidence is "
                "independently verified."
            ),
        ],
    }

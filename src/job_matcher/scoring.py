import math

from job_matcher.questions import questions

WEIGHTS = {"leadership": 0.4, "technical": 0.35, "seniority": 0.25}


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("TypeSafe returned a nonnumeric value")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("TypeSafe returned a value outside the expected range")
    return value


def validate_answers(response):
    answers = response.get("answers", {})
    for key, question in questions().items():
        answer = answers.get(key, {})
        if answer.get("type") != question["type"]:
            raise ValueError(f"Missing or invalid TypeSafe answer: {key}")
        number(answer.get("confidence"), 0, 1)
        expected = (
            set(question["criteria"])
            if question["type"] == "choice"
            else {str(i) for i in range(len(question["criteria"]))}
        )
        probabilities = answer.get("probabilities", {})
        if set(probabilities) != expected:
            raise ValueError(f"Invalid TypeSafe probability keys: {key}")
        total = sum(number(p, 0, 1) for p in probabilities.values())
        if abs(total - 1) > 0.02:
            raise ValueError(f"Invalid TypeSafe probability distribution: {key}")
        if question["type"] == "choice":
            if answer.get("choice") not in expected:
                raise ValueError(f"Invalid TypeSafe choice: {key}")
            if probabilities[answer["choice"]] < max(probabilities.values()) - 0.001:
                raise ValueError(f"Inconsistent TypeSafe choice: {key}")
        else:
            number(answer.get("score"), 0, 4)
    return answers


SCORING_VERSION = "3"
CONFLICTS = {
    "role": "mismatch",
    "remote": "incompatible",
    "location": "incompatible",
    "salary": "below_floor",
    "authorization": "incompatible",
    "dealbreakers": "conflict",
}
# For a remote-only candidate, silence about remote work usually means it isn't.
UNKNOWN_IS_CONCERN = {"remote", "dealbreakers"}
# Below the qualification bar, uncertainty is judged against a fixed bar so a stricter
# stop can never turn a skip into a second look.
BELOW_BAR_SETTLED = 0.65


def settled(probabilities):
    """Probability mass on the likeliest pair of adjacent levels.

    A rubric score split between "strong" and "very strong" is a settled judgment;
    Jev's per-answer confidence reflects only the single top level.
    """
    levels = [probabilities[str(i)] for i in range(len(probabilities))]
    return max(a + b for a, b in zip(levels, levels[1:]))


def summarize(profile, response):
    """Turn Jev answers into a recommendation under the profile's strictness.

    Confident hard conflicts always skip. Doubts (material conflict risk, an unsettled
    qualification rating, and at stricter stops details the posting omits) go wherever
    the strictness stop sends them: apply with notes, investigate, or skip. Details the
    posting omits are otherwise returned as checks to resolve while applying.
    """
    answers = validate_answers(response)
    policy = profile.policy.resolved()
    qualification = sum(answers[k]["score"] / 4 * w * 100 for k, w in WEIGHTS.items())
    # Salary satisfaction is a preference signal, not an inferred dollar amount.
    salary_points = {
        "below_floor": 0,
        "overlaps_floor": 50,
        "meets_floor": 75,
        "meets_target": 100,
        "unknown": 50,
    }
    no_salary_requirement = not profile.salary_floor and not profile.salary_target
    if no_salary_requirement:
        # No floor or target was given, so salary cannot raise or lower the fit.
        salary_points = dict.fromkeys(salary_points, 50)
    fit = 0.8 * qualification + 0.15 * salary_points[answers["salary"]["choice"]]
    fit += 0.05 * answers["preferences"]["score"] / 4 * 100
    blockers, concerns, checks, signals = [], [], [], []
    for key, answer in answers.items():
        confidence = answer["confidence"]
        signals.append(
            {
                "dimension": key,
                "value": answer.get("choice", answer.get("score")),
                "confidence": confidence,
            }
        )
        if key == "salary" and no_salary_requirement:
            continue  # no floor or target: salary evidence is neutral, never a conflict
        if key in CONFLICTS:
            risk = answer["probabilities"][CONFLICTS[key]]
            if answer["choice"] == CONFLICTS[key] and confidence >= policy["conflict_confidence"]:
                blockers.append(key)
            elif answer["choice"] == CONFLICTS[key] or risk >= policy["conflict_risk_threshold"]:
                concerns.append(f"{key}: possible conflict ({risk:.0%} likely)")
            elif answer["choice"] == "unknown":
                (concerns if key in UNKNOWN_IS_CONCERN else checks).append(
                    f"{key}: not stated in the posting"
                )
            if (
                key == "salary"
                and answer["choice"] == "overlaps_floor"
                and not no_salary_requirement
            ):
                checks.append("salary: range spans your floor; confirm the offer can meet it")
    unsettled = [
        k for k in WEIGHTS if settled(answers[k]["probabilities"]) < policy["settled_threshold"]
    ]
    concerns += [f"{key}: rating uncertain" for key in unsettled]
    doubts = concerns + (checks if policy["unstated_is_doubt"] else [])
    if blockers:
        recommendation = "skip"
    elif qualification < policy["stretch_threshold"]:
        # Below the bar, only an unsettled rating earns a second look, never an apply.
        uncertain = any(settled(answers[k]["probabilities"]) < BELOW_BAR_SETTLED for k in WEIGHTS)
        second_look = uncertain and policy["doubt"] != "skip"
        recommendation = "investigate" if second_look else "skip"
    elif doubts:
        recommendation = policy["doubt"]
    else:
        recommendation = "apply"
    return {
        "recommendation": recommendation,
        "fit_percent": round(fit, 1),
        "qualification_strength_percent": round(qualification, 1),
        "stretch": policy["stretch_threshold"] <= qualification < policy["apply_threshold"],
        "strictness": profile.policy.strictness,
        "confidence": min(a["confidence"] for a in answers.values()),
        "dealbreakers": blockers,
        "investigate": concerns,
        "checks": checks,
        "signals": signals,
        "scoring_version": SCORING_VERSION,
        "score_note": "Percentages are rubric-based ranking scores, not hiring probabilities.",
    }

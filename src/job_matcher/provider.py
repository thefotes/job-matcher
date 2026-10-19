import os

from typesafe_sdk import TypeSafeClient, TypeSafeError

from job_matcher.questions import questions


class ProviderError(RuntimeError):
    pass


class JevProvider:
    def __init__(self):
        self.model = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")

    def evaluate(self, profile, job):
        if not os.environ.get("TYPESAFE_API_KEY", "").strip():
            raise ProviderError("Set TYPESAFE_API_KEY in the environment or the project .env file")
        try:
            with TypeSafeClient(model=self.model, timeout=45) as client:
                response = client.system_one(
                    state={"candidate": profile.evidence(), "job": job.model_dump()},
                    questions=questions(profile),
                )
                return response.model_dump(mode="json")
        except TypeSafeError as exc:
            # SDK exception bodies can contain candidate data; expose only a safe error type.
            raise ProviderError(
                f"TypeSafe request failed ({type(exc).__name__}); retry later "
                "or check your API key and account"
            ) from None

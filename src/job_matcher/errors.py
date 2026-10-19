from pydantic import ValidationError


def error_message(exc):
    """Do not echo raw resume/job input in validation diagnostics."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
    return str(exc)

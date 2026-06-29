"""Structured applicant profile — the single source of truth for the facts the
apply agent uses to self-assess eligibility against a listing's requirements.

The free-text reference message (`message_template.py`) is copy to paraphrase;
THIS is data to compare. Keep the two consistent when either changes.

Fill in your own details below, or override any field at runtime with the
matching ``APPLICANT_*`` environment variable (e.g. ``APPLICANT_NAME``,
``APPLICANT_GROSS_MONTHLY_INCOME``). The defaults are placeholders.
"""
import os
from dataclasses import dataclass


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ApplicantProfile:
    name: str = _env_str("APPLICANT_NAME", "Jane Doe")
    age: int = _env_int("APPLICANT_AGE", 30)
    nationality: str = _env_str("APPLICANT_NATIONALITY", "Dutch")
    # Gross MONTHLY income in euros (bruto maandinkomen). Decisive criterion.
    gross_monthly_income: float = _env_float("APPLICANT_GROSS_MONTHLY_INCOME", 4000.00)
    employment: str = _env_str(
        "APPLICANT_EMPLOYMENT", "permanent contract (vast contract)"
    )
    household_size: int = _env_int("APPLICANT_HOUSEHOLD_SIZE", 1)  # solo by default
    is_student: bool = _env_bool("APPLICANT_IS_STUDENT", False)
    is_woningdeler: bool = _env_bool("APPLICANT_IS_WONINGDELER", False)
    has_pets: bool = _env_bool("APPLICANT_HAS_PETS", False)
    smoker: bool = _env_bool("APPLICANT_SMOKER", False)

    @property
    def gross_yearly_income(self) -> float:
        return self.gross_monthly_income * 12

    def to_prompt_block(self) -> str:
        return (
            f"- Name: {self.name}\n"
            f"- Age: {self.age}\n"
            f"- Nationality: {self.nationality}\n"
            f"- Gross monthly income (bruto maandinkomen): "
            f"EUR {self.gross_monthly_income:,.2f} "
            f"(approx EUR {self.gross_yearly_income:,.0f} per year)\n"
            f"- Employment: {self.employment}\n"
            f"- Household size: {self.household_size} (applying solo)\n"
            f"- Student: {'yes' if self.is_student else 'no'}\n"
            f"- Room-sharer / woningdeler: "
            f"{'yes' if self.is_woningdeler else 'no'}\n"
            f"- Pets: {'yes' if self.has_pets else 'no'}\n"
            f"- Smoker: {'yes' if self.smoker else 'no'}"
        )


PROFILE = ApplicantProfile()

# Income tolerance: if a listing states a minimum gross monthly income, still
# apply when the applicant is within this fraction below it (landlords are
# sometimes flexible / count assets). Below the tolerance band -> not_eligible.
INCOME_TOLERANCE = 0.05

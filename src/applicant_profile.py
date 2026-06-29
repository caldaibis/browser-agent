"""Structured applicant profile — the single source of truth for the facts the
apply agent uses to self-assess eligibility against a listing's requirements.

The free-text reference message (`message_template.py`) is copy to paraphrase;
THIS is data to compare. Keep the two consistent when either changes.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ApplicantProfile:
    name: str = "Jane Doe"
    age: int = 29
    nationality: str = "Dutch"
    # Gross MONTHLY income in euros (bruto maandinkomen). Decisive criterion.
    gross_monthly_income: float = 4000.00
    employment: str = "permanent contract (vast contract), my role"
    household_size: int = 1  # applying solo, no partner/housemates
    is_student: bool = False
    is_woningdeler: bool = False  # not seeking shared/room-share housing
    has_pets: bool = False
    smoker: bool = False

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

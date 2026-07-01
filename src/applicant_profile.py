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


def _parse_number(value: str | float | int) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("EUR", "").replace("€", "").replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    elif "." in text:
        parts = text.split(".")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            text = "".join(parts)
    return float(text)


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float) -> float:
    return _parse_number(os.environ.get(key, default))


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _ja_nee(value: bool) -> str:
    return "Ja" if value else "Nee"


@dataclass(frozen=True)
class ApplicantProfile:
    name: str = _env_str("APPLICANT_NAME", "Collin Aldaibis")
    first_name: str = _env_str("APPLICANT_FIRST_NAME", "Collin")
    preferred_name: str = _env_str("APPLICANT_PREFERRED_NAME", "Collin")
    initials: str = _env_str("APPLICANT_INITIALS", "C.D.")
    last_name_prefix: str = _env_str("APPLICANT_LAST_NAME_PREFIX", "")
    last_name: str = _env_str("APPLICANT_LAST_NAME", "Aldaibis")
    gender: str = _env_str("APPLICANT_GENDER", "Man")
    birth_date: str = _env_str("APPLICANT_BIRTH_DATE", "27-02-1997")
    age: int = _env_int("APPLICANT_AGE", 29)
    nationality: str = _env_str("APPLICANT_NATIONALITY", "Dutch")
    residence_country: str = _env_str("APPLICANT_RESIDENCE_COUNTRY", "Nederland")
    postcode: str = _env_str("APPLICANT_POSTCODE", "3532VB")
    house_number: str = _env_str("APPLICANT_HOUSE_NUMBER", "25")
    house_number_addition: str = _env_str("APPLICANT_HOUSE_NUMBER_ADDITION", "BIS")
    street: str = _env_str("APPLICANT_STREET", "Bilderdijkstraat")
    city: str = _env_str("APPLICANT_CITY", "Utrecht")
    phone: str = _env_str("APPLICANT_PHONE", "0614916251")
    email: str = _env_str("APPLICANT_EMAIL", "caldaibis@gmail.com")
    desired_rent_start: str = _env_str("APPLICANT_DESIRED_RENT_START", "Per direct")
    cohabitants: str = _env_str("APPLICANT_COHABITANTS", "Alleen huren")
    children_count: int = _env_int("APPLICANT_CHILDREN_COUNT", 0)
    current_housing: str = _env_str("APPLICANT_CURRENT_HOUSING", "Ik huur momenteel")
    work_situation: str = _env_str("APPLICANT_WORK_SITUATION", "Loondienst")
    contract_type: str = _env_str("APPLICANT_CONTRACT_TYPE", "Vast contract")
    priority_profession: bool = _env_bool("APPLICANT_PRIORITY_PROFESSION", False)
    has_savings: bool = _env_bool("APPLICANT_HAS_SAVINGS", True)
    savings_amount: float = _env_float("APPLICANT_SAVINGS_AMOUNT", 110000)
    has_financial_obligations: bool = _env_bool("APPLICANT_HAS_FINANCIAL_OBLIGATIONS", False)
    # Gross MONTHLY income in euros (bruto maandinkomen). Decisive criterion.
    gross_monthly_income: float = _env_float("APPLICANT_GROSS_MONTHLY_INCOME", 5482.00)
    employment: str = _env_str(
        "APPLICANT_EMPLOYMENT", "Loondienst, vast contract"
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
            f"- Gender / geslacht: {self.gender}\n"
            f"- First name / voornaam: {self.first_name}\n"
            f"- Roepnaam: {self.preferred_name}\n"
            f"- Voorletters: {self.initials}\n"
            f"- Tussenvoegsel: {self.last_name_prefix or '(none / empty)'}\n"
            f"- Achternaam: {self.last_name}\n"
            f"- Date of birth / geboortedatum: {self.birth_date}\n"
            f"- Age: {self.age}\n"
            f"- Nationality: {self.nationality}\n"
            f"- Current country of residence: {self.residence_country}\n"
            f"- Address: {self.street} {self.house_number} "
            f"{self.house_number_addition}, {self.postcode} {self.city}\n"
            f"- Postcode: {self.postcode}\n"
            f"- Huisnummer: {self.house_number}\n"
            f"- Toevoeging: {self.house_number_addition}\n"
            f"- Straat: {self.street}\n"
            f"- Woonplaats: {self.city}\n"
            f"- Phone / telefoonnummer: {self.phone}\n"
            f"- Email / e-mail: {self.email}\n"
            f"- Desired rental start / wanneer huren: {self.desired_rent_start}\n"
            f"- Meewonenden: {self.cohabitants}\n"
            f"- Household size: {self.household_size} (applying solo)\n"
            f"- Children / waarvan kinderen: {self.children_count}\n"
            f"- Current housing situation: {self.current_housing}\n"
            f"- Current work situation: {self.work_situation}\n"
            f"- Contract type: {self.contract_type}\n"
            f"- Priority profession in permanent employment: "
            f"{_ja_nee(self.priority_profession)}\n"
            f"- Gross monthly income (bruto maandinkomen): "
            f"EUR {self.gross_monthly_income:,.2f} "
            f"(approx EUR {self.gross_yearly_income:,.0f} per year)\n"
            f"- Employment: {self.employment}\n"
            f"- Savings / eigen vermogen: {_ja_nee(self.has_savings)}"
            f", EUR {self.savings_amount:,.0f}\n"
            f"- Credits or other financial obligations: "
            f"{_ja_nee(self.has_financial_obligations)}\n"
            f"- Student: {_ja_nee(self.is_student)}\n"
            f"- Room-sharer / woningdeler: "
            f"{_ja_nee(self.is_woningdeler)}\n"
            f"- Pets: {_ja_nee(self.has_pets)}\n"
            f"- Smoker: {_ja_nee(self.smoker)}"
        )


PROFILE = ApplicantProfile()

# Income tolerance: if a listing states a minimum gross monthly income, still
# apply when the applicant is within this fraction below it (landlords are
# sometimes flexible / count assets). Below the tolerance band -> not_eligible.
INCOME_TOLERANCE = 0.05

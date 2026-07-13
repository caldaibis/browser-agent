"""Reference message used by the apply agent for rental applications.

The reference message is generated from ``applicant_profile.PROFILE`` instead
of hard-coded identity text. This prevents a dangerous drift where the
structured profile contains the real applicant while the free-text template
still says "Jane Doe" / "you@example.com"; the model may paraphrase either.
Keep style changes here, but keep personal facts in ``applicant_profile.py`` or
the matching ``APPLICANT_*`` environment variables.
"""
from __future__ import annotations

from .applicant_profile import PROFILE


def _bool_phrase(value: bool, yes: str, no: str) -> str:
    return yes if value else no


def _dutch_identity() -> str:
    nationality = PROFILE.nationality.strip().lower()
    if nationality in {"dutch", "nederlands", "nederlandse"}:
        if PROFILE.gender.lower().startswith("man"):
            return "Nederlandse man"
        if PROFILE.gender.lower().startswith(("vrouw", "female")):
            return "Nederlandse vrouw"
        return "Nederlander"
    return PROFILE.nationality


def build_reference_application_message() -> str:
    pet_phrase_nl = _bool_phrase(PROFILE.has_pets, "Ik heb huisdieren.", "Ik heb geen huisdieren.")
    smoke_phrase_nl = _bool_phrase(PROFILE.smoker, "Ik rook.", "Ik rook niet.")
    pet_phrase_en = _bool_phrase(PROFILE.has_pets, "I have pets.", "I do not have pets.")
    smoke_phrase_en = _bool_phrase(PROFILE.smoker, "I smoke.", "I do not smoke.")
    savings_nl = (
        f" Daarnaast beschik ik over eigen vermogen van ongeveer "
        f"EUR {PROFILE.savings_amount:,.0f}."
        if PROFILE.has_savings else ""
    )
    savings_en = (
        f" I also have savings of approximately EUR {PROFILE.savings_amount:,.0f}."
        if PROFILE.has_savings else ""
    )

    return f"""Beste,

Graag kom ik in aanmerking voor de huurwoning aan de [[ADDRESS]]. De woning sprak mij direct aan en past goed bij mijn situatie vanwege de locatie, huurprijs en indeling. Ik zoek een nette, zelfstandige woning voor mijzelf, zonder partner of huisgenoten, waar ik voor langere tijd prettig kan wonen.

Ik ben een {PROFILE.age}-jarige {_dutch_identity()} en werk op dit moment in {PROFILE.employment_nl}. Mijn bruto maandinkomen is EUR {PROFILE.gross_monthly_income:,.2f} en mijn huurdossier is compleet.{savings_nl} {smoke_phrase_nl} {pet_phrase_nl}

Ik kan op korte termijn bezichtigen en bij een passende match snel beslissen. Mijn documenten, waaronder ID met BSN afgeschermd, werkgeversverklaring, recente loonstroken, bewijs van salarisstorting en verhuurdersverklaring, kan ik direct aanleveren.

Ik hoor graag of ik in aanmerking kan komen voor een bezichtiging. Hartelijk dank voor uw tijd en overweging.

Met vriendelijke groet,

{PROFILE.name}

{PROFILE.phone}
{PROFILE.email}

-------------

Dear,

I would like to apply for the rental property at [[ADDRESS]]. The property immediately caught my attention and seems to fit my situation well in terms of location, rent and layout. I am looking for a neat, independent home for myself, without a partner or housemates, where I can live comfortably for the longer term.

I am {PROFILE.age} years old and currently in {PROFILE.employment_en}. My gross monthly income is EUR {PROFILE.gross_monthly_income:,.2f}, and my rental file is complete.{savings_en} {smoke_phrase_en} {pet_phrase_en}

I am available for a viewing on short notice and can decide quickly if there is a good match. I can immediately provide my documents, including ID with BSN shielded, employer statement, recent payslips, proof of salary payment and a landlord reference.

I would appreciate it if you could let me know whether I may be considered for a viewing. Thank you for your time and consideration.

Best regards,

{PROFILE.name}

Phone: {PROFILE.phone}
{PROFILE.email}
"""


REFERENCE_APPLICATION_MESSAGE = build_reference_application_message()

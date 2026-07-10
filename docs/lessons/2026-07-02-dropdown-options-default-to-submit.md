# Custom dropdown options can default to type=submit

(from AGENTS.md, verbatim; see git history for context)

**Custom dropdown options can default to `type="submit"`.** REBO Groep's
"Soort inkomen" options are `<button>`s with no explicit `type="button"`
inside a `<form>` — clicking one to just *select* it can fire a real,
premature form submission before the rest of the dialog is filled (verified:
selecting the option early showed the browser's native "Vul dit veld in"
validation on every other still-empty required field). `select_option_by_label`
now attaches a one-time capturing submit-preventing guard to every form right
before clicking an option.

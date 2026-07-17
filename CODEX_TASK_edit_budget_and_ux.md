# Task: stop wasted edit-budget on ungroundable fabrications + clarify the review UI

## Context — this is NOT a validation bug

Confirmed via a real run (NVIDIA posting, resume unchanged except a
Technologies reorder): the local model proposed 6 Experience/Projects bullet
edits, and **all 6 tried to append a fabricated claim of Python experience**
to bullets that never mention Python — e.g. appending "...Utilized Python for
efficient data handling." to a bullet about ticket pagination/JSON pipelines.
`resume_parser.py`'s `validate_edit` correctly rejected every one via the
existing entity-grounding and token-grounding checks
(`resume_parser.py:250-275`). The safeguards are working as designed — do
**not** loosen entity grounding or numeric grounding, those are the actual
anti-fabrication mechanism and are not the problem here.

The real, worthwhile problem: `generate_edits` in `llm.py` caps proposals at
6 (`wire_schema`'s `maxItems: 6` at `llm.py:127`, and
`ProposedEditsResponse.edits` capped at `max_length=12` in `models.py:44`,
whichever binds first). In this run, the model spent all 6 of its attempts on
the same doomed pattern (fabricate Python usage) across different bullets,
leaving zero budget for any other, legitimately groundable rewording it might
otherwise have found elsewhere in Experience/Projects. This is a prompt/
product problem, not a security one.

## Task 1 (minor) — strengthen the system prompt with a concrete negative example

File: `backend/app/llm.py`, the `system` string inside `generate_edits`
(currently `llm.py:95-107`).

Add one or two sentences giving a concrete example of the exact failure mode
observed, so the model recognizes it up front instead of spending its
edit budget discovering it via rejection:

> Do not propose adding a sentence or clause naming a skill, tool, or result
> the target bullet does not already describe, even if that skill appears
> elsewhere on the resume (e.g., in Technologies) or in the job posting. If
> no bullet can be truthfully connected to a posting requirement, propose
> fewer edits — do not force an ungroundable one.

This is a pure prompt-text change — no schema, no validation logic, no
behavior change to what's *allowed*, only what the model is nudged to
attempt. Low risk, quick to apply, safe to do without extended review.

## Task 2 (minor) — surface "why nothing changed" more plainly in the popup UI

Files: `extension/popup.js` (`renderResults`), `extension/popup.html`.

Right now a user has to read raw validation-issue strings per card (e.g.
`entity is not grounded in the original target bullet: Python`) to work out
the pattern themselves. Add a plain-language summary above the edit cards
when most/all bullet-targeting edits share the same rejected entity — e.g.:

> Most rejected edits tried to add "Python" to a bullet that doesn't
> currently describe it. Nothing was changed there because that would be an
> unsupported claim, not because of a technical error.

Implementation sketch: after computing `flaggedCount`/`selectableCount` in
`renderResults` (`popup.js` around line 34-40), scan
`edit.issues` for the `entity is not grounded in the original target bullet:
X` pattern across all edits targeting `Experience`/`Projects`, tally by `X`,
and if one entity accounts for a majority of the flagged bullet edits, render
that sentence. This is purely additive UI text — don't change any existing
field names, don't touch the backend response shape.

Also add short inline labels to each edit card so a non-technical reader
doesn't have to infer what the two stacked text blocks are — e.g. a small
`<span class="label">Current:</span>` before the old-text block and
`<span class="label">Proposed:</span>` before the new-text block in the
`renderResults` card-building loop (`popup.js`, the `$("#edits").replaceChildren(...)`
block). Purely cosmetic, no logic change.

## Task 3 (optional, larger — do not implement unless the user explicitly asks for it in a follow-up)

Judgment call, not a bug fix: currently the token-level word-grounding check
in `validate_edit` (`resume_parser.py:258-275`) requires every content word
in a proposed bullet to trace back (via crude suffix-stripping) to a word
already in that exact original bullet, which in practice limits "tailoring"
to reordering/trimming existing wording rather than natural rephrasing (e.g.
it would reject swapping "leveraging" for "using" even though that changes
no fact). This check is separate from, and stricter than, the entity/number
grounding that actually prevents fabrication — loosening *only* this
specific check (leave entity grounding, numeric grounding, and copied-bullet
detection fully intact) would allow more natural-sounding rewording without
opening the door to new fabricated facts. This is a product/risk-tolerance
decision the user should make explicitly, not something to change
preemptively — flag it to them but wait for a clear go-ahead before touching
`resume_parser.py`'s grounding logic.

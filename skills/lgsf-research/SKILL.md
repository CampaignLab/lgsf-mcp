---
name: lgsf-research
description: >-
  Research UK local government using the lgsf-mcp tools over the Council
  Gateway API. Use when asked about UK council minutes, committee decisions,
  councillors, local government meetings, planning or licensing decisions,
  officer and delegated decisions, council tax or local policy debates, or
  "what did <council> decide about <topic>". Teaches the data model, the
  search-then-read loop, when to filter instead of search, and the coverage
  and party-naming traps that otherwise produce confidently wrong answers.
---

# Researching UK councils with lgsf-mcp

These tools query the Council Gateway API: real scraped minutes, agendas,
reports, decisions and councillor records from UK local authorities. The
individual tool descriptions tell you what each call does. This skill tells
you how to *sequence* them, and which plausible-looking answers are wrong.

## The data model

Two branches hang off a council, and documents span both:

```
council ──┬── committee ──── meeting ──┬── documents  (agendas, reports, minutes)
          │                            └── decisions  (taken at the meeting)
          │
          └── decision (officer / delegated, outside any meeting)
                       └── documents
          └── person   (councillors)
```

The thing that trips models up: **documents are polymorphic**. Every document
carries `source_type` (`"Meeting"` or `"Decision"`) and `source_id` pointing
at whichever parent it belongs to. If you assume documents only hang off
meetings you will miss every officer-decision report in the dataset. When you
have a document and want its context, branch on `source_type`:

- `source_type == "Meeting"` → `get_meeting(source_id)`
- `source_type == "Decision"` → `get_decision(source_id)`

**Only documents are searchable.** `search_council_data` indexes document
text and (in `classification` mode) LLM-generated document classifications.
Meetings, decisions and people are **not** in the index. Searching for a
councillor's name finds documents that *mention* them, never their person
record; searching for a committee name finds papers, never the meeting. To
reach a meeting, decision or person you must filter, or arrive via a document.

## The canonical research loop

Search for the topic, read the highlights, read the document text, then get
the surrounding context. Here is that loop run for real against the live API,
with the actual values returned (re-verified 2026-08-23). Treat the totals and
counts below as an illustration of the shape of the responses, not as figures
to quote: the corpus grows continuously.

### 1. Search

```
search_council_data(q="low traffic neighbourhood", size=3)
```

Returned `total: 340`, `total_is_capped: false`, three hits. Third hit:

```json
{
  "id": "Document-1199223",
  "score": 10.270778,
  "document_id": 1199223,
  "meeting_id": 95384,
  "meeting_name": "Culture and Environment Scrutiny Panel - Tuesday, 11th November, 2025 7.00 pm",
  "title": "Culture and Environment Scrutiny Panel - Tuesday, 11th November, 2025 7.00 pm",
  "highlights": [
    "Members sought clarification on the difference between **Low** **Traffic**\n       **Neighbourhood** (LTN) and Quieter Neighbourhood (QN) schemes."
  ]
}
```

Keep queries **short and phrase-like**. This is a phrase query with slop, so
`"low traffic neighbourhood"` works and `"what did the council say about low
traffic neighbourhoods last year"` does not.

### 2. Read the highlights before fetching anything

`highlights` gives you the matched sentence with terms in `**bold**`. That is
frequently enough to judge relevance, and it costs nothing extra. Only fetch
full text for the hits whose highlights actually answer the question. Note
the `title` here is the *meeting* name — document `name` is usually null, so
search falls back to the meeting name. Two hits with the same title can be
two different documents.

### 3. Read the document text, in windows

```
get_document(document_id=1199223, include_text=True, max_chars=2000)
```

Returned:

```json
{
  "id": 1199223,
  "source_type": "Meeting",
  "source_id": 95384,
  "url": "https://governance.enfield.gov.uk/documents/g15232/Printed+minutes+11th-Nov-2025+19.00+Culture+and+Environment+Scrutiny+Panel.pdf?T=1",
  "kind": "unclassified",
  "extract_status": "success",
  "is_minutes": false,
  "text_length": 7703,
  "text_offset": 0,
  "has_more": true,
  "next_offset": 2000
}
```

with `text` beginning:

```
       CULTURE AND ENVIRONMENT SCRUTINY PANEL - 11.11.2025
MINUTES OF THE MEETING OF THE CULTURE AND
ENVIRONMENT SCRUTINY PANEL HELD ON TUESDAY,
11TH NOVEMBER, 2025
MEMBERS: Councillors Hivran Dalkaya (Chair), Eylem Yuruk (Vice-Chair),
Nawshad Ali, Alessandro Georgiou, Patricia Gregory, Nelly Gyosheva and
Joanne Laban
```

`has_more` is true, so read on by feeding `next_offset` back in:

```
get_document(document_id=1199223, include_text=True, text_offset=2000, max_chars=2000)
```

which returned `text_offset: 2000`, `next_offset: 4000`, `has_more: true`,
continuing mid-sentence:

```
the proposed Controlled Parking Zones (CPZs) in
Edmonton Green and Ponders End, which were considered the most
contentious. Members reported that feedback from residents indicated a
preference for tim
```

**Loop while `has_more` is true.** Check `text_length` first and decide:
7703 characters is three or four windows, so just read it. If `text_length`
were 400000, do not page through the lot — search within that council
instead (`search_council_data(q=..., council_id=...)`) to find the relevant
passage, then window around it. Windows cut mid-word, so stitch the pieces
before quoting.

### 4. Get the surrounding context

```
get_meeting(meeting_id=95384)
```

Returned the meeting: `name` "Culture and Environment Scrutiny Panel -
Tuesday, 11th November, 2025 7.00 pm", `date` `"2025-11-11T00:00:00"`,
`council_id` 176, `committee_id` 11183, `committee` `{"id": 11183,
"council_id": 176, "name": "Culture and Environment Scrutiny Panel", ...}`,
and `url` the council's own page.

If you need the authority itself:

```
get_council(council_id=176)
```

returned `name` "Enfield", `council_type_label` `"modern_gov"`,
`majority_party` `"LAB"`, `committees_total` 75.

### 5. Cite

Cite the council, the committee, the meeting date and the council's own
`url` — from the meeting record or the document record, both of which are
real public links. Quote the extracted text, not your summary of it, when
the claim is contentious. If you paged through windows, make sure the quote
does not straddle a window boundary you stitched badly.

## When to filter instead of search

Search answers *what*. Filters answer *who* and *when* — and filters are the
**only** way to reach meetings, decisions and people at all, since none of
them are indexed.

| You know | Use |
| --- | --- |
| A topic, no council | `search_council_data(q=...)` |
| A topic and a council | `search_council_data(q=..., council_id=...)` |
| A council and a period | `list_meetings(council_id=..., date_from=..., date_to=...)` |
| A specific committee | `get_council(id)` for committee ids, then `list_meetings(committee_id=...)` |
| You want officer decisions | `list_decisions(council_id=..., is_key=True)` — never search |
| You want a councillor | `list_people(council_id=...)` — never search |

Verified live: `list_meetings(council_id=176, committee_id=11183,
date_from="2025-11-01", date_to="2025-11-30")` returned `total: 1` — exactly
the meeting the search hit came from. That is the cheap, exact route when you
already know the body and the period.

There is **no name search for councils**. To turn "Enfield" into id 176, page
`list_councils(per_page=100)` and match `name` client-side. Same for people:
`list_people` has no name filter, so filter by `council_id` and match `name`
yourself. `role` has the form `"Councillor, <ward>"` — the ward is embedded
there, there is no ward field.

## The remaining tools

Two tools the loop above does not reach, and two you should ignore.

- **`get_document_file_url(document_id=...)`** gives a presigned link to the
  original PDF. Use it when the user wants the source document itself rather
  than your reading of it — a link they can open and check is often the most
  useful thing you can hand over. It expires in about 600 seconds, so present
  it straight away rather than holding it. It legitimately 404s for documents
  whose `extract_status` is `pending` or `failed`, so check that first.
- **`get_person(person_id=...)`** returns one councillor. Ids only come from
  `list_people`; there is no lookup by name. A 404 means that id is not in
  the dataset — it is **not** evidence the person does not hold office. The
  dataset is councillors only, so officers are absent by construction.
- **`search` and `fetch`** exist solely for ChatGPT, which can only call
  tools with those exact names. Ignore them: `search_council_data` and
  `get_document` do the same job with filters, paging, highlights and text
  windowing that the aliases throw away.

## The party-counting trap

**Never report a filtered `party` total as a party's councillor count.**
`party` is unnormalised free text matched exactly, and the same party is
spelled many ways. Live totals from `list_people` (measured 2026-08-23; these
drift as councils are rescraped, so re-measure rather than quoting them):

| filter | `total` |
| --- | --- |
| none | 19745 |
| `party="Conservative"` | 3089 |
| `party="Conservative Party"` | 337 |
| `party="The Conservative Party"` | 167 |
| `party="Local Conservatives"` | 125 |

Those four spellings already sum to 3718 against the 3089 that
`party="Conservative"` reports — a 20% undercount, and that is only the
spellings someone thought to guess. A single page of 100 people contained
**27 distinct party values**, including six spellings of Labour (`Labour`,
`Labour Party`, `Labour and Co-operative`, `Labour and Co-operative Party`,
`Labour & Co-operative`, `Labour & Cooperative Party`) and four of the
Liberal Democrats (`Liberal Democrat`, `Liberal Democrats`,
`(Liberal Democrats)`, `Scottish Liberal Democrat Party`).

To count or gather a party properly: page `list_people(per_page=100)`
unfiltered (or per council), collect the `party` strings, and group them
yourself — normalising case, punctuation, `&`/`and`, and the leading `The`.
Say in your answer how you grouped them, because the grouping is a judgement
call and someone may reasonably disagree about, say, whether
`Labour and Co-operative` counts as Labour.

Two related snags: `party` is sometimes null, and the `majority_party` field
on **council** records uses unrelated short codes (`LAB`, `CON`, `NOC`) that
match no person — do not pass them to `list_people(party=...)`.

## Performance

- **Never call `list_documents(council_id=...)` on its own.** A council has
  tens of thousands of documents and this takes up to ~15 seconds. Scope it
  with `meeting_id` or `decision_id`, or use search.
- `get_meeting` already returns documents, so you rarely need
  `list_documents(meeting_id=...)` at all.
- `search_council_data`'s `total` is **capped at 10000** — when
  `total_is_capped` is true it is a floor, not a count. Report it as "at
  least 10000", never as an exact figure.
- Use `include_text=False` (the default) when you only want document
  metadata. Only set it true when you intend to read.
- Set `max_chars` to what you actually need. It is clamped at 50000.
- For paging listings, `per_page=100` is the upstream maximum and is what you
  want when scanning.

## Coverage: absence of evidence is not evidence of absence

**This is the single most important caveat if anyone will draw a political or
journalistic conclusion from your answer.**

The dataset covers councils whose websites run ModernGov (plus a few CMIS) —
roughly **80% of UK councils**. Live on 2026-08-23, `list_councils` reported
`total: 320` (289 `modern_gov`, 31 `cmis`); check it with a single
`list_councils(per_page=1)` rather than quoting that number, since councils
are added over time. Hundreds of UK authorities publish through other systems
and are simply not here.

So an empty result means **one of**:

1. The thing did not happen; or
2. It happened at a council that is not scraped; or
3. It happened but the document's text extraction failed or is pending; or
4. It happened but your phrase does not match the wording used.

You cannot tell these apart from a null result. Never write "no council has
discussed X" or "council Y never debated Z". Write "I found no record of X in
the councils covered by this dataset (roughly 80% of UK authorities),
searching for the phrase '...'". Before concluding absence, try at least one
reworded phrase, and check the council is in the dataset at all.

## Data-quality traps found in live data

These are things the data does, not things the API documents.

- **`is_minutes` under-reports.** Document 1199223 above is literally
  `Printed+minutes+...pdf`, has `extract_status: "success"` and contains the
  minutes text — and its `is_minutes` is **false**. Every single document on
  that meeting had `is_minutes: false` — all 161 of them, measured 2026-08-23.
  So `list_documents(is_minutes=True)` and `list_meetings(has_minutes=True)`
  silently drop real minutes. Treat a true value as a positive signal;
  **never treat a false value as "this is not minutes"**, and never conclude
  from `has_minutes=True` returning nothing that a council published no
  minutes. Search the text, or check document `url`s for "minutes".
- **`get_meeting`'s `documents` is not scoped to that sitting.** Meeting
  95384 — the Culture and Environment Scrutiny Panel, dated 11 November 2025 —
  returned over 150 documents whose `created_at` spans several weeks either
  side of it (161 documents created between 10 Nov and 8 Dec 2025, measured
  2026-08-23; the set grows as the council publishes).

  A date range alone could be explained away as papers published late. The
  `url`s cannot: most of them belong to **other committees entirely**. On that
  Scrutiny Panel sitting you will find

  ```
  https://governance.enfield.gov.uk/documents/g15224/Decisions+05th-Nov-2025+19.00+Cabinet.pdf?T=2
  ```

  — a **Cabinet** decisions pack from 5 November, six days before the meeting
  — alongside a Licensing Sub-Committee agenda, a Full Council reports pack
  for 12 November and a Councillor Conduct Committee pack for 17 November.
  Only a handful of the documents on that meeting even mention the panel's own
  name in their `url`.

  So do not assume a document listed on a meeting was published *for* that
  meeting, and never attribute its contents to that committee. Check
  `created_at` and the document `url` against the meeting `date` and the
  committee `name` before citing anything.
- **`extract_status` has three values, not two**: `success`, `pending` and
  `failed`, and `failed` is common — well over one in ten of that meeting's
  documents (22 of 161, measured 2026-08-23). A `failed` document has no text
  and never will; a `pending` one may get some later.
  `get_document(include_text=True)` on either returns empty `text` with
  `text_length: 0` — check `extract_status` before concluding the document
  is blank.
- **`attendees` is often empty.** Four meetings sampled across two councils
  (Enfield 95384; Aberdeen 130462, 116785, 115599) all had `attendees: []`
  and `additional_attendees: []`, on both 2026-08-23 and the earlier sampling.
  Do not rely on it for who was present — the minutes text carries the
  attendance list (see the `MEMBERS:` block in the extracted text above).
- **LLM summary fields are usually null.** `topline`, `about` and `notes` on
  meetings, and `topline` on decisions, were null in every record sampled.
  Read them if present; do not plan a strategy around them.
- **Dates run into the future.** Aberdeen meetings dated March and August
  2026 exist, with zero documents. A future-dated meeting with no papers is
  a scheduled sitting, not a missing document.
- **Some people's `name` is a bare number**, an artefact of scraping. Sanity
  check names before quoting them.

## Answering well

- Distinguish **what a document says** from **what was decided**. A report
  recommending X is not the council deciding X; look for the meeting's
  `decisions`, or the minutes text recording the resolution.
- A petition or objection quoted in minutes is a *resident's* view, not the
  council's. The first search hit above — "Hackney Council has introduced a
  growing number of road closures ... without proper consultation" — is a
  petition's wording, not Hackney's position.
- Give the council, committee, date and link every time. Local government
  claims are checkable, and readers will want to check them.
- When the data is thin, say so plainly rather than padding. "The minutes
  record the discussion but not the vote" is a better answer than an
  inferred vote.

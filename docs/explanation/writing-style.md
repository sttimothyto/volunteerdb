# How this manual is written

This manual is written in a subset of ASD-STE100, Simplified Technical
English. STE is the controlled language of the aerospace industry. Its rules
make text easy to read for people who do not know the subject, or the
language, well. The rules below are the house subset. A test holds every page
to the rules a machine can check.

## Who the manual is for

- The default reader is a parishioner who can click in a web page and knows
  nothing else about the software.
- The technical pages are for the people who run or develop the site. They
  follow the same rules, so the two halves read as one manual.

## The rules

### Words

- Use one word for one thing, and the same word each time. A team is a
  *team*, never a *ministry* on one page and a *group* on the next.
- Use a word in one meaning only. To *close* is what you do to a dialog, not
  a distance.
- Technical names stay as they are: the label on a button (*Sign out*), the
  name of a page (the *Dashboard*), a setting (`VDB_TIMEZONE`). Write a label
  in italics, exactly as the screen shows it.
- You can use technical verbs: *click*, *select*, *type*, *open*, *sync*.
- Write numbers as digits: 3 teams, 24 hours.
- Do not use the words in the table below. Use the replacement.

| Do not write | Write |
|---|---|
| should | must |
| may (permission or chance) | can |
| ensure | make sure |
| utilize | use |
| prior to | before |
| in order to | to |
| via | through, with |
| e.g. | for example |
| i.e. | that is |
| etc. | (list all the items) |
| allow | let |
| perform | do |
| obtain | get |
| locate | find |
| don't, it's, you're | do not, it is, you are |

### Sentences

- Keep an instruction to 20 words or fewer. Keep a descriptive sentence to
  25 words or fewer.
- Give one instruction in one sentence. If a step has two actions, write two
  steps.
- Write an instruction in the imperative: *Click Save.* Not: *The Save button
  is then clicked.*
- Use the active voice. Say who does what.
- Use these verb forms only: the imperative, the simple present, the simple
  past, the past participle as an adjective, and the future with *will*.
- Do not use the *-ing* form of a verb. Write *the page loads*, not *the page
  is loading*. An *-ing* word is fine inside a technical name (the *Workload*
  setting).
- Put an article (*a*, *an*, *the*) before a noun.
- Use *if* for a condition. Use *must* for a requirement and *can* for a
  possibility.

### Paragraphs and lists

- Keep a paragraph to one topic and to 6 sentences or fewer.
- Write a sequence of actions as a numbered list, one action per item.
- Write a set of options or facts as a bulleted list, one fact per item.
- Put a warning or a caution before the step it applies to. Start it with a
  command: *Do not close the window.*

## The shape of each kind of page

The manual follows [Diátaxis](https://diataxis.fr/): four kinds of page, each
with one job.

- A **tutorial** takes a new reader through a task once, step by step. It
  says what the reader sees after each step.
- A **how-to guide** gets one task done. It has a goal, what you need before
  you start, the numbered steps, and what you see when the task is done.
- A **reference** page states facts: tables and bullets, no story.
- An **explanation** page says why something is the way it is. It is the one
  kind of page written in paragraphs. The word and sentence rules still apply.

## The test that holds the line

- `tests/test_docs_style.py` reads every page under `docs/`. It counts the
  sentences over the limit, the paragraphs over the limit, and the banned
  words.
- The counts it found when it was written are its baseline. A count can only
  go down. A count that reaches zero must leave the baseline.
- To list the problems on a page with line numbers, run
  `uv run python tests/test_docs_style.py docs/how-to/deploy.md`.
- To print a new baseline, run the script with no arguments.
- The test does not see voice, verb forms, or word choice outside its list. A
  reviewer does.

## Source

- ASD-STE100, Issue 8 (2021), published by ASD, the AeroSpace, Security and
  Defence Industries Association of Europe. The specification is free after
  registration at <https://www.asd-ste100.org/>.

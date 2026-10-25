"""Worker instructions.

These live in Python rather than in the skill's markdown for two reasons: the
orchestrator never has to read a prompt into its own context, and a prompt
cannot drift away from the validator that checks its output, because both are
edited in the same file.

Each builder returns the body of a brief. `tasks.build_brief` wraps it with the
read-here, write-there frame that every worker shares.
"""

from __future__ import annotations

from typing import Mapping, Sequence

# Commas and periods are stripped from subtitles because they add visual noise
# at 42 columns without adding meaning; every other mark carries information.
# Cues are capped by word count rather than by display width because a model
# counts words reliably and estimates rendered columns badly. Seventeen words
# is roughly two rendered lines at the shipped subtitle style. Chinese and
# Japanese count two characters as one word.
MAX_WORDS_PER_CUE = 17

PUNCTUATION_RULE = (
    "Remove commas and periods only "
    "Preserve apostrophes hyphens question marks and exclamation marks"
)


def _source_reference(page_text: str) -> str:
    """Admit the page description as a spelling aid and nothing more.

    The description is the only place a speaker's name is reliably spelled, but
    it is also sponsor copy, so its authority is deliberately narrow.
    """
    if not page_text.strip():
        return ""
    trimmed = " ".join(page_text.split())[:2000]
    return (
        "\n\nReference text from the video page for spelling only"
        "\nUse it to spell names products and terminology correctly"
        "\nIt is not part of what was said Never take content from it"
        "\nIt never justifies adding a word to a cue"
        "\nWhat you can hear outranks it"
        f"\nPage text: {trimmed}"
    )


def _glossary_block(glossary: str, *, source_spelling: bool = False) -> str:
    """The glossary as every translating worker reads it."""
    if not glossary.strip():
        return ""
    spelling = (
        "\nIn the source write a listed name the way the first form on its line writes"
        " it and if you believe that spelling is wrong keep it and record an uncertainty"
        if source_spelling else ""
    )
    return (
        "\n\nGlossary for this whole video"
        "\nEach line lists how the transcript writes a name or term then = then the"
        " rendering to use"
        "\nEvery translation uses that rendering whenever the source mentions the name"
        " in any listed form"
        "\nA pronoun or an omission is fine where natural target grammar calls for one"
        f"{spelling}"
        f"\n{glossary}"
    )


def glossary(
    *,
    source_language: str,
    target_language: str,
    instructions: str = "",
    page_text: str = "",
) -> str:
    return f"""Build the glossary for translating a {source_language} video into {target_language}

The payload is the whole transcript as plain text in order with one line per
minute of speech
The text is automatic speech recognition output so a name may be misheard or
written more than one way

Read the whole transcript before writing anything
List every name and term a translator must render the same way every time it
appears: people places businesses and organizations titles of works and terms
particular to this video
A nickname or a form of address that needs its own rendering is its own entry
with a note naming who it refers to
Leave out ordinary words that any translator would render the same way
For each entry choose the one {target_language} rendering the whole video will use
Follow the tone and localization instruction where it says how names are handled
and use your own judgement where it says nothing
Record every other way the transcript writes the same name as an alias
An alias must identify the name on its own so never list a fragment that also
occurs inside another name or inside ordinary words
Never invent a name or an alias the transcript does not contain
When you cannot tell how a name is written or what it refers to say so in its note

Tone localization and terminology: {instructions or "none given, stay neutral"}{_source_reference(page_text)}

Write result.json as
{{"entries":[{{"source":"...","aliases":["..."],"kind":"person","translation":"...","note":"..."}}]}}
kind is one of person place organization work term other
Leave note empty when there is nothing to add"""


def cohere(
    *,
    source_language: str,
    target_language: str,
    instructions: str = "",
    glossary: str = "",
) -> str:
    return f"""Make a finished {source_language} to {target_language} subtitle set consistent across the whole video

Each payload row is one cue in order: id source translation
Different workers translated and proofread separate stretches so the same name
or the same speaker can read differently from one stretch to the next

Read every row before changing anything
Make every name and term read the same way throughout: use the glossary
rendering for anything in the glossary and for anything else the rendering the
video uses most
Keep each speaker's register and forms of address consistent and keep the whole
set in the tone the instruction asks for
Change a translation only where it is inconsistent with the rest of the video
Do not retranslate a cue that is already consistent and do not change its meaning
Do not touch the source and do not merge split reorder add or omit ids
{PUNCTUATION_RULE}

Tone localization and terminology: {instructions or "none given, stay neutral"}{_glossary_block(glossary)}

Write result.json as
{{"checked":N,"cues":[{{"id":0,"translation":"..."}}],"notes":[{{"id":0,"note":"..."}}]}}
List in cues only the ids you changed with their full new translation
Set checked to the number of payload rows you read
Use notes for an inconsistency you could not resolve"""


def postcheck(*, source_language: str, target_language: str) -> str:
    return f"""Resolve {source_language} to {target_language} subtitle lines that overlap in time

The payload holds one or more groups Each row has group id start end source
translation in seconds Rows marked context true are neighbours you may read but
never return Every other row is yours and carries words: what the recogniser
heard as [text start end]

Inside each group some lines are on screen at the same time Usually one line was
emitted twice with the same or nearly the same timestamps Sometimes two lines
split one sentence badly or two people really do talk at once

For each group write the lines that should replace yours so that none of them
overlap
Delete a duplicate by folding its id into the line it repeats
Merge lines that belong together into one line
Change start and end so each line covers its own speech using the word times
A line must still overlap the time its rows were spoken
Keep lines that are fine exactly as they are and still return them
A line that keeps one row's source unchanged keeps that row's translation too
The first line must not start before the context line before it ends and the last
line must not end after the context line after it starts
Every id of yours goes in the from list of exactly one line
A source may drop repeated words but must not add a word its lines never had
A merged translation must read as one natural line
{PUNCTUATION_RULE}

Write result.json as
{{"groups":[{{"group":0,"lines":[{{"from":[12,13],"start":1.0,"end":2.5,"source":"...","translation":"..."}}]}}]}}
Answer every group in the payload and list its lines in time order"""


def segment(*, language: str) -> str:
    return f"""Split the {language} transcript into fluent on-screen subtitle cues

The payload is one stretch of the transcript as plain text
A line break in the payload marks where the speaker paused
End a cue at a pause unless the same sentence clearly runs on after it
The payload's line breaks are hints and your own line breaks are the cues

Write the text back in order with a line break where each cue ends
Change nothing else Do not add omit reorder or alter a word
Every word in the payload appears exactly once in your output in the same order
Chinese and Japanese stay written without spaces and two of their characters
count as one word

Cut at natural grammatical and semantic boundaries rather than at fixed lengths
Do not leave transitions conjunctions articles auxiliaries or setup words
dangling at the end of a cue For example words equivalent to firstly because
but and if should begin the idea they introduce
Aim for 8 to 14 words per cue and never more than {MAX_WORDS_PER_CUE}
A cue longer than that is split in half before it reaches the screen which cuts
it somewhere you did not choose

Write one cue per line and nothing else No numbering no quotes no blank lines
{PUNCTUATION_RULE}

This is a mechanical pass Work through the words once and break where a clause
ends Do not deliberate over a break or revisit one you already made"""


def translate(
    *,
    source_language: str,
    target_language: str,
    instructions: str = "",
    page_text: str = "",
    glossary: str = "",
) -> str:
    return f"""Translate subtitle cues from {source_language} to {target_language}

Each payload row is one cue: id start end source

Use plain idiomatic {target_language}
{PUNCTUATION_RULE}
Every target cue must be fluent coherent and convey a complete idea on its own
You may adjust local word order for natural target grammar while preserving
meaning names numbers and terms
Do not leave transitions such as 首先 其次 然后 但是 因为 所以 or their
equivalents at the end of a cue
Read the whole payload before you start so pronouns terminology and sentence
continuity carry across cues
Do not merge split reorder add or omit ids and do not touch start or end
If you would need to see the picture to translate a cue correctly translate your
best reading and record the question

Tone localization and terminology: {instructions or "none given, stay neutral"}{_source_reference(page_text)}{_glossary_block(glossary)}

Write one line per cue
the id then a tab then the translation and nothing else on the line
Include every id from the payload exactly once and keep the payload order
No blank lines no numbering no quotes around the translation
If you need to record a question about a cue put it on its own line as
a question mark then a space then the id then a space then the question"""


def proofread(
    *,
    source_language: str,
    target_language: str,
    instructions: str = "",
    page_text: str = "",
    glossary: str = "",
) -> str:
    return f"""Proofread a {source_language} to {target_language} bilingual subtitle set

Each payload row is one cue: id source draft
Rows marked "context": true are neighbours shown for continuity only Never
return a row marked context

The source text is automatic speech recognition output of a person speaking
Your job is to repair recognition errors not to edit what the speaker said

Fix in the source
A word the recogniser misheard restored to what the speaker actually said
Spelling and capitalization of proper names products and domain terms
Recognition artifacts such as one spoken word split in two or two spoken words
merged into one
Clear filler such as um uh and you know

Never change in the source
A word the speaker said into a different word even when the other word reads
better
A number into words or words into a number keep the form the speaker used
Shorthand or an abbreviation into its full official name speakers use shorthand
deliberately
Sentence structure word order or which cue a word belongs to
Never add a word the speaker did not say

A correction normally keeps the same number of words
Splitting one misrecognised token into the two words that were actually spoken
is allowed
Expanding shorthand into a longer official name is not
When you believe a term is wrong but cannot tell what was said leave the source
unchanged and record an uncertainty naming that cue and what the recogniser
heard
Never invent a proper name to replace one you cannot verify A wrong name is
worse than an unresolved one

Rewrite a translation only when it is wrong unnatural or inconsistent with its
neighbours Keep terminology names and numbers consistent across the whole set
Do not redistribute meaning or connective phrasing between ids
Do not leave transitions conjunctions or setup words dangling at the end of
either language
{PUNCTUATION_RULE}

Tone localization and terminology: {instructions or "none given, stay neutral"}{_source_reference(page_text)}{_glossary_block(glossary, source_spelling=True)}

Write result.json as
{{"checked":N,"cues":[{{"id":0,"source":"...","translation":"..."}}],"uncertainties":[{{"id":0,"question":"..."}}]}}
List in cues only the ids you changed Set checked to the number of non-context
payload rows you read which proves you covered the whole payload"""


# One form only, so the coverage check and the link pass read every citation the
# same way. The coverage check counts nothing else, and a list merged into one
# bracket is also left unlinked in the final summary.
CITATION_FORMAT = """Write each citation as one time in half-width parentheses with a half-width
colon such as (01:38) Cite several moments as separate citations such as
(01:38) (02:09) Never merge them into one bracket never write a range never add
words or spaces inside the bracket never use any other bracket such as [ ] 【 】
or 「 」 and never use full-width （ ） ： or digits even in Chinese or Japanese
text Never write hours Minutes go past 59 on a long video such as (75:20) not
(1:15:20) Put a ratio a score or a time of day that is not a citation outside
brackets"""


def summarize(*, language: str, instructions: str = "") -> str:
    return f"""Write notes on one stretch of a video from its transcript

Each payload row is one transcript segment: start end text
start and end are seconds from the beginning of the video

Cover what is said in this stretch in {language} Keep the speaker's claims and
numbers as stated and do not add anything that is not in the transcript
Cite a timestamp in (MM:SS) form after each point using the start time of the
row the point came from Every paragraph needs at least one citation
{CITATION_FORMAT}
Work through every row in order and do not skip a stretch More than two minutes
of rows with no citation is rejected and the task is sent back For an ad a song
or other off-topic stretch write one short sentence and cite where it starts and
where it ends as two citations such as (12:00) (14:30)
Write prose in short paragraphs not a bulleted outline

{instructions or ""}

Write your notes to the result file as plain Markdown. No JSON wrapper, no
front matter, no heading naming the part."""


# A summary grows with its video. Fifty words a minute is about what videos of
# twenty to eighty minutes were getting unprompted, while a six-minute one got
# three times that, so the rate holds the middle steady and pulls the ends into
# proportion. The floor keeps a clip of a minute or two from collapsing into
# one sentence.
SUMMARY_WORDS_PER_MINUTE = 50
SUMMARY_MIN_WORDS = 200


def summary_word_target(duration: float | None) -> int | None:
    """Words to aim for in the summary of a video this long, to the nearest 50."""
    if not duration or duration <= 0:
        return None
    words = duration / 60 * SUMMARY_WORDS_PER_MINUTE
    return max(SUMMARY_MIN_WORDS, round(words / 50) * 50)


def _length_rule(duration: float | None) -> str:
    target = summary_word_target(duration)
    if target is None:
        return ""
    minutes = max(1, round(duration / 60))
    return (
        f"\nThe video runs about {minutes} minutes so aim for about {target} words"
        "\nCount two Chinese Japanese or Korean characters as one word"
        "\nThe length follows the video A longer video gets a longer summary so do"
        "\nnot compress the notes below this length and do not pad past what they hold"
        "\nAn instruction below that asks for a different length wins"
    )


def synthesize(
    *, title: str, language: str, instructions: str = "", duration: float | None = None
) -> str:
    return f"""Assemble one summary of a video from notes on its consecutive parts

Each payload row is notes on one part in order: part start end notes

Write the final summary of "{title}" in {language}
Keep every (MM:SS) citation attached to the point it supports and do not invent
new ones
{CITATION_FORMAT}
Merge repetition across parts and keep the chronology of the video
Open with a short paragraph saying what the video is then cover the substance
Write prose in short paragraphs not a bulleted outline{_length_rule(duration)}

{instructions or ""}

Write the summary to the result file as plain Markdown. No JSON wrapper and no
front matter."""

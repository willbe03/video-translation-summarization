"""The glossary: one decision per name, shared by every worker that renders it.

A model reads the whole transcript and decides which names and terms need a
fixed rendering. This module only checks what it wrote, formats it for briefs,
and measures how closely the cues follow it. It never guesses what a proper
noun is.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any, Mapping, Sequence

KINDS = ("person", "place", "organization", "work", "term", "other")


def normalize(value: str) -> str:
    """Width, case and spacing folded away, so a match does not depend on them."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def bare(value: str) -> str:
    """Folded like `normalize`, with punctuation and symbols removed as well."""
    return re.sub(r"[^\w]", "", normalize(value))


def validate(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Entries that have a source and a rendering, merged by source, and what was wrong."""
    entries: list[dict[str, Any]] = []
    by_source: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for index, row in enumerate(rows):
        source = str(row.get("source") or "").strip()
        rendering = str(row.get("translation") or "").strip()
        if not source or not rendering:
            problems.append(f"entry {index} has no source or no translation and was dropped")
            continue
        kind = str(row.get("kind") or "other").strip().lower()
        entry = by_source.get(normalize(source))
        if entry is None:
            entry = {"source": source, "aliases": [], "kind": kind if kind in KINDS else "other",
                     "translation": rendering, "note": str(row.get("note") or "").strip()}
            by_source[normalize(source)] = entry
            entries.append(entry)
        elif normalize(entry["translation"]) != normalize(rendering):
            problems.append(f"{source!r} was given two renderings; kept {entry['translation']!r}")
        for alias in row.get("aliases") or []:
            alias = str(alias).strip()
            known = {normalize(f) for f in (entry["source"], *entry["aliases"])}
            if alias and normalize(alias) not in known:
                entry["aliases"].append(alias)
    # A form inside another entry's form is ambiguous only when the renderings
    # disagree. A given name inside a full name is fine as long as the full
    # name's rendering contains the given name's rendering too.
    forms = [(normalize(form), form, entry)
             for entry in entries for form in (entry["source"], *entry["aliases"]) if normalize(form)]
    for key, form, entry in forms:
        for other_key, other_form, other in forms:
            if entry is other or key not in other_key:
                continue
            if bare(entry["translation"]) in bare(other["translation"]):
                continue
            problem = (f"{form!r} under {entry['source']!r} also occurs inside {other_form!r} "
                       f"under {other['source']!r}, whose rendering does not contain its own")
            if problem not in problems:
                problems.append(problem)
    return entries, problems


def brief_block(entries: Sequence[Mapping[str, Any]]) -> str:
    """One line per entry, the way a translator or proofreader reads it."""
    lines = []
    for entry in entries:
        heard = " / ".join((entry["source"], *entry.get("aliases", [])))
        note = f" ({entry['note']})" if entry.get("note") else ""
        lines.append(f"{heard} = {entry['translation']} [{entry['kind']}]{note}")
    return "\n".join(lines)


def _matcher(entries: Sequence[Mapping[str, Any]]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    """One pattern over every form of every entry, longest first, and each form's entry.

    A mention belongs to the longest form that matches it, so a short form of
    one entry never claims text that is part of a longer form of another.
    """
    owner: dict[str, str] = {}
    for entry in entries:
        for form in (entry["source"], *entry.get("aliases", [])):
            if normalize(form):
                owner.setdefault(normalize(form), entry["source"])
    if not owner:
        return None, owner
    return re.compile("|".join(map(re.escape, sorted(owner, key=len, reverse=True)))), owner


def _mentions(matcher: tuple[re.Pattern[str] | None, dict[str, str]], text: str) -> Counter[str]:
    pattern, owner = matcher
    if pattern is None:
        return Counter()
    return Counter(owner[match.group(0)] for match in pattern.finditer(normalize(text)))


def occurrences(entries: Sequence[Mapping[str, Any]], transcript: str) -> dict[str, int]:
    """How many times the transcript mentions each entry."""
    counts = _mentions(_matcher(entries), transcript)
    return {entry["source"]: counts[entry["source"]] for entry in entries}


def adherence(entries: Sequence[Mapping[str, Any]],
              cues: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each entry, the cues that mention it and those whose translation uses its rendering.

    A mention is found in what the recogniser heard, which no stage edits, so an
    entry has the same mentions at every stage and only the translations differ.
    A rendering counts as used whatever its width, case, spacing or punctuation.
    """
    matcher = _matcher(entries)
    mentioned: dict[str, list[int]] = {entry["source"]: [] for entry in entries}
    translations: dict[int, str] = {}
    for cue in cues:
        cue_id = int(cue["id"])
        translations[cue_id] = bare(str(cue.get("translation", "")))
        heard = str(cue.get("source_exact") or cue.get("source") or "")
        for source in _mentions(matcher, heard):
            mentioned[source].append(cue_id)
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        rendering = bare(entry["translation"]) or normalize(entry["translation"])
        ids = mentioned[entry["source"]]
        followed = {cue_id for cue_id in ids if rendering in translations[cue_id]}
        out[entry["source"]] = {"mentions": len(ids), "followed": len(followed),
                                "not_followed": [cue_id for cue_id in ids if cue_id not in followed]}
    return out


def coherence_changes(entries: Sequence[Mapping[str, Any]], before: Sequence[Mapping[str, Any]],
                      after: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Translations the coherence pass changed, how many only in typography, and how many changed a glossary rendering."""
    renderings = [bare(entry["translation"]) for entry in entries if bare(entry["translation"])]
    earlier = {int(c["id"]): str(c.get("translation", "")) for c in before}
    changed = typography = touching = 0
    for cue in after:
        was, now = earlier.get(int(cue["id"])), str(cue.get("translation", ""))
        if was is None or was == now:
            continue
        changed += 1
        if bare(was) == bare(now):
            typography += 1
            continue
        touching += any((rendering in bare(was)) != (rendering in bare(now)) for rendering in renderings)
    return {"translations_changed": changed, "typography_only": typography, "touching_glossary": touching}


def report(entries: Sequence[Mapping[str, Any]], transcript: str,
           stages: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Quantity, whether each entry is really in the transcript, and adherence per stage."""
    heard = occurrences(entries, transcript)
    per_stage = {name: adherence(entries, cues) for name, cues in stages.items()}
    summary = {}
    for name, rows in per_stage.items():
        mentions = sum(r["mentions"] for r in rows.values())
        followed = sum(r["followed"] for r in rows.values())
        summary[name] = {"mentions": mentions, "followed": followed,
                         "rate": round(followed / mentions, 3) if mentions else None}
    return {
        "entries": len(entries),
        "kinds": dict(Counter(e["kind"] for e in entries)),
        "aliases": sum(len(e.get("aliases", [])) for e in entries),
        "brief_characters": len(brief_block(entries)),
        "not_in_transcript": [source for source, count in heard.items() if count == 0],
        "adherence": summary,
        "per_entry": [
            {"source": e["source"], "translation": e["translation"], "kind": e["kind"],
             "heard": heard[e["source"]],
             **{name: f"{rows[e['source']]['followed']}/{rows[e['source']]['mentions']}"
                for name, rows in per_stage.items()}}
            for e in entries
        ],
    }

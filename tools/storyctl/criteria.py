"""Acceptance criteria: AC lines, and numbering criteria pasted from Jira (pure functions)."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *

AC_RE = re.compile(r'^(\s*)(?:[-*]\s+)?(?:\*\*)?AC\d+(?:\*\*)?\s*:')

def parse_criteria(story):
    """Each AC line plus its continuation lines (indented deeper than the AC line, or bulleted under an
    unbulleted AC line), up to a blank line or the next AC."""
    out, cur_ind, cur_bullet = [], None, False
    for l in story.splitlines():
        m = AC_RE.match(l)
        if m:
            out.append(l.strip()); cur_ind = len(m.group(1)); cur_bullet = bool(re.match(r'\s*[-*]\s', l))
            continue
        if cur_ind is None:
            continue
        if not l.strip():
            cur_ind = None; continue
        ind = len(l) - len(l.lstrip())
        if ind > cur_ind or (not cur_bullet and re.match(r'\s*([-*]|\d+[.)])\s', l)):
            out.append(l.rstrip())
        else:
            cur_ind = None
    return '\n'.join(out)

# Acceptance criteria as copied from Jira (or anywhere): numbered as AC1:, AC2:, ... without a model.
AC_HEAD_RE = re.compile(r'^\s*(?:h[1-6]\.\s*|#{1,6}\s+)?[*_]{0,2}\s*(?:acceptance\s+criteri(?:a|on)s?|a\.?c\.?s?)\b(?!\s*[#-]?\s*\d)[^:\n]{0,30}?[*_]{0,2}\s*(?::\s*(.*))?$', re.I)

END_WORDS = (r'definition\s+of\s+(?:done|ready)|dod|dor|notes?|technical\s+notes|tech\s+notes|implementation\s+notes|'
             r'out\s+of\s+scope|non[- ]goals?|dependencies|assumptions|risks?|designs?|mock-?ups?|attachments?|'
             r'description|background|context|test(?:ing)?\s+notes|qa\s+notes|open\s+questions|questions|links?|'
             r'resources|story\s+points|scope|summary|user\s+story|story|tasks?|sub-?tasks?|comments?|activity')

END_RE = re.compile(r'^\s*(?:h[1-6]\.\s*.*|#{1,6}\s+[*_]{0,2}(?:' + END_WORDS + r')\b.*|[*_]{0,2}(?:' + END_WORDS + r')[*_]{0,2}\s*:?\s*[*_]{0,2})$', re.I)

LABEL_RE = re.compile(r'^\s*[*_]{0,2}(?:AC|acceptance\s+criteri(?:a|on)|criteri(?:a|on)|scenario|req(?:uirement)?)\s*[#-]?\s*\d+[*_]{0,2}\s*(?:[:.)\-–—]\s*[*_]{0,2}\s*|\s+)(.*)$', re.I)

MARK_RE = re.compile(r'^(\s*)(#+|\*+|[-+•◦▪▸►–—·]|\(?(?:\d{1,2}|[a-zA-Z])[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$')

GWT_RE = re.compile(r'^\s*[*_]{0,2}(given|when|then|and|but)\b', re.I)

SEP_RE = re.compile(r'^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$')     # a Markdown table's |---|---| row

def _cells(l):
    l = l.strip()
    if '\t' in l: return [c.strip() for c in l.split('\t')], False
    head = l.startswith('||')
    l = re.sub(r'^\|\|?|(?<!\\)\|\|?$', '', l)
    return [c.strip().replace('\\|', '|') for c in re.split(r'(?<!\\)\|\|?', l)], head

def _number(lines, heading):
    """Criteria (list of lists of lines: first line, then continuation lines) from the lines of an AC section."""
    lines = [l.rstrip() for l in lines]
    rows = [l for l in lines if l.strip() and not SEP_RE.match(l)]
    if rows and all('\t' in l or l.strip().startswith('|') for l in rows) and len(rows) >= 2:
        cells = [_cells(l) for l in rows]
        hdr = None
        if cells[0][1] or all(len(c.split()) <= 3 and c for c in cells[0][0]):
            hdr = cells.pop(0)[0]
        out = []
        for c, _ in cells:
            if hdr and len(hdr) == len(c):
                out.append(['; '.join(f'{h}: {v or "(empty)"}' for h, v in zip(hdr, c) if h or v)])
            else:
                out.append([' | '.join(v for v in c if v)])
        return [o for o in out if o[0]], []
    items, depth = [], []
    for l in lines:
        m = LABEL_RE.match(l)
        if m and m.group(1).strip():
            items.append(('label', 0, m.group(1).strip())); continue
        m = MARK_RE.match(l)
        if m and LABEL_RE.match(m.group(3)) and LABEL_RE.match(m.group(3)).group(1).strip():
            items.append(('label', 0, LABEL_RE.match(m.group(3)).group(1).strip())); continue     # "- AC1: ..."
        if m:
            ind = len(m.group(1).expandtabs(4)); mk = m.group(2)
            d = ind + (len(mk) - 1) * 2 if mk[0] in '#*' else ind
            items.append(('mark', d, m.group(3).strip())); continue
        items.append(('blank', 0, '') if not l.strip() else ('plain', len(l) - len(l.lstrip()), l.strip()))
    marks = [d for k, d, _ in items if k == 'mark']
    labels = any(k == 'label' for k, _, _ in items)
    out, intro = [], []
    if marks or labels:
        top = min(marks) if marks else None
        for k, d, t in items:
            if k == 'label' or (k == 'mark' and d == top and not labels):
                out.append([t])
            elif k == 'mark' and out:
                out[-1].append('- ' + t)
            elif k == 'plain' and out:
                out[-1].append(t)
            elif k == 'plain':
                intro.append(t)                         # an intro line such as "The user can:"
        return out, intro
    texts = [t for k, _, t in items if k == 'plain']
    if any(GWT_RE.match(t) for t in texts):               # Given/When/Then scenarios without bullets
        for t in texts:
            w = GWT_RE.match(t); w = w.group(1).lower() if w else None
            steps = bool(out) and any(GWT_RE.match(x) for x in out[-1])
            if not out or (steps and w in (None, 'given')): out.append([t])
            else: out[-1].append(t)
        return out, []
    if heading or len(texts) >= 2:                       # one criterion per line (bullets lost in the copy)
        intro = texts[:1] if texts and texts[0].endswith(':') else []
        return [[t] for t in texts[len(intro):]], intro
    return [], []

def criteria_problem(story):
    """Why AC lines already in the story cannot be used as they are: labels in two styles ("AC1:" and
    "AC 2 -"), or a number used twice. '' when they can."""
    canon, other = [], []
    for l in story.splitlines():
        if AC_RE.match(l): canon.append(re.search(r'AC(\d+)', l).group(1)); continue
        t = MARK_RE.match(l).group(3) if MARK_RE.match(l) else l
        m = re.match(r'\s*[*_]{0,2}AC\s*[#-]?\s*(\d+)', t, re.I)
        if m and LABEL_RE.match(t): other.append(m.group(1))
    if canon and other: return 'the criteria are labelled in two styles'
    ids = canon or other
    if len(ids) != len(set(ids)): return 'a criterion number is used twice'
    return ''

def number_criteria(story):
    """(story with its acceptance criteria numbered "AC1:", "AC2:", ..., the numbered criteria, sure), or
    (story, '', False) when no criteria can be found. A story whose AC lines are consistent is returned as it is.
    sure is False when the developer should confirm the numbering before the run starts: criteria found without
    an "Acceptance criteria" heading in a paste that has other text before them, plain lines only, or AC labels
    that had to be renumbered (two styles, or a number used twice)."""
    crit0 = parse_criteria(story)
    problem = criteria_problem(story) if crit0 else ''
    if crit0 and not problem:
        return story, crit0, True
    lines = story.splitlines()
    start = next((i for i, l in enumerate(lines) if AC_HEAD_RE.match(l)), None)
    if start is not None:
        first = (AC_HEAD_RE.match(lines[start]).group(1) or '').strip()
        lvl = re.match(r'\s*(#{1,6})\s', lines[start])
        md_end = re.compile(r'^\s*#{1,%d}\s+\S' % len(lvl.group(1))) if lvl else None     # a Markdown heading of the same level ends it
        end = next((j for j in range(start + 1, len(lines)) if not AC_HEAD_RE.match(lines[j]) and
                    (END_RE.match(lines[j]) or (md_end and md_end.match(lines[j])))), len(lines))
        crit, intro = _number(([first] if first else []) + lines[start + 1:end], True)
        before, after = (lines[:start + 1] if not first else lines[:start] + ['Acceptance criteria:']) + intro, lines[end:]
    else:
        crit, _ = _number(lines, False)
        if not crit:
            return story, '', False
        # keep any text before the first criterion (a title, a description) as it is
        firsttext = crit[0][0]
        k = next((i for i, l in enumerate(lines) if firsttext in l), 0)
        before, after = lines[:k], []
        lead = any(l.strip() for l in before)
        if before and before[-1].strip(): before.append('')
        before.append('Acceptance criteria:')
    crit = [c for c in crit if c and c[0]]
    if not crit:
        return story, '', False
    block = []
    for n, c in enumerate(crit, 1):
        block.append(f'AC{n}: {c[0]}')
        block += ['  ' + x for x in c[1:]]
    tail = [''] + after if after else []
    new = '\n'.join(before + block + tail).rstrip() + '\n'
    plain = all(MARK_RE.match(l) is None and LABEL_RE.match(l) is None and GWT_RE.match(l) is None and '\t' not in l
                and not l.strip().startswith('|') for l in lines)
    labelled = any(re.match(r'\s*(?:[-*]\s+)?[*_]{0,2}AC\s*[#-]?\s*\d', l, re.I) for l in lines)
    problem = problem or criteria_problem(story)
    sure = not problem and (start is not None or (not plain and (labelled or not lead)))
    return new, parse_criteria(new), sure

__all__ = [n for n in dir() if not n.startswith('__')]

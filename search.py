"""Fuzzy matching for window search: 'disc' finds 'Friends - Discord', 'ovl' finds Overleaf,
'yt' finds '... - YouTube — Zen Browser'.  Every query word must match, in order, as a
subsequence; matches at word starts and runs of consecutive letters score higher."""
SEPARATORS = set(' -_./\\|()[]{}:—–·,#@')
WORD_START, CONSECUTIVE, BASE, GAP = 8.0, 5.0, 1.0, 0.15


def _bonus(text, j):
    if j == 0:
        return WORD_START + 2
    prev, cur = text[j - 1], text[j]
    if prev in SEPARATORS:
        return WORD_START
    if prev.islower() and cur.isupper():  # camelCase: YouTube, PowerShell
        return WORD_START - 1
    if prev.isalpha() != cur.isalpha():   # x2, v3
        return WORD_START / 2
    return 0.0


def word_score(q, text):
    """Best subsequence alignment of q in text, or None.  O(len(q) * len(text))."""
    lower = text.lower()
    n = len(text)
    if not q:
        return 0.0
    if n < len(q):
        return None
    prev = None
    for i, qc in enumerate(q):
        cur = [None] * n
        run = None  # max over k <= j-2 of prev[k] + GAP*k
        for j in range(n):
            if prev is not None and j >= 2 and prev[j - 2] is not None:
                v = prev[j - 2] + GAP * (j - 2)
                run = v if run is None or v > run else run
            if lower[j] != qc:
                continue
            gain = BASE + _bonus(text, j)
            if prev is None:
                cur[j] = gain - GAP * j * 0.2  # earlier first match is slightly better
                continue
            best = None
            if j >= 1 and prev[j - 1] is not None:
                best = prev[j - 1] + CONSECUTIVE
            if run is not None:
                gapped = run - GAP * (j - 1)
                best = gapped if best is None or gapped > best else best
            if best is not None:
                cur[j] = best + gain
        prev = cur
    vals = [v for v in prev if v is not None]
    return max(vals) if vals else None


def score(query, *fields):
    """Sum over query words of the best score in any field; None if some word matches nowhere."""
    words = query.lower().split()
    if not words:
        return 0.0
    total = 0.0
    for w in words:
        best = None
        for f in fields:
            s = word_score(w, f) if f else None
            if s is not None and (best is None or s > best):
                best = s
        if best is None:
            return None
        total += best
    return total


def rank_windows(query, windows):
    """windows in most-recently-used order; returns the matching ones, best first."""
    if not query.strip():
        return list(windows)
    scored = []
    for i, w in enumerate(windows):
        s = score(query, w.title, w.app)
        if s is not None:
            scored.append((s - 0.3 * i, i, w))  # recency breaks near-ties
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [w for _, _, w in scored]

"""Typo-tolerant matching shared by the ticker/coin searchers.

Pure stdlib (difflib). Searchers keep their exact-substring tiers as the
primary ranking and only fall back to `fuzzy_ratio` when those find nothing,
so fuzzy noise never dilutes good exact results.
"""

from __future__ import annotations

import difflib

# Minimum similarity to count as a match. 0.75 admits one-letter typos in
# short words ("APLE"→"APPLE" 0.89, "ORACEL"→"ORACLE" 0.83) while rejecting
# most unrelated pairs; callers rank survivors by score so near-misses that
# sneak past the cutoff sort below the intended hit.
FUZZY_CUTOFF = 0.75

# Queries shorter than this skip fuzzy entirely — 1-2 letters match half the
# universe at any sane cutoff.
MIN_QUERY = 3


def fuzzy_ratio(q: str, text: str) -> float:
    """Similarity score 0-1 between query `q` and `text`, word-aligned.

    Mean over q's words of each word's best difflib ratio against text's
    words, so a single-word query scores against the best word of a company
    title ("NVIDAI" vs "NVIDIA CORP" scores on NVIDIA) and a multi-word one
    needs every word to land ("BANK OF AMRICA" vs "BANK OF AMERICA CORP").
    Both sides are expected upper-cased by the caller.
    """
    qwords = q.split()
    twords = text.split()
    if not qwords or not twords:
        return 0.0
    total = 0.0
    sm = difflib.SequenceMatcher()
    for qw in qwords:
        sm.set_seq2(qw)  # difflib caches seq2; vary seq1 across text words
        best = 0.0
        for tw in twords:
            # A length gap > 3 can't reach the cutoff on the word lengths we
            # search; skipping it avoids the ratio() cost on 10k-row scans.
            if abs(len(tw) - len(qw)) > 3:
                continue
            sm.set_seq1(tw)
            if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
                continue
            best = max(best, sm.ratio())
        total += best
    return total / len(qwords)

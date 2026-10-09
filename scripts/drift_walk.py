"""Two 68000 bus streams walked together, board against referee, for the drift judges.

Each stream is a list of ``(t, word address, "r"/"w", held MCLK, data)``. From an anchor
in each, the walk steps both on together while they agree in address and kind, reports
every change in the time difference, and stops at the first cycle where the two part --
a different path, which is a difference in itself and the last place the streams can be
compared one to one. It is linear in the length walked; difflib's alignment, which the
judges used before, goes quadratic once the two part and took hours (docs/worklog.md,
"The judge's reach, corrected").

The referee logs an interrupt acknowledge as a read with A23-A4 high; the board does not
log it as a bus cycle, so the walk steps over a referee-only one when the next cycle
lines up.
"""

from __future__ import annotations


def walk(theirs: list[tuple], ours: list[tuple], a_r: int, a_b: int, span: int, *,
         changes: int = 60, min_step: int = 1, show: tuple[int, int] | None = None,
         data: bool = True) -> dict:  # fmt: skip
    """Print the walk's report; return its figures (``first``: the first step's time
    after the anchor or None, ``steps``, ``walked``, ``end``, ``parted``)."""
    t_r, t_b = theirs[a_r][0], ours[a_b][0]
    i, j = a_r, a_b
    last, steps, shown, acknowledges, first = None, 0, 0, 0, None
    differ: dict[int, list] = {}
    parted = False
    while i < len(theirs) and j < len(ours) and theirs[i][0] - t_r <= span:
        rc, bc = theirs[i], ours[j]
        if (rc[1], rc[2]) != (bc[1], bc[2]):
            following = theirs[i + 1] if i + 1 < len(theirs) else None
            if (rc[2] == "r" and rc[1] >= 0xFFFFF0 and following is not None
                    and (following[1], following[2]) == (bc[1], bc[2])):  # fmt: skip
                acknowledges += 1
                i += 1
                continue
            parted = True
            break
        diff = (bc[0] - t_b) - (rc[0] - t_r)
        if show and show[0] <= rc[0] - t_r <= show[1]:
            print(f"  {rc[0] - t_r:11,} {rc[2]} ${rc[1]:06X} h{rc[3]:3d} {rc[4]:04X} | "
                  f"{bc[0] - t_b:11,} h{bc[3]:3d} {bc[4]:04X} | {diff:+d}")  # fmt: skip
        if last is None or abs(diff - last) >= min_step:
            if last is not None and first is None:
                first = rc[0] - t_r
            if shown < changes:
                print(f"  +{rc[0] - t_r:11,}  diff {diff:+6d}  {rc[2]} ${rc[1]:06X}  "
                      f"held referee {rc[3]:3d} board {bc[3]:3d}")  # fmt: skip
                shown += 1
            steps += 1
            last = diff
        if rc[4] != bc[4]:
            differ.setdefault(rc[1], [0, rc, bc])[0] += 1
        i += 1
        j += 1
    end = theirs[i - 1][0] - t_r
    # The anchor's line counts as a step, as the judge has always printed it.
    print(f"{steps} steps of {min_step}+ MCLK; final difference {last:+d}")
    print(f"walked {i - a_r - acknowledges:,} cycles to +{end:,} "
          f"({acknowledges} referee acknowledges stepped over); first step "
          + (f"+{first:,}" if first is not None else "none"))
    if parted:
        print(f"the paths part at referee +{theirs[i][0] - t_r:,}:")
        for k in range(-6, 8):
            rc = theirs[i + k] if 0 <= i + k < len(theirs) else None
            bc = ours[j + k] if 0 <= j + k < len(ours) else None
            left = f"{rc[0] - t_r:11,} {rc[2]} ${rc[1]:06X} h{rc[3]:3d} {rc[4]:04X}" if rc else ""
            right = f"{bc[0] - t_b:11,} {bc[2]} ${bc[1]:06X} h{bc[3]:3d} {bc[4]:04X}" if bc else ""
            print(f"  {'>' if k == 0 else ' '} {left:42} | {right}")
    elif end < span - 100_000:
        print(f"a stream ends at +{end:,}, short of the span")
    if data and differ:
        print(f"{sum(n for n, _, _ in differ.values()):,} walked cycles with different "
              "data (the referee logs both halves of the data bus on a byte access):")  # fmt: skip
        for address, (n, rc, bc) in sorted(differ.items(), key=lambda x: -x[1][0])[:8]:
            print(f"  ${address:06X} {rc[2]} x{n}: first +{rc[0] - t_r:,} referee {rc[4]:04X} "
                  f"board {bc[4]:04X}")  # fmt: skip
    return {"first": first, "steps": steps, "walked": i - a_r, "end": end, "parted": parted}

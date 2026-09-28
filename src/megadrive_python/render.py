"""The VDP's picture: planes A and B, the window, sprites, priority, shadow/highlight.

A line at a time, drawn in pieces. :meth:`Renderer.begin_line` is called by the VDP
when a line's active display begins (its HBlank-clear point), so a raster effect
written in that line's horizontal blank -- where an H interrupt handler runs -- is
already in the registers. What the line needs *for the whole line* is read there: the
scroll values, the window's columns, and the sprites, which the hardware fetched
during the preceding horizontal blank (Kabuto, phase 3).

The pixels themselves are drawn lazily. Whenever the VDP is about to change something
the rest of the line would be drawn from -- a FIFO write landing at an access slot, a
DMA fill or copy, a register write -- it calls :meth:`catch_up` with the master clock
of that access slot, and the line is drawn up to the pixel the slot falls on before
the change lands (milestone 5). So plane names, patterns, scroll and the palette are
read where the hardware reads them, and a write made part way along a line shows from
that point on rather than from the next line. With nothing written mid-line the line
is drawn in one piece, exactly as before, which is what keeps it affordable.

Two effects come out of that machinery: r57shell's redraw-timing tests, which want
VRAM writes to land inside the line being drawn, and the **CRAM dots** -- a CRAM write
during the active display shows the colour written, for one pixel, at its access slot
(docs/undocumented-behavior.md; BlastEm's ``write_cram``, ``vdp.c`` lines 429-451, is
the only implementation pinned here, and is a detector).

Where each rule comes from (docs/validation.md ranks the sources):

- **Planes, scroll modes, plane sizes, the window and its column-fetch bug**: CMD
  ``genvdp.txt`` sections 13-15 and 17 (research, but the window bug and the invalid
  scroll modes are his own hardware tests, and his capture photos in
  ``md_tech.zip`` show the bug).
- **Priority**: CMD ``genvdp.txt`` section 14 (backdrop < B low < A low < sprite low
  < B high < A high < sprite high).
- **Shadow/highlight**: Kabuto's "S/H" (hardware-measured research), which corrects
  the Manual and CMD's ``ste.txt``: three steps, in order, over the sprite pixel and
  the two planes' priority bits. His debug-register mixer
  ``(A|!a)&(B|!b)&(S|!s)&(G|!g)`` is the shape of :meth:`_debug_pixel`.
- **The colour ramp**: Plutiedev ``vdp-color-ramp`` (measured DAC levels; the ramp is
  not linear and shadow and highlight are not exact halves and doubles).
- **Sprites**: Kabuto's "Precise details of sprite rendering" (the cache, the phases,
  the 20/16 slots, the 40/32 tile budget, the X = 0 rule and its carry from line to
  line) and Nemesis's rules in forum t=541 start=30, which his sprite masking ROM
  checks on an MD1600.

What this module does *not* model, and why (docs/worklog.md, milestones 3 and 5): the
sprite phases are timed to the line, not the slot -- phases 1 and 2 run when the line
before begins and phase 3 when the line begins (docs/undocumented-behavior.md, VDP
item 25), so a sprite table changed part way through the line before takes effect
at the wrong point in it; a mid-line change lands at the access slot that
carried it, not at the pixel the VDP would have been fetching for (the plane fetches
run several pixels ahead of the pixels they feed); and with the display disabled the
line shows the backdrop colour rather than the bus garbage Kabuto describes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vdp import VDP

#: How many pixels the picture's output runs behind the H counter, by mode: a change
#: that acts where the colour is looked up (a CRAM write, the backdrop index in
#: register 7) shows that many pixels left of the pixel the H counter names when it
#: lands. Nuked-MD shows a CRAM write committed at its H counter h from output pixel
#: h - 24 in both modes, and its H40 slots are numbered three above the board's
#: (the board's $042 is its $045); so 21 in H40 and 24 in H32, which puts H40's slot
#: $012 at X = -3 -- Kabuto's figure for it ("Access timing"), a hardware
#: measurement the die model agrees with. docs/worklog.md, "Where a mid-line write
#: shows". Changes that act where tiles are fetched are not delayed: not measured.
OUTPUT_DELAY = {"H40": 21, "H32": 24}

#: When each plane's tiles are fetched, in the board's pixel time (its H counter, the
#: display starting at 0), relative to the start 16g of the 2-cell group g being
#: fetched: (the group's name, its first cell's pattern row, its second's), planes A
#: and B. Read off Nuked-MD's own VRAM accesses (``validation/nuked_md_headless.c``,
#: ``NMD_VRAMSIG``), which in die H are 16g + 3, 7, 9 for plane A and 11, 15, 17 for
#: plane B in both modes; the die's H40 counter runs three above the board's
#: (docs/undocumented-behavior.md VDP item 20). Group g shows the screen pixels from
#: 16g plus the plane's fine scroll; group -1, fetched in the blank, is the partial
#: column that fine scroll brings in. docs/worklog.md, "Where tiles are fetched".
FETCH_AT = {"H40": ((0, 4, 6), (8, 12, 14)), "H32": ((3, 7, 9), (11, 15, 17))}

#: The VDP's measured output levels, 0-255, for the 15 steps of its DAC ramp
#: (Plutiedev ``vdp-color-ramp``: "In a scale from 0 to 255, the approximate values
#: are: 0, 29, 52, 70, 87, 101, 116, 130, 144, 158, 172, 187, 206, 228, 255. The normal
#: colors are every other value, the shadow colors the first eight values, and the
#: highlight colors the last eight values.")
RAMP = (0, 29, 52, 70, 87, 101, 116, 130, 144, 158, 172, 187, 206, 228, 255)
LEVELS = (RAMP[0:15:2], RAMP[0:8], RAMP[7:15])  # normal, shadow, highlight

#: A pixel of a rendered line: the CRAM index in bits 5-0, the shade in bits 7-6
#: (0 normal, 1 shadow, 2 highlight).
SHADE_SHIFT = 6

#: Sprites examined per frame, sprites per line, and sprite tiles per line, by display
#: width (Nemesis, forum t=541 start=30, checked on an MD1600; Kabuto, phases 1 and 3).
SPRITE_LIMITS = {256: (64, 16, 32), 320: (80, 20, 40)}

#: A plane's size by the two bits of register 16 (00 = 32 cells, 01 = 64, 10 invalid,
#: 11 = 128; CMD ``genvdp.txt``, register $10), as the VDP addresses it:
#:
#: - ``PLANE_CELLS``: cells across, which is the column number's wrap.
#: - ``PLANE_ROW_SHIFT``: how far the tile row number is shifted to give the name
#:   table row's offset in bytes. The invalid width shifts by nothing, so with the
#:   0x1FC0 mask every line of a screen takes its names from the table's first row --
#:   "If the HSZ bits are set to 10b (invalid), then the first row of the name table is
#:   shown for every line of the display" (CMD).
#: - ``PLANE_ROW_MASK``: the vertical wrap, in pixels and applied as a mask, so that
#:   the invalid height (0x2FF) wraps as oddly as the hardware does.
#:
#: The shift and the masks are Genesis Plus GX's tables (``core/vdp_ctrl.c``
#: ``shift_table``/``col_mask_table``/``row_mask_table``; emulator-derived, read and
#: rewritten, never copied), which is the only statement of the invalid sizes' shape
#: beyond CMD's sentence; CMD's sentence and this agree where they overlap.
#: Decoded tile rows kept before the cache is emptied (bounded memory in a long run).
TILE_CACHE_LIMIT = 100_000

PLANE_CELLS = (32, 64, 32, 128)
PLANE_ROW_SHIFT = (6, 7, 0, 8)
PLANE_ROW_MASK = (0x0FF, 0x1FF, 0x2FF, 0x3FF)


class Renderer:
    """The picture of one :class:`~megadrive_python.vdp.VDP`."""

    def __init__(self, vdp: VDP) -> None:
        self.vdp = vdp
        #: The frame being drawn: ``pixels[row][x]`` is a CRAM index and a shade
        #: (:data:`SHADE_SHIFT`). 480 rows are kept so that double interlace fits.
        self.pixels = [bytearray(320) for _ in range(480)]
        #: The picture's size once a frame is done, and the width each row was drawn
        #: at: a game that switches H32 and H40 part way down a frame (CMD photographed
        #: one) draws rows of both widths, and the frame is as wide as its widest.
        self.width = 320
        self.widths = [0] * 480
        self.height = 0
        self.rows = 0
        #: The finished picture, swapped in by :meth:`start_frame` so that drawing the
        #: next frame cannot overwrite the one a caller is about to read. ``height``
        #: and ``done_width`` above describe it; the arrays parallel the ones being
        #: drawn into, and the two sets exchange places once a frame.
        self.done_width = 320
        self._done_pixels = [bytearray(320) for _ in range(480)]
        self._done_widths = [0] * 480
        #: Sprite state that outlives a line (Kabuto, phase 3): whether the sprite read
        #: last had X != 0, which is what makes an X = 0 sprite mask the rest of a line.
        self.last_x_nonzero = False
        #: Set when a line runs out of sprite slots or of sprite tiles, and when two
        #: sprite pixels overlap: status bits 6 and 5, cleared when the status is read.
        self.overflow = False
        self.collision = False
        #: Decoded tile rows, keyed by the four pattern bytes and the attribute: the key
        #: holds the data, so an entry can never go stale when VRAM changes. A game
        #: settles at a few thousand of them (Sonic: 12,208 after a minute); the cache
        #: is emptied if one ever churns through :data:`TILE_CACHE_LIMIT`.
        self._tiles: dict[int, tuple] = {}
        #: The palettes each row was drawn with (normal, shadow and highlight RGB for
        #: every CRAM entry), as ``(first pixel, palette)`` in order: a game that
        #: rewrites CRAM part way down the frame -- as CMD's shadow/highlight tests do,
        #: a band at a time -- gets a row of its own, and one that rewrites it part way
        #: *along* a line gets an entry from that pixel on.
        self._palettes: list[list[tuple[int, tuple]]] = [[] for _ in range(480)]
        self._done_palettes: list[list[tuple[int, tuple]]] = [[] for _ in range(480)]
        self._palette: tuple = ()
        #: Set by the VDP on a CRAM write: the next pixels drawn take a fresh palette.
        self.cram_dirty = True
        #: CRAM dots: ``(pixel, RGB)`` per row, from a CRAM write during the display.
        self._dots: list[list[tuple[int, int]]] = [[] for _ in range(480)]
        self._done_dots: list[list[tuple[int, int]]] = [[] for _ in range(480)]
        #: Rows drawn with register $00 bit 0 set, which "actually turns off all
        #: display generation, as opposed to the screen blanking feature which simply
        #: shows the backdrop color" (CMD ``genvdp.txt`` 17). Such a row leaves the
        #: picture black whatever CRAM holds; it is kept per row rather than per pixel
        #: because there is no CRAM entry that means "nothing".
        self._blanked = bytearray(480)
        self._done_blanked = bytearray(480)
        #: The line being drawn (``begin_line``): where it starts in master clocks, how
        #: much of it has been drawn, and everything its remaining pixels need.
        self.pending = False
        self._line_start = -1
        self._drawn = 0
        self._row = 0
        self._width = 320
        self._backdrop = 0
        self._out = self.pixels[0]
        self._line_a: list[int] = []
        self._line_b: list[int] = []
        self._line_s: list[int] = []
        self._spans: tuple = ()
        #: The sprite cache: the Y and size/link words of each of the 80 sprites,
        #: written through by VRAM writes that land in the sprite table (Kabuto,
        #: "General"). Moving the table does not fill it, as on the hardware.
        self.sprite_cache = bytearray(80 * 4)
        #: What the line before scanned for the next line (:meth:`_sprite_scan`), as
        #: ``((row, width, double interlace), latched sprites)``, or None.
        self._scanned: tuple | None = None

    # -- the sprite cache ---------------------------------------------------------

    def sprite_cache_write(self, address: int, value: int) -> None:
        """A VRAM byte write at ``address`` (17 bits): update the cache if it lands in
        the first four bytes of a sprite table entry.

        "the moment a write to VRAM occurs the VDP checks whether it goes to the
        current location of the sprite table, and if it does and affects an Y or
        size/link field then the respective cache table entry is updated ... this
        mechanism is sensitive to the entire 128 KB address range even in 64 KB mode"
        (Kabuto). The table's base is register 5 shifted left 9, with bit 9 ignored in
        H40 (the Manual; Nemesis's per-frame limits test needs the H32 bit).
        """
        base = (self.vdp.registers[5] << 9) & 0x1FFFF
        if self.vdp.hmode.name == "H40":
            base &= ~0x200
        offset = address - base
        if 0 <= offset < 80 * 8 and not offset & 4:
            self.sprite_cache[(offset >> 3) * 4 + (offset & 3)] = value

    # -- tiles --------------------------------------------------------------------

    def _tile_row(self, address: int, attribute: int, flip: bool) -> tuple:
        """One pattern row as eight pixels: the colour in bits 3-0, the tile's palette
        in bits 5-4 and its priority in bit 6 -- a transparent pixel (colour 0) keeps
        the palette and priority too. Opacity is the colour bits alone everywhere these
        are used. The priority of a transparent plane pixel still decides shadow ("If
        both A+B are low prio, the visible color becomes shadowed", Kabuto, "S/H";
        "sprite darkness depends on plane tile priority, even when all plane tile
        pixels are transparent"), and its palette shows where the debug register
        forces the plane on. BlastEm's ``render_map`` keeps both the same way."""
        vram = self.vdp.vram
        data = (
            (vram[address] << 24)
            | (vram[address + 1] << 16)
            | (vram[address + 2] << 8)
            | vram[address + 3]
        )
        key = (data << 8) | attribute | (0x80 if flip else 0)
        row = self._tiles.get(key)
        if row is None:
            pixels = [
                (data >> 28) & 15, (data >> 24) & 15, (data >> 20) & 15, (data >> 16) & 15,
                (data >> 12) & 15, (data >> 8) & 15, (data >> 4) & 15, data & 15,
            ]  # fmt: skip
            if flip:
                pixels.reverse()
            row = tuple(attribute | pixel for pixel in pixels)
            if len(self._tiles) >= TILE_CACHE_LIMIT:
                self._tiles.clear()
            self._tiles[key] = row
        return row

    # -- the planes ---------------------------------------------------------------

    def _plane_columns(self, line: list[int], base: int, row: int, size: int, scroll: int,
                       im2: bool, window_bug: int, first: int, last: int) -> None:  # fmt: skip
        """One scrolling plane, over the screen pixels ``first``..``last`` - 1.

        ``row`` is the plane row in pixels before its vertical wrap, ``size`` register
        16, ``scroll`` the plane's horizontal scroll (the plane moves right by it), and
        ``window_bug`` the screen pixel where CMD's window bug starts (two cells
        fetched from two cells further on), or -1.
        """
        vram = self.vdp.vram
        tile_rows = 16 if im2 else 8
        pattern_mask, pattern_bytes = (0x3FF, 64) if im2 else (0x7FF, 32)
        row_mask = PLANE_ROW_MASK[(size >> 4) & 3]
        row &= (row_mask << 1) | 1 if im2 else row_mask
        name_row = base + (((row // tile_rows) << PLANE_ROW_SHIFT[size & 3]) & 0x1FC0)
        pattern_row = row & (tile_rows - 1)
        column_mask = PLANE_CELLS[size & 3] - 1
        x = first - ((first - scroll) & 7)
        while x < last:
            cell = ((x - scroll) >> 3) & column_mask
            if window_bug >= 0 and 0 <= x - window_bug < 16:
                cell = (cell + 2) & column_mask
            entry = (name_row + cell * 2) & 0xFFFF
            name = (vram[entry] << 8) | vram[entry + 1]
            pattern_line = (tile_rows - 1 - pattern_row) if name & 0x1000 else pattern_row
            address = ((name & pattern_mask) * pattern_bytes + pattern_line * 4) & 0xFFFF
            row_pixels = self._tile_row(address, (name >> 9) & 0x70, bool(name & 0x0800))
            if first <= x and x + 8 <= last:
                line[x : x + 8] = row_pixels
            else:
                for i in range(8):
                    at = x + i
                    if first <= at < last:
                        line[at] = row_pixels[i]
            x += 8

    def _window_line(self, line: list[int], width: int, row: int, first: int, last: int,
                     im2: bool, left: int = 0, right: int = 1 << 30) -> None:  # fmt: skip
        """The window's cells ``first``..``last``, unscrolled, clipped to the screen
        pixels ``left``..``right`` - 1.

        Its name table is 32 cells wide in H32 and 64 in H40 ("The name table for plane
        W is 32x32 in 32-cell mode, and 64x32 in 40-cell mode", CMD ``genvdp.txt``).
        Register 3's "Bits 5-1 ... correspond to bits A15-A11 ... In 40-cell mode, A11
        is always forced to zero" (CMD), so the base is masked to 0xF800, or 0xF000 in
        H40 (the same masks as Genesis Plus GX's ``ntwb``).
        """
        vram = self.vdp.vram
        columns = 64 if width == 320 else 32
        base = (self.vdp.registers[3] << 10) & (0xF000 if width == 320 else 0xF800)
        tile_rows = 16 if im2 else 8
        pattern_mask, pattern_bytes = (0x3FF, 64) if im2 else (0x7FF, 32)
        name_row = base + (row // tile_rows) * columns * 2
        pattern_row = row & (tile_rows - 1)
        for cell in range(max(first, left >> 3), min(last, (right - 1) >> 3) + 1):
            entry = (name_row + cell * 2) & 0xFFFF
            name = (vram[entry] << 8) | vram[entry + 1]
            pattern_line = (tile_rows - 1 - pattern_row) if name & 0x1000 else pattern_row
            address = ((name & pattern_mask) * pattern_bytes + pattern_line * 4) & 0xFFFF
            x = cell * 8
            line[x : x + 8] = self._tile_row(address, (name >> 9) & 0x70, bool(name & 0x0800))

    def _window_columns(self, cells: int, v: int) -> tuple[int, int]:
        """The first and last cell the window covers on line ``v`` (CMD ``genvdp.txt``,
        registers $11 and $12); ``(0, -1)`` when it covers nothing."""
        registers = self.vdp.registers
        vertical = registers[18] & 0x1F
        line_cell = v >> 3
        if (line_cell >= vertical) if registers[18] & 0x80 else (line_cell < vertical):
            return 0, cells - 1
        horizontal = (registers[17] & 0x1F) * 2
        if registers[17] & 0x80:
            return (horizontal, cells - 1) if horizontal < cells else (0, -1)
        return (0, min(horizontal, cells) - 1) if horizontal else (0, -1)

    def _plane_setup(self, width: int, v: int, screen_row: int, im2: bool) -> tuple:
        """What both planes and the window need for the whole of line ``v``, read where
        the hardware reads it -- at the line's start.

        The horizontal scroll entry is fetched once a line (Kabuto's "H" access is the
        first of the line), and so are the window's columns, so a mid-line write to
        either does not move the line being drawn.
        """
        vdp = self.vdp
        registers = vdp.registers
        vram = vdp.vram
        cells = width >> 3
        # Horizontal scroll: full screen, the first eight entries, every cell, or every
        # line (CMD ``genvdp.txt``, register $0B bits 1-0, with his pseudocode; mode 01
        # "will only read the first sixteen entries in the scroll table").
        mask = (0, 7, 0xFFF8, 0xFFFF)[registers[11] & 3]
        # Register 13 bits 5-0 are A15-A10 of the scroll table (CMD ``genvdp.txt``).
        table = (registers[13] << 10) & 0xFC00
        # The scroll table is read with the V counter's line, not the doubled one, in
        # interlace mode 2 (Genesis Plus GX ``render_bg_m5_im2``, which computes the
        # index before doubling; Kabuto says only "the lowmost 8 bits of the raster
        # line are used for HScroll lookup").
        entry = (table + (v & mask) * 4) & 0xFFFF
        scroll_a = ((vram[entry] << 8) | vram[entry + 1]) & 0x3FF
        scroll_b = ((vram[entry + 2] << 8) | vram[entry + 3]) & 0x3FF
        window_first, window_last = self._window_columns(cells, v)

        window_bug = -1
        if window_first == 0 and 0 <= window_last < cells - 1 and scroll_a & 0x0F:
            # "There is a bug in the window processing. This occurs when the window is
            # showing partially on the left side of the screen, specifically when the
            # VDP is drawing the 2-cell column from plane A that immediately proceeds
            # the last column the window was drawn on ... If the lower four bits of the
            # horizontal scroll value for the current scan line are nonzero, the name
            # table attribute data are fetched from next column" (CMD ``genvdp.txt``;
            # his capture photos ``cv-win-00.gif`` and ``cv-win-01.gif``).
            #
            # The column it hits is the *partial* one the fine scroll puts before plane
            # A's first whole 2-cell column, so its 16 pixels end at the window's edge
            # plus the fine scroll; its two cells then repeat in the column after it,
            # which is CMD's "1 column that has identical patterns as the next column".
            window_bug = (window_last + 1) * 8 - 16 + (scroll_a & 0x0F)
        return (scroll_a, scroll_b, window_first, window_last, window_bug,
                screen_row, im2, width)  # fmt: skip

    def _plane_span(self, first: int, last: int, planes: tuple[int, ...] = (0, 1)) -> None:
        """Fetch planes A (with the window over it) and B over the screen pixels
        ``first``..``last`` - 1, reading VRAM, VSRAM and the registers as they stand."""
        (scroll_a, scroll_b, window_first, window_last, window_bug,
         screen_row, im2, width) = self._spans  # fmt: skip
        vdp = self.vdp
        registers = vdp.registers
        size = registers[16]
        two_cell = bool(registers[11] & 4)
        words = vdp.vsram_words
        # Register 2 bits 5-3 are plane A's A15-A13 and register 4 bits 2-0 plane B's
        # (CMD ``genvdp.txt``), so both bases mask to 0xE000.
        for line, base, scroll, plane in (
            (self._line_a, (registers[2] << 10) & 0xE000, scroll_a, 0),
            (self._line_b, (registers[4] << 13) & 0xE000, scroll_b, 1),
        ):
            if plane not in planes:
                continue
            # The window bug is plane A's alone (CMD: "the VDP is drawing the 2-cell
            # column from plane A"; ``_window_columns``'s docstring); plane B has no
            # window over it and must fetch its own columns undisturbed.
            plane_window_bug = window_bug if plane == 0 else -1
            if two_cell:
                # Two-cell column vertical scroll: each 16-pixel column of the display
                # has its own VSRAM entry (CMD ``genvdp.txt``, register $0B bit 2).
                x = first & ~15
                while x < last:
                    index = (x >> 4) * 2 + plane
                    vscroll = vdp.column_scroll(index) if index < words else 0
                    self._plane_columns(line, base, screen_row + vscroll, size, scroll,
                                        im2, plane_window_bug, max(x, first),
                                        min(x + 16, last))  # fmt: skip
                    x += 16
            else:
                # Full-screen: the value latched at the end of the line before, not
                # VSRAM as it stands (:attr:`VDP.vscroll_latch`).
                self._plane_columns(line, base, screen_row + vdp.vscroll_latch[plane],
                                    size, scroll, im2, plane_window_bug, first,
                                    last)  # fmt: skip
        if window_last >= window_first and 0 in planes:
            # "Plane A is not shown in any column where plane W is shown" (CMD).
            self._window_line(self._line_a, width, screen_row, window_first, window_last,
                              im2, first, last)  # fmt: skip

    # -- sprites ------------------------------------------------------------------

    def _sprites(self, line: list[int], width: int, screen_row: int, im2: bool) -> None:
        """The sprite line buffer, Kabuto's phases 1 to 3 run together: what a line
        gets when there was no line before it to scan for it (:meth:`begin_line`)."""
        self._sprite_draw(line, width, self._sprite_scan(width, screen_row, im2), im2)

    def _sprite_scan(self, width: int, screen_row: int, im2: bool) -> list[tuple]:
        """Phases 1 and 2 for display row ``screen_row``, which the hardware runs a line
        ahead of it (Kabuto: phase 1 "2 H-borders before the raster line to be
        rendered", phase 2 "1 raster line before").

        Phase 1 walks the cached linked list and keeps the first 20 (16) sprites whose
        rows cover the line; phase 2 reads each one's X and pattern from the table at
        register 5 as it stands, and takes Y and size from the cache again to work out
        the pattern row. What phase 2 latched is returned for :meth:`_sprite_draw`, so
        a table moved or rewritten after the scan does not reach this line.
        """
        vdp = self.vdp
        vram = vdp.vram
        cache = self.sprite_cache
        max_sprites, max_line, _ = SPRITE_LIMITS[width]
        tile_rows = 16 if im2 else 8
        start_v = 0x100 if im2 else 0x80
        mask_v = 0x3FF if im2 else 0x1FF

        visible: list[int] = []
        index = 0
        for _ in range(max_sprites):
            entry = index * 4
            y = (((cache[entry] << 8) | cache[entry + 1]) & mask_v) - start_v
            if y <= screen_row < y + ((cache[entry + 2] & 3) + 1) * tile_rows:
                if len(visible) < max_line:
                    visible.append(index)
                else:
                    self.overflow = True  # more sprites on this line than there are slots
            link = cache[entry + 3] & 0x7F
            if link == 0 or link >= max_sprites:
                # "a link attribute of 0 makes it stop scanning the list further"
                # (Kabuto); an entry past the per-frame limit "will not be processed and
                # the sprite list will terminate" (Nemesis, t=541 start=30, test 9).
                break
            index = link

        base = (vdp.registers[5] << 9) & 0xFFFF
        if width == 320:
            base &= ~0x200
        pattern_mask = 0x3FF if im2 else 0x7FF
        latched: list[tuple] = []
        for sprite in visible:
            entry = (base + sprite * 8) & 0xFFFF
            cached = sprite * 4
            y = (((cache[cached] << 8) | cache[cached + 1]) & mask_v) - start_v
            size = cache[cached + 2]
            cells_x = ((size >> 2) & 3) + 1
            cells_y = (size & 3) + 1
            name = (vram[entry + 4] << 8) | vram[entry + 5]
            x = (((vram[entry + 6] << 8) | vram[entry + 7]) & 0x1FF) - 0x80
            # "The VDP just considers the lowermost 5 bits of the difference of sprite Y
            # coordinate and raster line" (Kabuto, phase 2), doubled in interlace mode 2.
            row = (screen_row - y) & (0x3F if im2 else 0x1F)
            if name & 0x1000:
                row = cells_y * tile_rows - 1 - row
            attribute, flip_x = (name >> 9) & 0x70, bool(name & 0x0800)
            latched.append((x, attribute, flip_x, cells_x, cells_y, name & pattern_mask,
                            row // tile_rows, row & (tile_rows - 1)))  # fmt: skip
        return latched

    def _sprite_draw(self, line: list[int], width: int, latched: list[tuple], im2: bool) -> None:
        """Phase 3: fetch the latched sprites' tiles and render them into the line
        buffer, 40 (32) tiles at most, earlier sprites winning every pixel, with the
        X = 0 rule masking the rest of the line."""
        pattern_bytes = 64 if im2 else 32
        tiles_left = SPRITE_LIMITS[width][2]
        rendering = True
        last_x_nonzero = self.last_x_nonzero
        for x, attribute, flip_x, cells_x, cells_y, pattern, cell_row, pattern_line in latched:
            if x == -0x80:
                # "Rendering of further sprite pixels is disabled whenever a sprite is
                # read from a cache slot with X=0 when the previous sprite read ... had
                # X != 0" (Kabuto, phase 3); X = 0 is screen -128 here.
                if last_x_nonzero:
                    rendering = False
                last_x_nonzero = False
            else:
                last_x_nonzero = True
            out_of_tiles = False
            for cell in range(cells_x):
                if tiles_left == 0:
                    out_of_tiles = True  # the sprite is cut off partway: the dots ran out
                    break
                tiles_left -= 1
                if not rendering:
                    continue
                column = (cells_x - 1 - cell) if flip_x else cell
                tile = pattern + column * cells_y + cell_row
                address = (tile * pattern_bytes + pattern_line * 4) & 0xFFFF
                left = x + cell * 8
                if left <= -8 or left >= width:
                    continue
                row_pixels = self._tile_row(address, attribute, flip_x)
                for i in range(8):
                    # A transparent pixel still carries its sprite's palette and
                    # priority, and any pixel whose colour bits are 0 is overwritten:
                    # "Only transparent pixels (i.e. the 4 bits that come from tile
                    # data are 0; palette and priority are ignored for this check)
                    # will get overwritten with the 7 bits from the current sprite"
                    # (Kabuto, phase 3). It shows only where the debug register forces
                    # the sprite plane on: palette entry 0 instead of the backdrop.
                    pixel = row_pixels[i]
                    at = left + i
                    if 0 <= at < width:
                        if not line[at] & 15:
                            line[at] = pixel
                        elif pixel & 15:
                            self.collision = True
            if out_of_tiles:
                break
        if tiles_left:
            # "When there are less than 40 sprite tiles on a raster line then further
            # dummy sprites act as if they had X=0" (Kabuto): the line ends on an X = 0
            # read, so a mask in the next line's first slot does nothing.
            last_x_nonzero = False
        else:
            self.overflow = True
        self.last_x_nonzero = last_x_nonzero

    # -- one line -----------------------------------------------------------------

    def begin_line(self, v: int) -> None:
        """Start display line ``v``, the V counter's value for it: finish the line
        before it, then read everything this one needs for its whole length."""
        self.finish_line()
        vdp = self.vdp
        width = 320 if vdp.hmode.name == "H40" else 256
        im2 = vdp.interlace_double
        row = ((v << 1) | (1 if vdp.odd else 0)) if im2 else v
        # The sprites of the line after this one are scanned now (phases 1 and 2). A
        # line with its display off scans nothing ahead, and the line after it is then
        # scanned when it begins, as every line was before the scan moved: Kabuto's
        # "No sprites are checked while display is disabled" (phase 1) would leave it
        # without sprites, and that is not measured here.
        scanned, self._scanned = self._scanned, None
        after = (v + 1) & 0x1FF
        after_row = ((after << 1) | (1 if vdp.odd else 0)) if im2 else after
        if row >= 480:
            # The line before the first (V = 511) is not shown, but its sprite scan is
            # the first line's (Kabuto, "Topmost and bottommost raster line").
            if vdp.display_enabled and after_row < 480:
                self._scan_ahead(after_row, im2)
            return
        out = self.pixels[row]
        if self.cram_dirty or not self._palette:
            self._palette = self._make_palette()
            self.cram_dirty = False
        self._palettes[row] = [(0, self._palette)]
        if self._dots[row]:
            self._dots[row] = []
        self.width = width
        if row + 1 > self.rows:
            self.rows = row + 1
        backdrop = vdp.registers[7] & 0x3F
        self.widths[row] = width
        self._blanked[row] = vdp.registers[0] & 0x01
        if width < 320:
            for x in range(width, 320):
                out[x] = backdrop
        if not vdp.display_enabled:
            # The backdrop colour. On the hardware the planes keep showing whatever is
            # on the bus (Kabuto, "Effects of 'disable display'"), which is not modelled.
            for x in range(width):
                out[x] = backdrop
            self.last_x_nonzero = False  # no sprites are fetched with the display off
            return
        self._line_a = [0] * width
        self._line_b = [0] * width
        self._line_s = [0] * width
        # The sprites of a line are fetched and drawn into the line buffer during the
        # horizontal blank before it (Kabuto, phase 3), so a mid-line write cannot
        # change them: they are done once, here, from what the line before scanned. A
        # line nothing scanned for (the display was off, or the mode changed) is
        # scanned now, as the board did for every line before the scan moved.
        if scanned is not None and scanned[0] == (row, width, im2):
            self._sprite_draw(self._line_s, width, scanned[1], im2)
        else:
            self._sprites(self._line_s, width, row, im2)
        if after_row < 480:
            self._scan_ahead(after_row, im2)
        self._spans = self._plane_setup(width, v, row, im2)
        #: How far each plane has been fetched, and each plane's fine scroll, which
        #: places its 2-cell groups on the screen (``FETCH_AT``).
        self._fetched = [0, 0]
        self._fine = (self._spans[0] & 15, self._spans[1] & 15)
        self._out = out
        self._row = row
        self._width = width
        self._backdrop = backdrop
        self._drawn = 0
        self._line_start = vdp.line_start
        self.pending = True

    def _scan_ahead(self, row: int, im2: bool) -> None:
        """Run phases 1 and 2 for display row ``row``, the next line's, and keep them."""
        width = 320 if self.vdp.hmode.name == "H40" else 256
        self._scanned = ((row, width, im2), self._sprite_scan(width, row, im2))

    def catch_up(self, offset: int, names: bool = False, limit: int | None = None) -> None:
        """Bring the line under way up to ``offset`` master clocks into it, before
        something it is drawn from changes (:mod:`megadrive_python.vdp`, ``_slot`` and
        ``_write_register``).

        Two stages, as on the chip. Every tile whose fetch time (``FETCH_AT``) has come
        is fetched first, from the state before the change -- with ``names``, for a
        change to a name table or to where the planes are, every tile whose *name* has
        been fetched, since its pattern is then read through the old name. Then the
        output is mixed up to the pixel the H counter names less ``OUTPUT_DELAY``,
        where the colour output has got to. ``limit``: a pixel not to fetch past yet
        (a 2-cell column still to be latched).
        """
        if not self.pending or self.vdp.line_start != self._line_start:
            return
        mode = self.vdp.hmode
        t = mode.position(offset) - mode.display_at
        self._fetch_until(t, names, limit)
        self._draw_to(t - OUTPUT_DELAY[mode.name])

    def shows_window(self) -> bool:
        """Whether the line under way shows the window plane anywhere."""
        return bool(self.pending and self._spans and self._spans[3] >= self._spans[2])

    def fetch_to(self, plane: int, x: int) -> None:
        """Fetch ``plane`` over the screen pixels up to ``x``, from the state as it
        stands."""
        x = min(x, self._width)
        if x > self._fetched[plane]:
            self._plane_span(self._fetched[plane], x, (plane,))
            self._fetched[plane] = x

    def _fetch_until(self, t: int, names: bool, limit: int | None) -> None:
        """Fetch every cell of both planes whose fetch time is at or before ``t``."""
        offsets = FETCH_AT[self.vdp.hmode.name]
        for plane in (0, 1):
            name_at, even_at, odd_at = offsets[plane]
            fine = self._fine[plane]
            # Cells in fetch order, from the partial column (group -1) on: cell c
            # starts at screen pixel 8c + fine and belongs to group c >> 1.
            cell = (self._fetched[plane] - fine) // 8
            while True:
                group = cell >> 1
                at = 16 * group + (name_at if names else (odd_at if cell & 1 else even_at))
                start = 8 * cell + fine
                if at > t or start >= self._width:
                    break
                cell += 1
            x = max(0, 8 * cell + fine)
            if limit is not None:
                x = min(x, limit)
            self.fetch_to(plane, x)

    def finish_line(self) -> None:
        """Draw whatever is left of the line under way."""
        if self.pending:
            self._draw_to(self._width)
            self.pending = False

    def cram_dot(self, colour: int) -> None:
        """A CRAM write landing at an access slot during the active display shows the
        colour it wrote, for the one pixel of that slot (docs/undocumented-behavior.md,
        "The VDP" item 5; BlastEm ``write_cram``, ``vdp.c`` lines 443-451, writes the
        new colour into the pixel at its own slot). The caller has already drawn the
        line up to that pixel."""
        x = self._drawn
        if self.pending and 0 <= x < self._width:
            levels = LEVELS[0]
            self._dots[self._row].append(
                (x, (levels[(colour >> 1) & 7] << 16)
                    | (levels[(colour >> 5) & 7] << 8)
                    | levels[(colour >> 9) & 7])
            )  # fmt: skip

    def _draw_to(self, x: int) -> None:
        """Draw the line's pixels from where it had got to up to ``x``."""
        first = self._drawn
        last = min(x, self._width)
        if last <= first:
            return
        if not self.vdp.display_enabled:
            # The display was turned off part way along the line: the rest is the
            # backdrop, as the border stage gives (Kabuto, "Effects of 'disable
            # display'"; what the planes really show there is not modelled).
            backdrop = self.vdp.registers[7] & 0x3F
            out = self._out
            for x in range(first, last):
                out[x] = backdrop
            self._drawn = last
            return
        if self.cram_dirty:
            # CRAM changed part way along the line: the rest of it is drawn with the
            # new colours, and the pixels already drawn keep the ones they had.
            self._palette = self._make_palette()
            self.cram_dirty = False
            self._palettes[self._row].append((first, self._palette))
        self.fetch_to(0, last)
        self.fetch_to(1, last)
        # The backdrop index is read as it stands: a mid-line write to register 7
        # changes the rest of the line (Nuked-MD; docs/worklog.md, "Where a mid-line
        # write shows"), where the board used to keep the line's first value.
        self._mix(self._out, self._line_a, self._line_b, self._line_s, first, last,
                  self.vdp.registers[7] & 0x3F)  # fmt: skip
        self._drawn = last

    # -- the mixer ----------------------------------------------------------------

    def _mix(self, out: bytearray, line_a: list[int], line_b: list[int], line_s: list[int],
             first: int, last: int, backdrop: int) -> None:  # fmt: skip
        """Priority and shadow/highlight, pixel by pixel, over ``first``..``last`` - 1.

        Priority (CMD ``genvdp.txt`` 14): backdrop < B low < A low < sprite low <
        B high < A high < sprite high. Shadow/highlight (Kabuto, "S/H"): the three
        steps of his "assumed operation mode", in order.
        """
        shadow_mode = bool(self.vdp.registers[12] & 8)
        debug = self.vdp.debug_register
        force = (debug >> 7) & 3
        blank = bool(debug & 0x40)
        for x in range(first, last):
            av = line_a[x]
            bv = line_b[x]
            sv = line_s[x]
            a_high = av & 0x40
            b_high = bv & 0x40
            s_visible = bool(sv & 15) and (
                sv & 0x40 or (not (av & 15 and a_high) and not (bv & 15 and b_high))
            )
            shade = 0
            if shadow_mode:
                colour = sv & 0x3F
                # 1: a visible sprite pixel of colour 62 or 63 is not drawn and turns
                # the shade on instead.
                if s_visible and colour >= 62:
                    s_visible = False
                    shade = 1 if colour == 63 else 2
                # 2: both planes low priority, the sprite plane inactive or low
                # priority: shadow, and no highlight.
                if not a_high and not b_high and not (s_visible and sv & 0x40):
                    shade = 1
                # 3: a sprite pixel whose lowest four bits are 14 cancels the shadow.
                if shade == 1 and (sv & 0x0F) == 14:
                    shade = 0
            if s_visible:
                value = sv & 0x3F
            elif av & 15 and a_high:
                value = av & 0x3F
            elif bv & 15 and b_high:
                value = bv & 0x3F
            elif av & 15:
                value = av & 0x3F
            elif bv & 15:
                value = bv & 0x3F
            else:
                value = -1  # no plane is showing: the backdrop
            if force or blank:
                value = self._debug_pixel(value, av, bv, sv, s_visible, force, blank, backdrop)
            elif value < 0:
                value = backdrop
            out[x] = value | (shade << SHADE_SHIFT)

    def _debug_pixel(self, value: int, av: int, bv: int, sv: int, s_visible: bool,
                     force: int, blank: bool, backdrop: int) -> int:  # fmt: skip
        """The debug register's plane forcing (Kabuto, "The mighty debug register").

        Bit 6 turns every plane off (so only a forced plane shows); bits 8-7 force one
        plane permanently active (01 sprites, 10 A, 11 B), and where another plane also
        shows, the two are ANDed before the palette lookup, which is his mixer
        ``(A|!a)&(B|!b)&(S|!s)&(G|!g)`` with one plane's flag held at 1.
        """
        forced = (0, sv, av, bv)[force] & 0x3F
        if blank:
            return forced if force else backdrop
        if not force:
            return backdrop if value < 0 else value
        if value < 0:
            return forced  # the backdrop was showing: the forced plane shows instead
        return value & forced

    # -- frames -------------------------------------------------------------------

    def start_frame(self) -> None:
        """A new frame begins: the rows drawn so far become the finished picture.

        The finished rows are handed over by swapping the row arrays rather than
        copied, so a frame costs five reference exchanges and the second set of
        buffers (about 150 KB). Drawing then continues into what the *previous*
        finished picture used, which nothing is reading any more.

        This is what makes :meth:`frame` safe to call at any time. It used to hand
        out ``self.pixels`` directly, and ``System.run_frame`` stops when the VDP's
        frame counter changes, which happens *inside* a 68000 step: a step that holds
        the bus for a whole 68000-memory DMA can carry the VDP tens of lines past the
        boundary. The picture handed out was then the new frame's first few rows --
        320x1, 320x4, 320x55 -- and the finished frame's own rows had already been
        overwritten in place, so it could not be recovered either. Milestone 8's
        sweep scored 17 cartridges ``size`` for exactly that (docs/worklog.md).
        """
        self.finish_line()
        if self.rows:
            self.height = self.rows
            self.done_width = max(self.widths[: self.rows], default=self.width) or self.width
            self.pixels, self._done_pixels = self._done_pixels, self.pixels
            self.widths, self._done_widths = self._done_widths, self.widths
            self._blanked, self._done_blanked = self._done_blanked, self._blanked
            self._palettes, self._done_palettes = self._done_palettes, self._palettes
            self._dots, self._done_dots = self._done_dots, self._dots
        self.rows = 0

    def frame(self) -> tuple[int, int, list[bytearray]]:
        """The picture: its width, its height, and its rows of shaded CRAM indices.

        Always the last *finished* frame, whether or not the next one has begun (see
        :meth:`start_frame`). Before any frame has finished -- only at power-on --
        what has been drawn so far, so that a caller still gets a picture.
        """
        width, height, pixels, _blanked, _palettes, _dots = self._finished()
        return width, height, [pixels[row] for row in range(height)]

    def _finished(self) -> tuple[int, int, list, bytearray, list, list]:
        """The finished frame's size and its four row arrays.

        One place decides between the finished buffers and, before any frame has
        finished, the ones being drawn into, so that :meth:`frame` and
        :meth:`frame_rgb` can never disagree about which picture they are showing.
        """
        if self.height:
            height, width = self.height, self.done_width
            arrays = (self._done_pixels, self._done_blanked, self._done_palettes,
                      self._done_dots)  # fmt: skip
        else:  # power-on: nothing has finished yet, so show what has been drawn
            height = self.rows
            width = max(self.widths[:height], default=self.width) or self.width
            arrays = (self.pixels, self._blanked, self._palettes, self._dots)
        if self.vdp.interlace_double:
            height = (height + 1) & ~1  # both fields make up the picture
        return width, height, *arrays

    def _make_palette(self) -> tuple:
        """CRAM as RGB888: normal, shadowed and highlighted."""
        cram = self.vdp.cram
        return tuple(
            [
                (levels[(colour >> 1) & 7] << 16)
                | (levels[(colour >> 5) & 7] << 8)
                | levels[(colour >> 9) & 7]
                for colour in cram
            ]
            for levels in LEVELS
        )

    def frame_rgb(self) -> list[list[int]]:
        """The picture as rows of RGB888, each pixel through the palette it was drawn
        with: a game that rewrites CRAM part way down a frame (CMD's shadow/highlight
        tests do, a band at a time) keeps each band's colours, and one that rewrites it
        part way along a line keeps each part's. CRAM dots are laid over the result."""
        width, height, pixels, blanked, palettes_of, dots_of = self._finished()
        rows = [pixels[row] for row in range(height)]
        default = [(0, self._palette or self._make_palette())]
        out = []
        for index, row in enumerate(rows):
            if blanked[index]:
                # Register $00 bit 0: no display generation at all, so no colour of
                # CRAM's reaches the screen (CMD ``genvdp.txt`` 17; r57shell's "VDP
                # Blink Test (Reg 0 bit 0)" is what makes it visible).
                out.append([0] * width)
                continue
            palettes = palettes_of[index] or default
            line = [0] * width
            for span, (start, palette) in enumerate(palettes):
                stop = palettes[span + 1][0] if span + 1 < len(palettes) else width
                for x in range(start, min(stop, width)):
                    value = row[x]
                    line[x] = palette[value >> SHADE_SHIFT][value & 0x3F]
            for x, colour in dots_of[index]:
                if x < width:
                    line[x] = colour
            out.append(line)
        return out

"""The VDP's picture: planes A and B, the window, sprites, priority, shadow/highlight.

One line at a time. :meth:`Renderer.render_line` is called by the VDP when a line's
active display begins (its HBlank-clear point), so a raster effect written in that
line's horizontal blank -- where an H interrupt handler runs -- is already in the
registers. What the line needs is then read in one go: the scroll values, the two
planes' name tables and patterns, and the sprites.

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

What this module does *not* model, and why (docs/worklog.md, milestone 3): the sprite
phases are all evaluated when the line is rendered rather than spread over the two
preceding lines, so a sprite table changed between a line's phase 1 and its phase 3
takes effect a line or two early; mid-line register and CRAM writes (CRAM dots) land
at the line's start; and with the display disabled the line shows the backdrop colour
rather than the bus garbage Kabuto describes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .vdp import VDP

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

#: Cells of a plane by the two bits of register 16: 32, 64, invalid, 128. "If the HSZ
#: bits are set to 10b (invalid), then the first row of the name table is shown for
#: every line of the display" (CMD ``genvdp.txt``), which a 32-cell wrap gives here.
PLANE_CELLS = (32, 64, 32, 128)


class Renderer:
    """The picture of one :class:`~megadrive_python.vdp.VDP`."""

    def __init__(self, vdp: VDP) -> None:
        self.vdp = vdp
        #: The frame being drawn: ``pixels[row][x]`` is a CRAM index and a shade
        #: (:data:`SHADE_SHIFT`). 480 rows are kept so that double interlace fits.
        self.pixels = [bytearray(320) for _ in range(480)]
        #: The picture's size once a frame is done.
        self.width = 320
        self.height = 0
        self.rows = 0
        #: Sprite state that outlives a line (Kabuto, phase 3): whether the sprite read
        #: last had X != 0, which is what makes an X = 0 sprite mask the rest of a line.
        self.last_x_nonzero = False
        #: Set when a line runs out of sprite slots or of sprite tiles, and when two
        #: sprite pixels overlap: status bits 6 and 5, cleared when the status is read.
        self.overflow = False
        self.collision = False
        #: Decoded tile rows, keyed by the four pattern bytes and the attribute: the key
        #: holds the data, so an entry can never go stale when VRAM changes.
        self._tiles: dict[int, tuple] = {}
        #: The sprite cache: the Y and size/link words of each of the 80 sprites,
        #: written through by VRAM writes that land in the sprite table (Kabuto,
        #: "General"). Moving the table does not fill it, as on the hardware.
        self.sprite_cache = bytearray(80 * 4)

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
        """One pattern row as eight pixels: the CRAM index of each with the tile's
        priority in bit 6, or 0 where the pattern is transparent."""
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
            row = tuple(attribute | pixel if pixel else 0 for pixel in pixels)
            self._tiles[key] = row
        return row

    # -- the planes ---------------------------------------------------------------

    def _plane_columns(self, line: list[int], base: int, row: int, columns: int, rows: int,
                       scroll: int, im2: bool, window_bug: int, first: int,
                       last: int) -> None:  # fmt: skip
        """One scrolling plane, over the screen pixels ``first``..``last`` - 1.

        ``row`` is the plane row in pixels, ``columns``/``rows`` its size in cells,
        ``scroll`` its horizontal scroll (the plane moves right by it), ``window_bug``
        the screen pixel where CMD's window bug starts (two cells fetched from two
        cells further on), or -1.
        """
        vram = self.vdp.vram
        tile_rows = 16 if im2 else 8
        row %= rows * tile_rows
        name_row = base + (row // tile_rows) * columns * 2
        pattern_row = row & (tile_rows - 1)
        column_mask = columns - 1
        x = first - ((first - scroll) & 7)
        while x < last:
            cell = ((x - scroll) >> 3) & column_mask
            if window_bug >= 0 and 0 <= x - window_bug < 16:
                cell = (cell + 2) & column_mask
            entry = (name_row + cell * 2) & 0xFFFF
            name = (vram[entry] << 8) | vram[entry + 1]
            pattern_line = (tile_rows - 1 - pattern_row) if name & 0x1000 else pattern_row
            pattern = (name & 0x7FE) if im2 else (name & 0x7FF)
            address = (pattern * 32 + pattern_line * 4) & 0xFFFF
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
                     im2: bool) -> None:  # fmt: skip
        """The window's cells ``first``..``last``, unscrolled.

        Its name table is 32 cells wide in H32 and 64 in H40 ("The name table for plane
        W is 32x32 in 32-cell mode, and 64x32 in 40-cell mode", CMD ``genvdp.txt``),
        and in H40 bit 10 of the base address is ignored (the Manual).
        """
        vram = self.vdp.vram
        columns = 64 if width == 320 else 32
        base = (self.vdp.registers[3] << 10) & 0xFFFF
        if width == 320:
            base &= ~0x400
        tile_rows = 16 if im2 else 8
        name_row = base + (row // tile_rows) * columns * 2
        pattern_row = row & (tile_rows - 1)
        for cell in range(first, last + 1):
            entry = (name_row + cell * 2) & 0xFFFF
            name = (vram[entry] << 8) | vram[entry + 1]
            pattern_line = (tile_rows - 1 - pattern_row) if name & 0x1000 else pattern_row
            pattern = (name & 0x7FE) if im2 else (name & 0x7FF)
            address = (pattern * 32 + pattern_line * 4) & 0xFFFF
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

    def _planes(self, line_a: list[int], line_b: list[int], width: int, v: int,
                screen_row: int, im2: bool) -> tuple[int, int]:  # fmt: skip
        """Draw planes A (with the window over it) and B; return the window's cells."""
        vdp = self.vdp
        registers = vdp.registers
        vram = vdp.vram
        cells = width >> 3
        # Horizontal scroll: full screen, the first eight entries, every cell, or every
        # line (CMD ``genvdp.txt``, register $0B bits 1-0, with his pseudocode; mode 01
        # "will only read the first sixteen entries in the scroll table").
        mask = (0, 7, 0xFFF8, 0xFFFF)[registers[11] & 3]
        table = (registers[13] << 10) & 0xFFFF
        entry = (table + (screen_row & mask) * 4) & 0xFFFF
        scroll_a = ((vram[entry] << 8) | vram[entry + 1]) & 0x3FF
        scroll_b = ((vram[entry + 2] << 8) | vram[entry + 3]) & 0x3FF
        columns = PLANE_CELLS[registers[16] & 3]
        rows = PLANE_CELLS[(registers[16] >> 4) & 3]
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
            window_bug = (window_last + 1) * 8

        two_cell = bool(registers[11] & 4)
        vsram = vdp.vsram
        words = vdp.vsram_words
        for line, base, scroll, plane in (
            (line_a, (registers[2] << 10) & 0xFFFF, scroll_a, 0),
            (line_b, (registers[4] << 13) & 0xFFFF, scroll_b, 1),
        ):
            if two_cell:
                # Two-cell column vertical scroll: each 16-pixel column of the display
                # has its own VSRAM entry (CMD ``genvdp.txt``, register $0B bit 2).
                x = 0
                while x < width:
                    index = (x >> 4) * 2 + plane
                    vscroll = vsram[index] if index < words else 0
                    self._plane_columns(line, base, screen_row + vscroll, columns, rows,
                                        scroll, im2, window_bug, x, min(x + 16, width))  # fmt: skip
                    x += 16
            else:
                self._plane_columns(line, base, screen_row + vsram[plane], columns, rows,
                                    scroll, im2, window_bug, 0, width)  # fmt: skip
        if window_last >= window_first:
            # "Plane A is not shown in any column where plane W is shown" (CMD).
            self._window_line(line_a, width, screen_row, window_first, window_last, im2)
        return window_first, window_last

    # -- sprites ------------------------------------------------------------------

    def _sprites(self, line: list[int], width: int, screen_row: int, im2: bool) -> None:
        """The sprite line buffer: Kabuto's phases 1 to 3, run together.

        Phase 1 walks the cached linked list and keeps the first 20 (16) sprites whose
        rows cover this line; phase 2 reads each one's X and pattern from the table;
        phase 3 renders them into the line buffer, 40 (32) tiles at most, earlier
        sprites winning every pixel, with the X = 0 rule masking the rest of the line.
        """
        vdp = self.vdp
        vram = vdp.vram
        cache = self.sprite_cache
        max_sprites, max_line, max_tiles = SPRITE_LIMITS[width]
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
        step = 2 if im2 else 1
        tiles_left = max_tiles
        rendering = True
        last_x_nonzero = self.last_x_nonzero
        for sprite in visible:
            entry = (base + sprite * 8) & 0xFFFF
            cached = sprite * 4
            y = (((cache[cached] << 8) | cache[cached + 1]) & mask_v) - start_v
            size = cache[cached + 2]
            cells_x = ((size >> 2) & 3) + 1
            cells_y = (size & 3) + 1
            name = (vram[entry + 4] << 8) | vram[entry + 5]
            x = (((vram[entry + 6] << 8) | vram[entry + 7]) & 0x1FF) - 0x80
            if x == -0x80:
                # "Rendering of further sprite pixels is disabled whenever a sprite is
                # read from a cache slot with X=0 when the previous sprite read ... had
                # X != 0" (Kabuto, phase 3); X = 0 is screen -128 here.
                if last_x_nonzero:
                    rendering = False
                last_x_nonzero = False
            else:
                last_x_nonzero = True
            attribute = (name >> 9) & 0x70
            flip_x = bool(name & 0x0800)
            # "The VDP just considers the lowermost 5 bits of the difference of sprite Y
            # coordinate and raster line" (Kabuto, phase 2), doubled in interlace mode 2.
            row = (screen_row - y) & (0x3F if im2 else 0x1F)
            if name & 0x1000:
                row = cells_y * tile_rows - 1 - row
            pattern = (name & 0x7FE) if im2 else (name & 0x7FF)
            cell_row = row // tile_rows
            pattern_line = row & (tile_rows - 1)
            out_of_tiles = False
            for cell in range(cells_x):
                if tiles_left == 0:
                    out_of_tiles = True  # the sprite is cut off partway: the dots ran out
                    break
                tiles_left -= 1
                if not rendering:
                    continue
                column = (cells_x - 1 - cell) if flip_x else cell
                tile = pattern + (column * cells_y + cell_row) * step
                address = ((tile & 0x7FF) * 32 + pattern_line * 4) & 0xFFFF
                left = x + cell * 8
                if left <= -8 or left >= width:
                    continue
                row_pixels = self._tile_row(address, attribute, flip_x)
                for i in range(8):
                    pixel = row_pixels[i]
                    at = left + i
                    if pixel and 0 <= at < width:
                        if line[at]:
                            self.collision = True
                        else:
                            line[at] = pixel
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

    def render_line(self, v: int) -> None:
        """Draw display line ``v``, the V counter's value for it."""
        vdp = self.vdp
        width = 320 if vdp.hmode.name == "H40" else 256
        im2 = vdp.interlace_double
        row = ((v << 1) | (1 if vdp.odd else 0)) if im2 else v
        if row >= 480:
            return
        out = self.pixels[row]
        self.width = width
        if row + 1 > self.rows:
            self.rows = row + 1
        backdrop = vdp.registers[7] & 0x3F
        if not vdp.display_enabled:
            # The backdrop colour. On the hardware the planes keep showing whatever is
            # on the bus (Kabuto, "Effects of 'disable display'"), which is not modelled.
            for x in range(width):
                out[x] = backdrop
            self.last_x_nonzero = False  # no sprites are fetched with the display off
            return
        line_a = [0] * width
        line_b = [0] * width
        line_s = [0] * width
        self._planes(line_a, line_b, width, v, row, im2)
        self._sprites(line_s, width, row, im2)
        self._mix(out, line_a, line_b, line_s, width, backdrop)

    # -- the mixer ----------------------------------------------------------------

    def _mix(self, out: bytearray, line_a: list[int], line_b: list[int], line_s: list[int],
             width: int, backdrop: int) -> None:  # fmt: skip
        """Priority and shadow/highlight, pixel by pixel.

        Priority (CMD ``genvdp.txt`` 14): backdrop < B low < A low < sprite low <
        B high < A high < sprite high. Shadow/highlight (Kabuto, "S/H"): the three
        steps of his "assumed operation mode", in order.
        """
        shadow_mode = bool(self.vdp.registers[12] & 8)
        debug = self.vdp.debug_register
        force = (debug >> 7) & 3
        blank = bool(debug & 0x40)
        for x in range(width):
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
        """A new frame begins: the rows drawn so far are the finished picture."""
        if self.rows:
            self.height = self.rows
        self.rows = 0

    def frame(self) -> tuple[int, int, list[bytearray]]:
        """The picture: its width, its height, and its rows of shaded CRAM indices.

        Between frames (where :meth:`~megadrive_python.system.System.run_frame` stops)
        that is the frame just finished; called part way through one, what it has drawn.
        """
        height = self.rows or self.height
        return self.width, height, [self.pixels[row] for row in range(height)]

    def frame_rgb(self) -> list[list[int]]:
        """The picture as rows of RGB888, through the VDP's measured colour ramp."""
        width, _height, rows = self.frame()
        cram = self.vdp.cram
        palette = []
        for shade in range(3):
            levels = LEVELS[shade]
            palette.append([
                (levels[(colour >> 1) & 7] << 16)
                | (levels[(colour >> 5) & 7] << 8)
                | levels[(colour >> 9) & 7]
                for colour in cram
            ])  # fmt: skip
        return [
            [palette[value >> SHADE_SHIFT][value & 0x3F] for value in row[:width]] for row in rows
        ]

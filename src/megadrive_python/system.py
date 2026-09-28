"""A whole console: m68000-python's 68000 and z80-python's Z80 on a :class:`Board`.

One scheduler, in master clocks (MCLK)
--------------------------------------

Every device's time is a count of master clocks from power-on (docs/timing.md): the
68000 ticks every 7, the Z80 every 15, the VDP by its slot table. The scheduler is
the 68000's loop, because the 68000 owns the main bus and makes almost every
access that matters to the VDP:

1. before a 68000 step, the VDP is brought up to the step's start and its interrupt
   level is placed on the 68000's IPL lines;
2. the 68000 runs one instruction (or exception entry); each of its bus accesses is
   placed at its own clock inside the instruction (``Board.m68k_access_clock``), so
   the VDP, when touched, catches up to that exact clock;
3. any stall a device imposes (a full FIFO, a DMA holding the bus) is added, in
   MCLK, to that step, which is how the CPU that suffers it is delayed;
4. the Z80 then runs until it has caught up with the 68000. It also catches up
   *before* the 68000 changes anything the Z80 can observe -- its RAM, its bus
   request and reset lines, the YM2612, the bank register -- so a bus request
   stops the Z80 at the right instruction.

Why this shape: the 68000 is the CPU whose accesses have to land on the right clock
(the FIFO and slot timing of milestones 2 and 5), and m68000-python reports the
clock of each access as it makes it; the Z80's accesses matter mostly by order, and
z80-python steps a whole instruction at a time (its access-timing table says where
in it each access falls, and the board places its device accesses there), so it runs
in the 68000's wake, at most one 68000 instruction behind, with the catch-up points
above keeping every shared state change in order. The VDP is lazy (it runs only when
read, written, or at the start of a 68000 step) because it has nothing to do between
those points that the CPUs could see. When the sound chips are modelled they catch
up the same way.
"""

from __future__ import annotations

from m68000_python import AUTOVECTOR, M68000CPU
from z80_python import Z80CPU
from z80_python.timing import instruction_timing

from .arbiter import BUSACK_PROPAGATION_MCLK, busack_grant
from .audio import AudioPath
from .board import Board
from .cartridge import Cartridge
from .machine import M68K_DIVIDER, Z80_DIVIDER, Machine

#: The longest Z80 step in T-states (an interrupt entry in IM 2 is 19, the longest
#: instruction 23): an instruction that starts this long before a bus request
#: has finished before it.
MAX_Z80_T = 23


#: 68000 clocks the reset button holds the 68000 in reset: "68k RESET pulse is asserted
#: for 128 VCLK" (Eke, forum t=1262; VCLK is the 68000's clock).
RESET_PULSE_CLOCKS = 128


class System:
    """Machine + cartridge, ready to run."""

    def __init__(self, machine: Machine, cartridge: Cartridge | None, *,
                 pads: tuple[object, object] | None = None,
                 tmss_rom: bytes | None = None) -> None:  # fmt: skip
        self.machine = machine
        board = self.board = Board(machine, cartridge, pads=pads)
        if tmss_rom is not None:
            board.install_tmss_rom(tmss_rom)
        self.m68k = M68000CPU(
            board.read_byte, board.read_word, board.write_byte, board.write_word,
            tas_write=board.tas_write, acknowledge=self._acknowledge,
        )  # fmt: skip
        self.z80 = Z80CPU(board.z80_read, board.z80_write)
        board.m68k = self.m68k
        board.catch_up_z80 = self.catch_up_z80
        board.z80_busack = self.z80_busack
        board.on_z80_reset = self._z80_reset_line
        self.vdp = board.vdp
        #: The analogue path the two sound chips meet in (docs: audio.py).
        self.audio = AudioPath(machine)
        #: The 68000's time (MCLK) at the start of its next step.
        self.mclk = 0
        #: The Z80's time (MCLK) at the start of its next step.
        self.z80_mclk = 0
        self._in_z80 = False
        self.m68k_instructions = 0
        #: Access offsets by instruction bytes (:meth:`_z80_offsets`).
        self._z80_timings: dict[tuple[int, ...], tuple[int, ...]] = {}
        self.z80_instructions = 0
        #: Instructions the Z80 left for later because the 68000 took its bus in the
        #: middle of them (catch_up_z80).
        self.z80_deferred = 0
        #: The reset button has been pressed and the 68000 not yet reset
        #: (:meth:`press_reset`).
        self.reset_pressed = False
        # Power-on: the 68000 takes its reset vector; the Z80 is held in reset.
        self.mclk = self.m68k.reset() * M68K_DIVIDER
        self._z80_reset_line(True)

    # -- the CPUs' interrupt lines ------------------------------------------------

    def _acknowledge(self, level: int) -> int:
        """The 68000's interrupt-acknowledge cycle: the VDP clears the request it
        answers; every level is autovectored (MAME's ``cpu_space_map`` as a
        detector; the Manual, PDF page 19, gives the levels)."""
        self.vdp.acknowledge(level)
        return AUTOVECTOR

    def _z80_reset_line(self, held: bool) -> None:
        if held:
            # The reset state is applied at once; the Z80 is not stepped while held.
            z80 = self.z80
            z80.request_reset()
            z80.step()
            z80.clear_reset()
            # RESET "clears the Program Counter and registers I and R" (Zilog UM0080,
            # "Reset"); z80-python 0.4.0 clears PC but leaves I and R, so the board does
            # it (a workaround: the change is proposed to z80-python, docs/worklog.md).
            z80.i = 0
            z80.r = 0

    # -- running --------------------------------------------------------------------

    def catch_up_z80(self, mclk: int, stopping: bool = False) -> None:
        """Run the Z80 until its time reaches ``mclk`` (no-op inside a Z80 step).

        ``stopping``: the caller is about to take the bus from the Z80 or reset it
        at ``mclk``. The Z80 samples BUSREQ "with the rising edge of the last clock
        period of any machine cycle" and lets go of the bus after it (Zilog UM0080,
        "Bus Request/Acknowledge Cycle"; specification tier, and MAME 0.285 agrees:
        Sonic's handshake reads the Z80 RAM byte the interrupted instruction had not
        yet written, docs/worklog.md). So an instruction under way at ``mclk`` whose
        memory accesses are not all started by then does not run now: it is left for
        when the Z80 has the bus again (:meth:`z80_access_offsets` estimates where
        the accesses fall).
        """
        if self._in_z80:
            return
        board, z80, vdp = self.board, self.z80, self.vdp
        self._in_z80 = True
        try:
            while self.z80_mclk < mclk:
                if not board.z80_running:
                    self.z80_mclk = mclk
                    break
                if stopping and self.z80_mclk + MAX_Z80_T * Z80_DIVIDER >= mclk:
                    offsets = self.z80_access_offsets()
                    if any(self.z80_mclk + t * Z80_DIVIDER >= mclk for t in offsets):
                        self.z80_deferred += 1
                        break
                if vdp.z80_irq_at(self.z80_mclk):
                    z80.request_maskable_interrupt(0xFF)
                    if z80.iff1 and not z80._ei_delay:  # z80-python's acceptance test
                        # The acknowledge takes this step; the table times instructions.
                        board.z80_offsets = None
                    else:
                        board.z80_offsets = self._z80_offsets()
                else:
                    z80.clear_maskable_interrupt()
                    board.z80_offsets = self._z80_offsets()
                board.z80_access = 0
                board.step_start = self.z80_mclk
                board.z80_stall = 0
                self.z80_mclk += z80.step() * Z80_DIVIDER + board.z80_stall
                self.z80_instructions += 1
        finally:
            self._in_z80 = False
            board.step_start = self.mclk

    def _z80_offsets(self) -> tuple[int, ...]:
        """Where each memory access of the Z80's next instruction falls, in master
        clocks from its start: z80-python's access-timing table (``timing``,
        ``instruction_timing``; derived from the SingleStepTests corpus, so
        emulator-derived), read at PC with no side effects and kept by the
        instruction's first four bytes."""
        board, pc = self.board, self.z80.pc
        key = tuple(board.z80_peek((pc + i) & 0xFFFF) for i in range(4))
        offsets = self._z80_timings.get(key)
        if offsets is None:
            timing = instruction_timing(key)
            offsets = tuple(a.t * Z80_DIVIDER for a in timing.accesses
                            if a.kind in ("read", "write"))  # fmt: skip
            self._z80_timings[key] = offsets
        return offsets

    def z80_access_offsets(self) -> list[int]:
        """The T-state offsets of :meth:`z80_probe`."""
        return self.z80_probe()[0]

    def z80_busack(self, mclk: int) -> int:
        """The master clock /BUSAK reaches the 68000, for a request made at ``mclk``.

        BUSREQ "is sampled by the CPU with the rising edge of the last clock period of
        any machine cycle" and the bus is released after it (Zilog UM0080,
        specification tier), so the Z80 lets go at the end of the machine cycle in
        progress; the arbiter then takes
        :data:`~megadrive_python.arbiter.BUSACK_PROPAGATION_MCLK` to report it (fitted
        to Nuked-MD, die-derived: the specification says when the Z80 lets go, the
        referee says how long the board takes to say so). The Z80 is first run up to
        the request, leaving at most one instruction -- the one the request falls
        inside -- unrun; its machine-cycle boundaries are the probe's
        (:meth:`z80_machine_cycle_ends`), and that instruction is executed when the bus
        comes back, as before.
        """
        self.catch_up_z80(mclk, stopping=True)
        if self.z80_mclk >= mclk or not self.board.z80_running:
            return max(mclk, self.z80_mclk) + BUSACK_PROPAGATION_MCLK
        return busack_grant(
            mclk, [self.z80_mclk + t * Z80_DIVIDER for t in self.z80_machine_cycle_ends()]
        )

    def z80_machine_cycle_ends(self) -> list[int]:
        """Where the next Z80 step's machine cycles end, in T-states from its start.

        The probe places each fetch and data access (:meth:`z80_probe`); a machine
        cycle ends where the next begins, and the step's own total ends the last one,
        so trailing internal T-states belong to the cycle before them.
        """
        return self.z80_probe()[2]

    def z80_probe(self) -> tuple[list[int], dict[int, int], list[int]]:
        """When the Z80's next step would start each of its data accesses, in T-states
        from the step's start: an estimate, with no effect on the machine.

        The step is run once on a probe bus that records its accesses, and the CPU's
        state is then restored. (z80-python's access-timing table, which
        :meth:`_z80_offsets` reads, now gives the same exactly; the probe predates it.)
        The leading reads at PC, PC+1, ... are the fetches: 4 T-states for the first
        opcode byte and for an opcode after a DD/ED/FD/CB prefix, 3 for other bytes
        (UM0080's M1 and memory-read cycles); each data access takes 3. Internal
        T-states are not placed (the core does not say where they fall), so each
        estimate is the earliest the access can start. Reads of devices return $FF
        on the probe bus: only RAM and ROM are read, so a probe has no side effects.
        Returns the offsets, the writes the step would make ({address: value}), and
        where each machine cycle ends (every fetch's and access's end, and the step's
        own total, so that trailing internal T-states belong to the cycle before them).
        """
        z80, board = self.z80, self.board
        state = z80.capture_state()
        read, write = z80.read_byte, z80.write_byte
        offsets: list[int] = []
        ends: list[int] = []
        written: dict[int, int] = {}
        clock = 0
        fetching = True
        next_fetch = z80.pc
        after_prefix = True  # the first byte is an M1 fetch

        def probe_read(address: int) -> int:
            nonlocal clock, fetching, next_fetch, after_prefix
            if address in written:
                value = written[address]
            elif address < 0x4000:
                value = board.z80_ram[address & 0x1FFF]
            elif address >= 0x8000 and (board.bank | (address & 0x7FFF)) < 0x400000:
                value = board.cartridge.read_byte(board.bank | (address & 0x7FFF))
            else:
                value = 0xFF
            if fetching and address == next_fetch:
                clock += 4 if after_prefix else 3
                after_prefix = value in (0xCB, 0xDD, 0xED, 0xFD) and after_prefix
                next_fetch = (address + 1) & 0xFFFF
            else:
                fetching = False
                offsets.append(clock)
                clock += 3
            ends.append(clock)
            return value

        def probe_write(address: int, value: int) -> None:
            nonlocal clock, fetching
            fetching = False
            written[address] = value
            offsets.append(clock)
            clock += 3
            ends.append(clock)

        z80.read_byte, z80.write_byte = probe_read, probe_write
        try:
            total = z80.step()
        finally:
            z80.read_byte, z80.write_byte = read, write
            z80.restore_state(state)
        return offsets, written, sorted({*(e for e in ends if e < total), total})

    def press_reset(self) -> None:
        """The console's reset button: not a power cycle.

        Measured on hardware (forum t=1262): the button pulses VRES#, which goes "to
        68000 and cartridge connector", and "the 68k RESET pulse is asserted for 128
        VCLK" with /HALT (Charles MacDonald, and Eke against the 68000's clock); "when
        RESET button is pressed, !ZRES is held low at the same time ... but remains low
        after that until software enables it again" (Eke), so the Z80 and the YM2612
        are held in reset until the game writes $A11200. SRES#, which resets the VDP
        and the I/O chip, "is only asserted on a cold boot", so they are left alone,
        as is the cartridge's board (what one does with VRES# is not in these sources).

        ZRES is asserted now; the 68000 is held and reset at its next step. The
        variable delay from the switch to the pulse -- "Minimum is 2.1ms" -- is not
        modelled: the pulse starts at the next instruction.
        """
        self.board.set_z80_reset(True, self.mclk)
        self.reset_pressed = True

    def step(self) -> int:
        """One 68000 instruction, then the Z80 up to the same time; returns MCLK."""
        board, cpu, vdp = self.board, self.m68k, self.vdp
        if self.reset_pressed:
            # The 68000 is held in reset for the pulse, the rest of the machine running
            # on, then takes the reset exception (which brings a halted one back too).
            self.reset_pressed = False
            start = self.mclk
            vdp.sync(start)
            cpu.clock += RESET_PULSE_CLOCKS
            elapsed = (RESET_PULSE_CLOCKS + cpu.reset()) * M68K_DIVIDER
            self.mclk = start + elapsed
            self.catch_up_z80(self.mclk)
            return elapsed
        owed = board.m68k_pending_stall
        if owed:
            # What the arbiter charged the 68000 for Z80 bank accesses made in the wake
            # of its last instruction: the Z80 runs behind it, so the delay is added
            # here rather than inside the instruction it really fell in (arbiter.py).
            board.m68k_pending_stall = 0
            self.mclk += owed
            cpu.clock += owed // M68K_DIVIDER  # keeps the E-clock phase in step
        start = self.mclk
        board.step_start = start
        board.stall = 0
        board.z80_space_stall = 0
        if board.m68k_accesses:
            board.m68k_accesses.clear()
        vdp.sync(start)
        cpu.set_ipl(vdp.ipl)
        clocks = cpu.step()
        stall = board.stall
        elapsed = clocks * M68K_DIVIDER + stall
        if stall:
            cpu.clock += stall // M68K_DIVIDER  # keeps the E-clock phase in step
        self.mclk = start + elapsed
        self.m68k_instructions += 1
        self.catch_up_z80(self.mclk)
        return elapsed

    def run_frame(self) -> None:
        """Run until the VDP starts its next frame, then bring the sound chips up
        to the same clock so their sample queues fill a frame at a time."""
        frame = self.vdp.frame
        while self.vdp.frame == frame:
            self.step()
        self.board.sound_sync(self.mclk)

    def take_audio(self) -> list[tuple[int, int]]:
        """The stereo samples finished since the last call, at the host rate.

        Both chips are brought up to the 68000's clock first, so a caller that only
        drains at long intervals still gets every sample.
        """
        self.board.sound_sync(self.mclk)
        return self.audio.mix(self.board.fm.take_samples(), self.board.psg.take_samples())

    def run_mclk(self, mclk: int) -> None:
        target = self.mclk + mclk
        while self.mclk < target:
            self.step()

"""Built In Self Test (BIST) modules for testing liteDRAM functionality."""

from functools import reduce
from operator import xor

from litex.gen import *
from litex.gen.genlib.cdc import PulseSynchronizer, BusSynchronizer

from litex.soc.interconnect.csr import *

from litedram.frontend.dma import LiteDRAMDMAWriter, LiteDRAMDMAReader


@CEInserter()
class LFSR(Module):
    """Linear-Feedback Shift Register to generate a pseudo-random sequence."""

    def __init__(self, n_out, n_state=31, taps=[27, 30]):
        self.o = Signal(n_out)

        # # #

        state = Signal(n_state)
        curval = [state[i] for i in range(n_state)]
        curval += [0]*(n_out - n_state)
        for i in range(n_out):
            nv = ~reduce(xor, [curval[tap] for tap in taps])
            curval.insert(0, nv)
            curval.pop()

        self.sync += [
            state.eq(Cat(*curval[:n_state])),
            self.o.eq(Cat(*curval))
        ]


@CEInserter()
class Counter(Module):
    """Simple incremental counter."""

    def __init__(self, n_out):
        self.o = Signal(n_out)
        self.sync += self.o.eq(self.o + 1)


class _LiteDRAMBISTGenerator(Module):

    def __init__(self, dram_port, random):
        self.start = Signal()
        self.done = Signal()
        self.base = Signal(dram_port.aw)
        self.length = Signal(dram_port.aw)

        # # #

        self.submodules.dma = dma = LiteDRAMDMAWriter(dram_port)

        if random:
        	self.submodules.gen = gen = LFSR(dram_port.dw)
        else:
        	self.submodules.gen = gen = Counter(dram_port.dw)

        self.started = started = Signal()
        enable = Signal()
        counter = Signal(dram_port.aw)
        self.comb += enable.eq(started & (counter != (self.length - 1)))
        self.sync += [
            If(self.start,
                started.eq(1),
                counter.eq(0)
            ).Elif(gen.ce,
                counter.eq(counter + 1)
            )
        ]

        self.comb += [
            dma.sink.valid.eq(enable),
            dma.sink.address.eq(self.base + counter),
            dma.sink.data.eq(gen.o),
            gen.ce.eq(enable & dma.sink.ready),

            self.done.eq(~enable & started)
        ]


class LiteDRAMBISTGenerator(Module, AutoCSR):
    """litex module to generate a given pattern in memory.abs

    CSRs:
     * reset - Reset the module
     * start - Start the checking
     * done - The module has completed writing pattern

     * base - DRAM address to start from.
     * length - Number of DRAM words to check for.
    """

    def __init__(self, dram_port, random=True):
        self.reset = CSR()
        self.start = CSR()
        self.done = CSRStatus()
        self.base = CSRStorage(dram_port.aw)
        self.length = CSRStorage(dram_port.aw)

        # # #

        cd = dram_port.cd

        core = ResetInserter()(_LiteDRAMBISTGenerator(dram_port, random))
        self.submodules.core = ClockDomainsRenamer(cd)(core)

        reset_sync = BusSynchronizer(1, "sys", cd)
        start_sync = PulseSynchronizer("sys", cd)
        done_sync = BusSynchronizer(1, cd, "sys")
        self.submodules += reset_sync, start_sync, done_sync

        base_sync = BusSynchronizer(dram_port.aw, "sys", cd)
        length_sync = BusSynchronizer(dram_port.aw, "sys", cd)
        self.submodules += base_sync, length_sync

        self.comb += [
            reset_sync.i.eq(self.reset.re),
            core.reset.eq(reset_sync.o),

            start_sync.i.eq(self.start.re),
            core.start.eq(start_sync.o),

            done_sync.i.eq(core.done),
            self.done.status.eq(done_sync.o),

            base_sync.i.eq(self.base.storage),
            core.base.eq(base_sync.o),

            length_sync.i.eq(self.length.storage),
            core.length.eq(length_sync.o)
        ]


class _LiteDRAMBISTChecker(Module, AutoCSR):

    def __init__(self, dram_port, random):
        self.start = Signal()

        self.base = Signal(dram_port.aw)
        self.length = Signal(dram_port.aw)
        self.halt_on_error = Signal()

        self.error_count = Signal(32)
        self.error_addr = Signal(dram_port.aw)

        self.error_wanted = Signal(dram_port.dw)
        self.error_actual = Signal(dram_port.dw)

        self.done = Signal()

        # # #

        self.submodules.dma = dma = LiteDRAMDMAReader(dram_port)

        if random:
        	self.submodules.gen = gen = LFSR(dram_port.dw)
        else:
        	self.submodules.gen = gen = Counter(dram_port.dw)

        self._address_counter = address_counter = Signal(dram_port.aw)

        self.started = started = Signal()
        self.sync += [
            If(self.start,
                started.eq(1),
                If(self.error_addr != 0,
                    self.error_addr.eq(0),
                    address_counter.eq(address_counter+1),
                ),
            ),
        ]

        address_enable = Signal()
        address_counter_ce = Signal()
        self.comb += [
            address_enable.eq(started & (address_counter != (self.length - 1))),
            address_counter_ce.eq(address_enable & dma.sink.ready),

            dma.sink.valid.eq(address_enable),
            dma.sink.address.eq(self.base + address_counter),
        ]
        self.sync += [
            If(address_counter_ce,
                address_counter.eq(address_counter + 1)
            ),
        ]

        self._data_counter = data_counter = Signal(dram_port.aw)
        data_enable = Signal()
        data_counter_ce = Signal()
        self.comb += [
            data_enable.eq(started & (data_counter != address_counter)),
            data_counter_ce.eq(data_enable & dma.source.valid),

            dma.source.ready.eq(data_counter_ce),
            gen.ce.eq(data_counter_ce),
        ]
        self.sync += [
            If(data_counter_ce,
                data_counter.eq(data_counter + 1),
                If(dma.source.data != gen.o,
                    self.error_count.eq(self.error_count + 1),
                    self.error_addr.eq(self.base + data_counter),
                    self.error_wanted.eq(gen.o),
                    self.error_actual.eq(dma.source.data),
                    If(self.halt_on_error,
                        started.eq(0),
                    ),
                )
            ),
        ]

        error = Signal()
        self.comb += [
            error.eq(self.halt_on_error & (self.error_addr != 0)),
        ]

        self.comb += self.done.eq((~data_enable & ~address_enable & started) | error)


class LiteDRAMBISTChecker(Module, AutoCSR):
    """litex module to check a given pattern in memory.

    Attributes
    ----------
    reset : in
        Reset the module
    start : in
        Start the checking

    base : in
        DRAM address to start from.
    length : in
        Number of DRAM words to check for.
    halt_on_error : in
        Stop checking at the first error to occur.

    done : out
        The module has completed checking

    error_count : out
        Number of DRAM words which don't match.
    error_addr : out
        Address of the last error to occur.
    """

    def __init__(self, dram_port, random=True):
        self.reset = CSR()
        self.start = CSR()
        self.done = CSRStatus()
        self.base = CSRStorage(dram_port.aw)
        self.length = CSRStorage(dram_port.aw)
        self.halt_on_error = CSRStorage()

        self.error_count = CSRStatus(32)
        self.error_addr = CSRStatus(dram_port.aw)

        # # #

        cd = dram_port.cd

        core = ResetInserter()(_LiteDRAMBISTChecker(dram_port, random))
        self.submodules.core = ClockDomainsRenamer(cd)(core)

        #reset_sync = PulseSynchronizer("sys", cd)
        reset_sync = BusSynchronizer(1, "sys", cd)
        start_sync = PulseSynchronizer("sys", cd)
        self.submodules += reset_sync, start_sync
        self.comb += [
            reset_sync.i.eq(self.reset.re),
            core.reset.eq(reset_sync.o),

            start_sync.i.eq(self.start.re),
            core.start.eq(start_sync.o),
        ]

        done_sync = BusSynchronizer(1, cd, "sys")
        self.submodules += done_sync
        self.comb += [
            done_sync.i.eq(core.done),
            self.done.status.eq(done_sync.o),
        ]

        base_sync = BusSynchronizer(dram_port.aw, "sys", cd)
        length_sync = BusSynchronizer(dram_port.aw, "sys", cd)
        halt_on_error_sync = BusSynchronizer(1, "sys", cd)
        self.submodules += base_sync, length_sync, halt_on_error_sync
        self.comb += [
            base_sync.i.eq(self.base.storage),
            core.base.eq(base_sync.o),

            length_sync.i.eq(self.length.storage),
            core.length.eq(length_sync.o),

            halt_on_error_sync.i.eq(self.halt_on_error.storage),
            core.halt_on_error.eq(halt_on_error_sync.o),
        ]

        error_count_sync = BusSynchronizer(32, cd, "sys")
        error_addr_sync = BusSynchronizer(dram_port.aw, cd, "sys")
        self.submodules += error_addr_sync, error_count_sync
        self.comb += [
            error_count_sync.i.eq(core.error_count),
            self.error_count.status.eq(error_count_sync.o),

            error_addr_sync.i.eq(core.error_addr),
            self.error_addr.status.eq(error_addr_sync.o),
        ]

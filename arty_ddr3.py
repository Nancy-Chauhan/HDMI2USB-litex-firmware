#!/usr/bin/env python3
import argparse
import os

from litex.gen import *
from litex.gen.genlib.resetsync import AsyncResetSynchronizer

from litex.boards.platforms import arty

from litex.soc.integration.soc_core import mem_decoder
from litex.soc.integration.soc_sdram import *
from litex.soc.cores.sdram.settings import SDRAMModule
from litex.soc.integration.builder import *
from litex.soc.cores.uart.bridge import UARTWishboneBridge

from gateware import a7ddrphy
from gateware import dna, xadc

from litescope import LiteScopeAnalyzer

# TODO: use half-rate DDR3 phy and use 100Mhz CPU clock

class MT41K128M16(SDRAMModule):
    memtype = "DDR3"
    # geometry
    nbanks = 8
    nrows  = 16384
    ncols  = 1024
    # timings (-7 speedgrade)
    tRP   = 13.75
    tRCD  = 13.75
    tWR   = 15
    tWTR  = 8
    tREFI = 64*1000*1000/8192
    tRFC  = 160


class _CRG(Module):
    def __init__(self, platform):
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_sys4x = ClockDomain(reset_less=True)
        self.clock_domains.cd_sys4x_dqs = ClockDomain(reset_less=True)
        self.clock_domains.cd_clk200 = ClockDomain()

        clk100 = platform.request("clk100")
        rst = platform.request("cpu_reset")

        pll_locked = Signal()
        pll_fb = Signal()
        self.pll_sys = Signal()
        pll_sys4x = Signal()
        pll_sys4x_dqs = Signal()
        pll_clk200 = Signal()
        self.specials += [
            Instance("PLLE2_BASE",
                     p_STARTUP_WAIT="FALSE", o_LOCKED=pll_locked,

                     # VCO @ 1600 MHz
                     p_REF_JITTER1=0.01, p_CLKIN1_PERIOD=10.0,
                     p_CLKFBOUT_MULT=16, p_DIVCLK_DIVIDE=1,
                     i_CLKIN1=clk100, i_CLKFBIN=pll_fb, o_CLKFBOUT=pll_fb,

                     # 100 MHz
                     p_CLKOUT0_DIVIDE=16, p_CLKOUT0_PHASE=0.0,
                     o_CLKOUT0=self.pll_sys,

                     # 400 MHz
                     p_CLKOUT1_DIVIDE=4, p_CLKOUT1_PHASE=0.0,
                     o_CLKOUT1=pll_sys4x,

                     # 400 MHz dqs
                     p_CLKOUT2_DIVIDE=4, p_CLKOUT2_PHASE=90.0,
                     o_CLKOUT2=pll_sys4x_dqs,

                     # 200 MHz
                     p_CLKOUT3_DIVIDE=8, p_CLKOUT3_PHASE=0.0,
                     o_CLKOUT3=pll_clk200,

                     # 400MHz
                     p_CLKOUT4_DIVIDE=4, p_CLKOUT4_PHASE=0.0,
                     #o_CLKOUT4=
            ),
            Instance("BUFG", i_I=self.pll_sys, o_O=self.cd_sys.clk),
            Instance("BUFG", i_I=pll_sys4x, o_O=self.cd_sys4x.clk),
            Instance("BUFG", i_I=pll_sys4x_dqs, o_O=self.cd_sys4x_dqs.clk),
            Instance("BUFG", i_I=pll_clk200, o_O=self.cd_clk200.clk),
            AsyncResetSynchronizer(self.cd_sys, ~pll_locked | ~rst),
            AsyncResetSynchronizer(self.cd_clk200, ~pll_locked | rst),
        ]

        reset_counter = Signal(4, reset=15)
        ic_reset = Signal(reset=1)
        self.sync.clk200 += \
            If(reset_counter != 0,
                reset_counter.eq(reset_counter - 1)
            ).Else(
                ic_reset.eq(0)
            )
        self.specials += Instance("IDELAYCTRL", i_REFCLK=ClockSignal("clk200"), i_RST=ic_reset)


class BaseSoC(SoCSDRAM):
    default_platform = "arty"

    csr_map = {
        "ddrphy":   17,
        "dna":      18,
        "xadc":     19,
        "analyzer": 20
    }
    csr_map.update(SoCSDRAM.csr_map)

    def __init__(self,
                 platform,
                 **kwargs):
        clk_freq = 100*1000000
        SoCSDRAM.__init__(self, platform, clk_freq,
            cpu_type=None,
            l2_size=0,
            with_uart=False,
            with_timer=False)

        self.submodules.crg = _CRG(platform)
        self.submodules.dna = dna.DNA()
        self.submodules.xadc = xadc.XADC()

        # sdram
        self.submodules.ddrphy = a7ddrphy.A7DDRPHY(platform.request("ddram"))
        sdram_module = MT41K128M16(self.clk_freq, "1:4")
        self.register_sdram(self.ddrphy, "minicon",
                            sdram_module.geom_settings,
                            sdram_module.timing_settings)


        # uart
        self.add_cpu_or_bridge(UARTWishboneBridge(platform.request("serial"), clk_freq, baudrate=115200))
        self.add_wb_master(self.cpu_or_bridge.wishbone)

        # litescope
        trigger = Signal()
        analyzer_signals = [
            # p0
            self.ddrphy.dfi.p0.address,
            self.ddrphy.dfi.p0.bank,
            self.ddrphy.dfi.p0.cas_n,
            self.ddrphy.dfi.p0.cs_n,
            self.ddrphy.dfi.p0.ras_n,
            self.ddrphy.dfi.p0.we_n,
            self.ddrphy.dfi.p0.cke,
            self.ddrphy.dfi.p0.odt,
            self.ddrphy.dfi.p0.reset_n,

            self.ddrphy.dfi.p0.wrdata,
            self.ddrphy.dfi.p0.wrdata_en,
            self.ddrphy.dfi.p0.wrdata_mask,

            self.ddrphy.dfi.p0.rddata_en,
            self.ddrphy.dfi.p0.rddata,
            self.ddrphy.dfi.p0.rddata_valid,

            # p1
            self.ddrphy.dfi.p1.address,
            self.ddrphy.dfi.p1.bank,
            self.ddrphy.dfi.p1.cas_n,
            self.ddrphy.dfi.p1.cs_n,
            self.ddrphy.dfi.p1.ras_n,
            self.ddrphy.dfi.p1.we_n,
            self.ddrphy.dfi.p1.cke,
            self.ddrphy.dfi.p1.odt,
            self.ddrphy.dfi.p1.reset_n,

            self.ddrphy.dfi.p1.wrdata,
            self.ddrphy.dfi.p1.wrdata_en,
            self.ddrphy.dfi.p1.wrdata_mask,

            self.ddrphy.dfi.p1.rddata_en,
            self.ddrphy.dfi.p1.rddata,
            self.ddrphy.dfi.p1.rddata_valid,

#            # p2
#            self.ddrphy.dfi.p2.address,
#            self.ddrphy.dfi.p2.bank,
#            self.ddrphy.dfi.p2.cas_n,
#            self.ddrphy.dfi.p2.cs_n,
#            self.ddrphy.dfi.p2.ras_n,
#            self.ddrphy.dfi.p2.we_n,
#            self.ddrphy.dfi.p2.cke,
#            self.ddrphy.dfi.p2.odt,
#            self.ddrphy.dfi.p2.reset_n,
#
#            self.ddrphy.dfi.p2.wrdata,
#            self.ddrphy.dfi.p2.wrdata_en,
#            self.ddrphy.dfi.p2.wrdata_mask,
#
#            self.ddrphy.dfi.p2.rddata_en,
#            self.ddrphy.dfi.p2.rddata,
#            self.ddrphy.dfi.p2.rddata_valid,
#
#            # p3
#            self.ddrphy.dfi.p3.address,
#            self.ddrphy.dfi.p3.bank,
#            self.ddrphy.dfi.p3.cas_n,
#            self.ddrphy.dfi.p3.cs_n,
#            self.ddrphy.dfi.p3.ras_n,
#            self.ddrphy.dfi.p3.we_n,
#            self.ddrphy.dfi.p3.cke,
#            self.ddrphy.dfi.p3.odt,
#            self.ddrphy.dfi.p3.reset_n,
#
#            self.ddrphy.dfi.p3.wrdata,
#            self.ddrphy.dfi.p3.wrdata_en,
#            self.ddrphy.dfi.p3.wrdata_mask,
#
#            self.ddrphy.dfi.p3.rddata_en,
#            self.ddrphy.dfi.p3.rddata,
#            self.ddrphy.dfi.p3.rddata_valid
        ]
#        self.submodules.analyzer = LiteScopeAnalyzer(analyzer_signals, depth=1024)

#    def do_exit(self, vns):
#        self.analyzer.export_csv(vns, "test/analyzer.csv")


def main():
    parser = argparse.ArgumentParser(description="Arty LiteX SoC")
    builder_args(parser)
    soc_sdram_args(parser)
    args = parser.parse_args()

    platform = arty.Platform()
    soc = BaseSoC(platform, **soc_sdram_argdict(args))
    builder = Builder(soc, output_dir="build", csr_csv="test/csr.csv")
    vns = builder.build()
#    soc.do_exit(vns)

if __name__ == "__main__":
    main()

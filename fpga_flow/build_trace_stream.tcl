# build_trace_stream.tcl — synthesise trace_ddr_stream_top, the shipping
# capture datapath, to a bitstream.
#
#   STM32 ETM 4-bit -> trace_capture_a7 (IBUF->IDDR direct, no IDELAY)
#     -> cap_byte -> la_ddr_writer (ping-pong pack 128b) -> DDR3 ring (16 MB)
#     -> la_ddr_ring_streamer (gearbox 128b->8b) -> packetiser
#     -> fpga_core_net -> UDP :5555
#
# Usage:
#   source $XILINX_VIVADO/settings64.sh   # Vivado 2021.1
#   cd build && vivado -mode batch -source ../fpga_flow/build_trace_stream.tcl
#   # optional: OUTBIT=<name>.bit env override
#
# On the board:
#   1. Program the .bit (openFPGALoader -c ft232 --fpga-part xc7a35tfgg484 [-f])
#   2. Host NIC .245 up; stream_grab captures the UDP stream
#   3. Feed the payload to host/decode/deframe_to_etm.py then cortrace-decode

set part      xc7a35tfgg484-2
set bdir      [file dirname [info script]]
set root      [file normalize [file join $bdir ..]]
set rtl       $root/rtl
set ipdir     $rtl/ddr3/ip
set ve        $rtl/external/verilog-ethernet

create_project -in_memory -part $part

# ---- IP: MIG DDR3 + clocking wizard (Vivado 2021.1 .xci committed) ----
read_ip $ipdir/mig_ddr3/mig_ddr3.xci
read_ip $ipdir/clock/clock.xci
generate_target all [get_ips]
synth_ip [get_ips]

# ---- self-written datapath ----
read_verilog $rtl/trace_capture_a7.v
read_verilog $rtl/ddr3/ddr3_ctrl.v
read_verilog $rtl/ddr3/ddr3_wr_ctrl.v
read_verilog $rtl/ddr3/ddr3_rd_ctrl.v
read_verilog $rtl/ddr3/ddr3_arbit.v
read_verilog $rtl/la_ddr_writer.v
read_verilog $rtl/la_ddr_ring_streamer.v
read_verilog $rtl/fpga_core_net.v
read_verilog $rtl/dbg_regfile.v
read_verilog $rtl/trace_ddr_stream_top.v

# ---- reused verilog-ethernet (MAC/UDP/IP/ARP + AXIS) ----
foreach s {
    lib/eth/rtl/iddr.v
    lib/eth/rtl/oddr.v
    lib/eth/rtl/ssio_ddr_in.v
    lib/eth/rtl/ssio_ddr_out.v
    lib/eth/rtl/rgmii_phy_if.v
    lib/eth/rtl/eth_mac_1g_rgmii_fifo.v
    lib/eth/rtl/eth_mac_1g_rgmii.v
    lib/eth/rtl/eth_mac_1g.v
    lib/eth/rtl/axis_gmii_rx.v
    lib/eth/rtl/axis_gmii_tx.v
    lib/eth/rtl/lfsr.v
    lib/eth/rtl/eth_axis_rx.v
    lib/eth/rtl/eth_axis_tx.v
    lib/eth/rtl/udp_complete.v
    lib/eth/rtl/udp_checksum_gen.v
    lib/eth/rtl/udp.v
    lib/eth/rtl/udp_ip_rx.v
    lib/eth/rtl/udp_ip_tx.v
    lib/eth/rtl/ip_complete.v
    lib/eth/rtl/ip.v
    lib/eth/rtl/ip_eth_rx.v
    lib/eth/rtl/ip_eth_tx.v
    lib/eth/rtl/ip_arb_mux.v
    lib/eth/rtl/arp.v
    lib/eth/rtl/arp_cache.v
    lib/eth/rtl/arp_eth_rx.v
    lib/eth/rtl/arp_eth_tx.v
    lib/eth/rtl/eth_arb_mux.v
    lib/eth/lib/axis/rtl/arbiter.v
    lib/eth/lib/axis/rtl/priority_encoder.v
    lib/eth/lib/axis/rtl/axis_fifo.v
    lib/eth/lib/axis/rtl/axis_async_fifo.v
    lib/eth/lib/axis/rtl/axis_adapter.v
    lib/eth/lib/axis/rtl/axis_async_fifo_adapter.v
    lib/eth/lib/axis/rtl/sync_reset.v
} {
    read_verilog $ve/$s
}

read_xdc $rtl/trace_ddr_stream.xdc

set build_id [clock seconds]
puts "BUILD_ID = $build_id ([clock format $build_id])"
# Capture front-end is IBUF->IDDR direct (no IDELAY): the centre-aligned STM32
# data is sampled on the TRACECLK edge, frequency-independent -- one bitstream
# covers every TRACECLK the board SI supports. See docs/history/.../30-*.md.
synth_design -top trace_ddr_stream_top -part $part -generic BUILD_ID=$build_id

puts "==== CLOCKS ===="
foreach c [get_clocks] { puts "  clock: $c  period=[get_property PERIOD $c]  src=[get_property SOURCE_PINS $c]" }
set g_sys [get_clocks {clk125_u clk125_90_u clk200_u clk100_u}]
set g_mig [get_clocks -include_generated_clocks -of_objects [get_pins u_clock/inst/*/CLKOUT0]]
set_clock_groups -asynchronous \
    -group [get_clocks sys_clk_50] \
    -group [get_clocks phy_rx_clk] \
    -group [get_clocks -include_generated_clocks trace_clk_in] \
    -group $g_sys \
    -group $g_mig
set_false_path -from [get_pins rst_sync_reg[3]/C] \
               -to [get_pins -hier -filter {NAME =~ *rgmii_phy_if_inst*rx_rst_reg_reg*/PRE}]

# CSR bits (clk125) into their clk200 first-stage synchroniser registers are
# quasi-static 2-FF crossings -- exclude the metastability-catcher stage.
set_false_path -to [get_cells -hier -filter {NAME =~ *selftest_s0_reg* || \
                                              NAME =~ *src_fixed_s0_reg*}]

opt_design
place_design
route_design
report_timing_summary -no_detailed_paths -no_header
puts "==== UTILIZATION ===="
report_utilization

set outbit "trace_ddr_stream.bit"
if {[info exists ::env(OUTBIT)]} { set outbit $::env(OUTBIT) }
write_bitstream -force $outbit
puts "============ TRACE STREAM BUILD DONE -> $outbit ============"

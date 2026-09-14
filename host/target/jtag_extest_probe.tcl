# Verify EXTEST + 812-bit boundary DR shift works (no bitstream).
# EXTEST opcode on 7-series = 0x26 (100110). SAMPLE/PRELOAD = 0x01.
# We load EXTEST, then shift a 812-bit DR of all-zero and read it back, to
# confirm the boundary register length and that we can drive/sample pins.

open_hw_manager
connect_hw_server -url localhost:3121 -allow_non_jtag
set tgt ""
set tries 0
while {$tgt eq "" && $tries < 20} {
    catch {refresh_hw_server -force_poll}
    foreach t [get_hw_targets -quiet] { if {$t ne ""} { set tgt $t; break } }
    if {$tgt eq ""} { after 1000; incr tries }
}
current_hw_target $tgt
open_hw_target $tgt -jtag_mode true

# 812 bits = 203 hex nibbles -> 203 hex chars (812/4 = 203)
set len 812
set zeros [string repeat 0 [expr {$len/4}]]

puts "=== load SAMPLE/PRELOAD (0x01), preload zeros ==="
scan_ir_hw_jtag 6 -tdi 01
set dr [scan_dr_hw_jtag $len -tdi $zeros]
puts "preload readback len = [string length $dr] nibbles"

puts "=== load EXTEST (0x26) ==="
scan_ir_hw_jtag 6 -tdi 26
set dr [scan_dr_hw_jtag $len -tdi $zeros]
puts "EXTEST DR readback len = [string length $dr] nibbles"
puts "EXTEST DR (first 32 nibbles) = [string range $dr 0 31]"

close_hw_target
disconnect_hw_server

# Probe whether raw JTAG shift primitives work over our hw_server, by
# reading IDCODE through scan_ir/scan_dr_hw_jtag. If this prints
# 0362d093 we can do EXTEST boundary-scan connectivity (no bitstream).

open_hw_manager
connect_hw_server -url localhost:3121 -allow_non_jtag

set tgt ""
set tries 0
while {$tgt eq "" && $tries < 20} {
    catch {refresh_hw_server -force_poll}
    foreach t [get_hw_targets -quiet] { if {$t ne ""} { set tgt $t; break } }
    if {$tgt eq ""} { after 1000; incr tries }
}
puts "target = $tgt"
current_hw_target $tgt
open_hw_target $tgt -jtag_mode true
puts "opened in jtag_mode"

# Raw JTAG: IR length on 7-series = 6. IDCODE opcode = 001001.
# Run-test/idle then shift IR=IDCODE, then shift 32 bits of DR.
# Raw JTAG: IR length on 7-series = 6. IDCODE opcode = 001001 (0x09).
# scan_ir/scan_dr_hw_jtag operate on the open hw_target's TAP directly.
puts "=== trying scan_ir_hw_jtag IDCODE (0x09) ==="
if {[catch {
    scan_ir_hw_jtag 6 -tdi 09
    set dr [scan_dr_hw_jtag 32 -tdi 00000000]
    puts "IDCODE DR = $dr"
} err]} {
    puts "scan_*_hw_jtag failed: $err"
}

close_hw_target
disconnect_hw_server

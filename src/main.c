/*
  main.c - An embedded CNC Controller with rs274/ngc (g-code) support
  Part of Grbl

  Copyright (c) 2011-2016 Sungeun K. Jeon for Gnea Research LLC
  Copyright (c) 2009-2011 Simen Svale Skogsrud

  Grbl is free software: you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

  Grbl is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with Grbl.  If not, see <http://www.gnu.org/licenses/>.
*/

#include "grbl.h"


// Declare system global variable structure
system_t sys;
int32_t sys_position[N_AXIS];      // Real-time machine (aka home) position vector in steps.
int32_t sys_probe_position[N_AXIS]; // Last probe position in machine coordinates and steps.
volatile uint8_t sys_probe_state;   // Probing state value.  Used to coordinate the probing cycle with stepper ISR.
volatile uint8_t sys_rt_exec_state;   // Global realtime executor bitflag variable for state management. See EXEC bitmasks.
volatile uint8_t sys_rt_exec_alarm;   // Global realtime executor bitflag variable for setting various alarms.
volatile uint8_t sys_rt_exec_motion_override; // Global realtime executor bitflag variable for motion-based overrides.
volatile uint8_t sys_rt_exec_accessory_override; // Global realtime executor bitflag variable for spindle/coolant overrides.
volatile uint8_t last_mcusr; // Saved MCUSR register for reset cause diagnostics.
#ifdef DEBUG
  volatile uint8_t sys_rt_exec_debug;
#endif


// Prints the caller of the last soft reset (RESET_SRC_*). Shared by the
// [MSG:RST:SRC:...] line and the [MSG:BOOT:src=...] line.
static void print_reset_source(uint8_t src)
{
  switch (src) {
    case RESET_SRC_LLP:        printPgmString(PSTR("LLP")); break;
    case RESET_SRC_A0:         printPgmString(PSTR("A0")); break;
    case RESET_SRC_RST:        printPgmString(PSTR("RST")); break;
    case RESET_SRC_HARD_LIMIT: printPgmString(PSTR("HARD_LIMIT")); break;
    case RESET_SRC_SOFT_LIMIT: printPgmString(PSTR("SOFT_LIMIT")); break;
    case RESET_SRC_HOMING:     printPgmString(PSTR("HOMING")); break;
    case RESET_SRC_OTHER:      printPgmString(PSTR("OTHER")); break;
    default:                   printPgmString(PSTR("NONE")); break;
  }
}


// Estimates Vcc (mV) from the internal 1.1 V bandgap measured against AVcc:
// Vcc = BANDGAP_MV * 1024 / adc. Returns 0 when the ADC does not answer within
// the bounded wait or the conversion is out of range, so a dead ADC cannot hang
// the boot. This is the only ADC user in the firmware (the probe is digital) and
// it leaves A0-A2, Timer2 and the buffers untouched.
static uint16_t measure_vcc_mv(void)
{
  ADMUX = (1 << REFS0) | (1 << MUX3) | (1 << MUX2) | (1 << MUX1); // 1.1 V bandgap, AVcc ref
  // ADEN + /128 (125 kHz @ 16 MHz) + start. The wait budget is shared by both
  // conversions: 20000 iterations is far more than the ~104 us a conversion takes
  // at 125 kHz, and an unresponsive ADC still cannot hang the boot.
  uint16_t guard = 20000;
  ADCSRA = (1 << ADEN) | (1 << ADPS2) | (1 << ADPS1) | (1 << ADPS0) | (1 << ADSC);
  while ((ADCSRA & (1 << ADSC)) && guard) { guard--; } // first MUX change: discarded
  ADCSRA = (1 << ADEN) | (1 << ADPS2) | (1 << ADPS1) | (1 << ADPS0) | (1 << ADSC);
  while ((ADCSRA & (1 << ADSC)) && guard) { guard--; }
  uint16_t adc = ADC;
  ADCSRA = 0; // nobody else uses the ADC; leave it off
  // ADIF may be left set (it is cleared by writing 1): a future ADC user must
  // clear it before enabling ADIE.

  // guard == 0 means the ADC timed out. A usable reading is 200..1022 => Vcc <=
  // ~5.63 V with BANDGAP_MV=1100; this rejects both the uint16_t wrap (adc <= 17)
  // and physically impossible rails.
  if (!guard || (uint16_t)(adc - 200) > 822) { return 0; }
  return (uint16_t)((uint32_t)BANDGAP_MV * 1024UL / adc);
}


int main(void)
{
  // Save MCU reset cause before anything clears MCUSR, then clear it for next event.
  // NOTE: the Arduino Uno bootloader (optiboot) reads and clears MCUSR before
  // jumping to the sketch, so on a stock Uno this is always 0 and every reset
  // reads as "SFR". The .noinit counters reported as [MSG:BOOT:...] are the
  // reliable signal on those boards.
  last_mcusr = MCUSR;
  MCUSR = 0;

  // Restore the diagnostics that survived the reset (see system.c). Returns 1
  // when RAM was preserved, i.e. this restart was not a power-on.
  uint8_t ram_preserved = system_diag_boot_init();
  // Peak stack usage of the previous life, measured on the painted region.
  // Meaningless after a cold start (the pattern is gone), so report '?'.
  uint16_t stk_peak = ram_preserved ? system_diag_stack_peak() : 0;
  system_diag_stack_paint();

  // Initialize system upon power-up.
  serial_init();   // Setup serial baud rate and interrupts
  settings_init(); // Load Grbl settings from EEPROM
  stepper_init();  // Configure stepper pins and interrupt timers
  system_init();   // Configure pinout pins and pin-change interrupt

  memset(sys_position,0,sizeof(sys_position)); // Clear machine position.
  sei(); // Enable interrupts

  // Initialize system state.
  #ifdef FORCE_INITIALIZATION_ALARM
    // Force Grbl into an ALARM state upon a power-cycle or hard reset.
    sys.state = STATE_ALARM;
  #else
    sys.state = STATE_IDLE;
  #endif
  
  // Check for power-up and set system alarm if homing is enabled to force homing cycle
  // by setting Grbl's alarm state. Alarm locks out all g-code commands, including the
  // startup scripts, but allows access to settings and internal commands. Only a homing
  // cycle '$H' or kill alarm locks '$X' will disable the alarm.
  // NOTE: The startup script will run after successful completion of the homing cycle, but
  // not after disabling the alarm locks. Prevents motion startup blocks from crashing into
  // things uncontrollably. Very bad.
  #ifdef HOMING_INIT_LOCK
    if (bit_istrue(settings.flags,BITFLAG_HOMING_ENABLE)) { sys.state = STATE_ALARM; }
  #endif

  // Report what survived the reset. Printed once per CPU reset: a soft reset
  // only re-runs the for(;;) loop below and reports itself with [MSG:RST:...].
  //   n   = restarts since the last power-on (1 on a cold start)
  //   m   = "ok" when RAM was preserved (not a power-on), "wipe" otherwise
  //   up  = uptime of the last main-loop pass of the previous life (ms)
  //   vcc = Vcc estimate from the internal bandgap (see measure_vcc_mv), '?' when
  //         the ADC read is not usable
  printPgmString(PSTR("[MSG:BOOT:n="));
  print_uint32_base10(system_diag_boot_count());
  printPgmString(PSTR(" m="));
  printPgmString(ram_preserved ? PSTR("ok") : PSTR("wipe"));
  printPgmString(PSTR(" up="));
  print_uint32_base10(system_diag_last_loop_ms());
  printPgmString(PSTR("]\r\n"));
  // Peak stack usage vs. the total stack area (bytes). If peak == limit the
  // stack reached the diagnostics (overflow into .noinit).
  printPgmString(PSTR("[MSG:BOOT:stk="));
  if (ram_preserved) { print_uint32_base10(stk_peak); }
  else { printPgmString(PSTR("?")); }
  printPgmString(PSTR(" lim="));
  print_uint32_base10(system_diag_stack_limit());
  printPgmString(PSTR("]\r\n"));
  printPgmString(PSTR("[MSG:BOOT:src="));
  print_reset_source(last_reset_source);
  printPgmString(PSTR(" ca="));
  print_uint8_base10(a0_transient_count);
  printPgmString(PSTR("]\r\n"));
  // Vcc estimate from the internal bandgap: a brown-out reads well below nominal,
  // a crash should not. '?' when the ADC read is not usable.
  uint16_t vcc_mv = measure_vcc_mv();
  printPgmString(PSTR("[MSG:BOOT:vcc="));
  if (vcc_mv) { printFloat(vcc_mv / 1000.0f, 2); }
  else { printPgmString(PSTR("?")); }
  printPgmString(PSTR("]\r\n"));

  // Grbl initialization loop upon power-up or a system abort. For the latter, all processes
  // will return to this loop to be cleanly re-initialized.
  for(;;) {

    // Reset system variables.
    uint8_t prior_state = sys.state;
    memset(&sys, 0, sizeof(system_t)); // Clear system struct variable.
    sys.state = prior_state;
    sys.f_override = DEFAULT_FEED_OVERRIDE;  // Set to 100%
    sys.r_override = DEFAULT_RAPID_OVERRIDE; // Set to 100%
    sys.spindle_speed_ovr = DEFAULT_SPINDLE_SPEED_OVERRIDE; // Set to 100%
		memset(sys_probe_position,0,sizeof(sys_probe_position)); // Clear probe position.
    sys_probe_state = 0;
    sys_rt_exec_state = 0;
    sys_rt_exec_alarm = 0;
    sys_rt_exec_motion_override = 0;
    sys_rt_exec_accessory_override = 0;
    #ifndef ENABLE_SAFETY_DOOR_INPUT_PIN
      // Drop a feed hold armed before this (soft) reset: it would otherwise be
      // confirmed in the new life and park the machine in HOLD unprompted.
      ctrl_hold_pending = 0;
    #endif

    // Reset Grbl primary systems.
    serial_reset_read_buffer(); // Clear serial read buffer
    gc_init(); // Set g-code parser to default state
    spindle_init();
    coolant_init();
    limits_init();
    probe_init();
    plan_reset(); // Clear block buffer and planner variables
    st_reset(); // Clear stepper subsystem variables.

    // Sync cleared gcode and planner positions to current system position.
    plan_sync_position();
    gc_sync_position();

    // Print welcome message. Indicates an initialization has occured at power-up or with a reset.
    report_init_message();

    // Print reset cause for diagnostics. MCUSR was captured at power-up; cleared afterwards
    // so subsequent soft resets through the main loop are reported as SFR.
    if (last_mcusr & (1<<BORF)) { printPgmString(PSTR("[MSG:RST:BOR]\r\n")); }
    else if (last_mcusr & (1<<EXTRF)) { printPgmString(PSTR("[MSG:RST:EXT]\r\n")); }
    else if (last_mcusr & (1<<WDRF)) { printPgmString(PSTR("[MSG:RST:WDR]\r\n")); }
    else if (last_mcusr & (1<<PORF)) { printPgmString(PSTR("[MSG:RST:POR]\r\n")); }
    else {
      printPgmString(PSTR("[MSG:RST:SFR]\r\n"));
      // Only a bare soft reset needs a source annotation. Hardware resets
      // (BOR/EXT/WDR/POR) are already self-explanatory.
      printPgmString(PSTR("[MSG:RST:SRC:"));
      print_reset_source(last_reset_source);
      printPgmString(PSTR("]\r\n"));
    }
    last_mcusr = 0;
    last_reset_source = RESET_SRC_NONE;

    // Start Grbl main loop. Processes program inputs and executes them.
    protocol_main_loop();

  }
  return 0;   /* Never reached */
}

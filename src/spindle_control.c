/*
  spindle_control.c - Minimal stub for PCB milling (no spindle control)
  Part of grbl-llp
*/

#include "grbl.h"

void spindle_init() { }
uint8_t spindle_get_state() { return SPINDLE_STATE_DISABLE; }
void spindle_stop() { }

#ifdef VARIABLE_SPINDLE
  void spindle_set_speed(uint8_t pwm_value) { (void)pwm_value; }
  uint8_t spindle_compute_pwm_value(float rpm) { (void)rpm; return 0; }
  void spindle_set_state(uint8_t state, float rpm) { (void)state; (void)rpm; }
  void spindle_sync(uint8_t state, float rpm) { (void)state; (void)rpm; }
#else
  void _spindle_set_state(uint8_t state) { (void)state; }
  void _spindle_sync(uint8_t state) { (void)state; }
#endif

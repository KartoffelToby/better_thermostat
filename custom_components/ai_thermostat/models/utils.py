class cleanState:
  def __init__(self, current_temperature, local_temperature,local_temperature_calibration,system_mode,has_real_mode = True, calibration = 0):
    self.current_temperature = current_temperature
    self.local_temperature = local_temperature
    self.local_temperature_calibration = local_temperature_calibration
    self.system_mode = system_mode
    self.has_real_mode = has_real_mode
    self.calibration = calibration
import os
import glob
import time
import RPi.GPIO as GPIO
import threading
from flask import Flask, render_template, jsonify, request, redirect, url_for, send_file # Changed render_template_string
import collections # For deque
import csv         # For CSV logging
import json        # For settings persistence
import datetime    # For daily stats reset

# --- Configuration File ---
SETTINGS_FILE = 'solar_heater_settings.json'

# --- Default Settings (used if settings file is missing or invalid) ---
DEFAULT_SETTINGS = {
    "INLET_SENSOR_ID": "28-xxxxxxxxxxxx",
    "OUTLET_SENSOR_ID": "28-xxxxxxxxxxxx",
    "PUMP_PWM_PIN": 18,
    "PWM_FREQUENCY": 100,
    "MIN_PUMP_SPEED": 20,
    "MAX_PUMP_SPEED": 100,
    "PUMP_SPEED_STEP": 5,
    "STABILIZATION_TIME_S": 45,
    "LOOP_INTERVAL_S": 300,
    "DELTA_T_ON": 4.0,
    "DELTA_T_OFF": 1.5,
    "MIN_INLET_TEMP_TO_RUN": 10.0,
    "MAX_OUTLET_TEMP_CUTOFF": 75.0,
    "MAX_PUMP_FLOW_RATE_LPM": 20.0,
    "LOG_SAVE_INTERVAL_S": 300,
    "TEMPERATURE_LOG_FILE": "temperature_log.csv",
    "MAX_HISTORY_POINTS": 150,
    "MAX_HISTORY_TABLE_ROWS": 200,
    "CONTROL_MODE": "auto",
    "MANUAL_PUMP_SPEED_SETTING": 30,
    "ENABLE_HARDWARE_WATCHDOG": False,
    "WATCHDOG_KICK_INTERVAL_S": 30,
    "WATCHDOG_DEVICE": "/dev/watchdog",
    "DISPLAY_TEMP_UNIT": "C"
}
# --- Active Settings (loaded from file or defaults) ---
current_settings = DEFAULT_SETTINGS.copy()

# --- Constants ---
BASE_DIR = '/sys/bus/w1/devices/'
SPECIFIC_HEAT_CAPACITY_WATER = 4186  # J/kg°C
WATER_DENSITY_KG_L = 1.0  # kg/L (approx)

# --- Application State & Data ---
app_status = {
    "inlet_temp_display": "N/A", 
    "outlet_temp_display": "N/A",
    "delta_t_display": "N/A",    
    "pump_speed": 0,
    "target_pump_speed": 0,
    "thermal_power_watts": "N/A",
    "system_message": "Initializing...",
    "optimal_pump_speed_found": "N/A",
    "max_delta_t_found_display": "N/A", 
    "control_mode": DEFAULT_SETTINGS["CONTROL_MODE"],
    "pump_on_time_today_s": 0.0,
    "energy_harvested_today_wh": 0.0,
    "last_stats_reset_date": datetime.date.today().isoformat(),
    "display_temp_unit_symbol": "°C", 
    "last_update": time.strftime("%Y-%m-%d %H:%M:%S")
}
temperature_history = collections.deque(maxlen=DEFAULT_SETTINGS["MAX_HISTORY_POINTS"]) 
log_buffer = [] 
data_lock = threading.RLock()

# --- Globals ---
inlet_sensor_file = None
outlet_sensor_file = None
pwm_pump = None
control_thread_running = False
watchdog_fd = None

# --- Temperature Conversion ---
def celsius_to_fahrenheit(temp_c):
    if temp_c is None: return None
    return (temp_c * 9/5) + 32

def convert_temp_for_display(temp_c, target_unit):
    if temp_c is None: return "N/A"
    if not isinstance(temp_c, (float, int)): return "Error" 
    
    if target_unit == "F":
        return f"{celsius_to_fahrenheit(temp_c):.1f}" 
    return f"{temp_c:.2f}" 

# --- Settings Load/Save Functions ---
def load_settings():
    global current_settings, temperature_history, app_status
    print(f"Attempting to load settings from {SETTINGS_FILE}...")
    try:
        with open(SETTINGS_FILE, 'r') as f:
            loaded_s = json.load(f)
            temp_settings = DEFAULT_SETTINGS.copy()
            for key in DEFAULT_SETTINGS.keys():
                if key in loaded_s:
                    value_from_file = loaded_s[key]
                    default_val_type = type(DEFAULT_SETTINGS[key])
                    try:
                        converted_value = None
                        if default_val_type == bool:
                            converted_value = str(value_from_file).lower() in ['true', 'on', '1', 'yes', 'checked']
                        elif default_val_type == int:
                            converted_value = int(float(value_from_file))
                        elif default_val_type == float:
                            converted_value = float(value_from_file)
                        else: 
                            converted_value = str(value_from_file)
                        temp_settings[key] = converted_value
                    except (ValueError, TypeError):
                         print(f"Warning: Could not convert loaded setting '{key}' value '{value_from_file}' to {default_val_type}. Using default: {DEFAULT_SETTINGS[key]}.")
                         temp_settings[key] = DEFAULT_SETTINGS[key]
            current_settings = temp_settings
            print("Settings loaded successfully.")
    except FileNotFoundError:
        print(f"{SETTINGS_FILE} not found. Using default settings and creating file.")
        current_settings = DEFAULT_SETTINGS.copy(); save_settings()
    except json.JSONDecodeError:
        print(f"Error decoding JSON from {SETTINGS_FILE}. Using default settings and overwriting.")
        current_settings = DEFAULT_SETTINGS.copy(); save_settings()
    except Exception as e:
        print(f"Error loading settings: {e}. Using default settings.")
        current_settings = DEFAULT_SETTINGS.copy()
    
    with data_lock:
        app_status["optimal_pump_speed_found"] = current_settings.get("MIN_PUMP_SPEED", DEFAULT_SETTINGS["MIN_PUMP_SPEED"])
        app_status["control_mode"] = current_settings.get("CONTROL_MODE", DEFAULT_SETTINGS["CONTROL_MODE"])
        app_status["display_temp_unit_symbol"] = "°F" if current_settings.get("DISPLAY_TEMP_UNIT", "C") == "F" else "°C"
        if app_status["control_mode"] == "manual":
            app_status["target_pump_speed"] = current_settings.get("MANUAL_PUMP_SPEED_SETTING", DEFAULT_SETTINGS["MANUAL_PUMP_SPEED_SETTING"])
        else:
            app_status["target_pump_speed"] = 0
    
    max_hist_points = current_settings.get("MAX_HISTORY_POINTS", DEFAULT_SETTINGS["MAX_HISTORY_POINTS"])
    if not isinstance(max_hist_points, int) or max_hist_points <= 0:
        max_hist_points = DEFAULT_SETTINGS["MAX_HISTORY_POINTS"]
        current_settings["MAX_HISTORY_POINTS"] = max_hist_points
    if temperature_history.maxlen != max_hist_points:
        temperature_history = collections.deque(maxlen=max_hist_points)
        print(f"Graph history points reconfigured to: {max_hist_points}")

def save_settings():
    print(f"Attempting to save settings to {SETTINGS_FILE}...")
    try:
        with data_lock: settings_to_save = current_settings.copy()
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings_to_save, f, indent=4)
        print("Settings saved successfully.")
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False

# --- Hardware Watchdog Functions ---
def setup_watchdog():
    global watchdog_fd
    if current_settings.get("ENABLE_HARDWARE_WATCHDOG", False):
        try:
            watchdog_device = current_settings.get("WATCHDOG_DEVICE", "/dev/watchdog")
            watchdog_fd = os.open(watchdog_device, os.O_WRONLY)
            print(f"Hardware watchdog {watchdog_device} opened.")
        except Exception as e:
            print(f"Error opening hardware watchdog {watchdog_device}: {e}. Watchdog disabled.")
            watchdog_fd = None
            with data_lock: current_settings["ENABLE_HARDWARE_WATCHDOG"] = False

def kick_watchdog():
    if watchdog_fd is not None:
        try: os.write(watchdog_fd, b'V')
        except Exception as e: print(f"Error kicking hardware watchdog: {e}")

def close_watchdog():
    global watchdog_fd
    if watchdog_fd is not None:
        try: os.close(watchdog_fd); print("Hardware watchdog closed.")
        except Exception as e: print(f"Error closing hardware watchdog: {e}")
        watchdog_fd = None

# --- Sensor Functions ---
def discover_sensors():
    global inlet_sensor_file, outlet_sensor_file
    try:
        inlet_id = current_settings["INLET_SENSOR_ID"]
        outlet_id = current_settings["OUTLET_SENSOR_ID"]
        if "x" in inlet_id.lower() or "x" in outlet_id.lower():
            raise KeyError("Default/placeholder sensor IDs are still in use. Please configure them in Settings.")
        inlet_device_folder = glob.glob(BASE_DIR + inlet_id)[0]
        inlet_sensor_file = inlet_device_folder + '/w1_slave'
        outlet_device_folder = glob.glob(BASE_DIR + outlet_id)[0]
        outlet_sensor_file = outlet_device_folder + '/w1_slave'
        update_status(system_message="Sensors discovered successfully.")
        return True
    except IndexError:
        errmsg = f"Error: Sensor(s) not found. Check IDs in settings: Inlet='{current_settings.get('INLET_SENSOR_ID', 'N/A')}', Outlet='{current_settings.get('OUTLET_SENSOR_ID', 'N/A')}' and 1-Wire setup."
        update_status(system_message=errmsg); return False
    except KeyError as e:
        errmsg = f"Error: Sensor ID key missing or invalid: {e}."
        update_status(system_message=errmsg); return False

def read_temp_raw(sensor_file_path):
    if not sensor_file_path: return None
    try:
        with open(sensor_file_path, 'r') as f: lines = f.readlines()
        return lines
    except: return None 

def read_temp_c(sensor_file_path): # Always returns Celsius
    lines = read_temp_raw(sensor_file_path)
    if not lines: return None
    read_attempts = 3
    while read_attempts > 0:
        if lines and lines[0].strip().endswith('YES'):
            equals_pos = lines[1].find('t=')
            if equals_pos != -1:
                try: return float(lines[1][equals_pos+2:]) / 1000.0
                except ValueError: return None 
            else: break 
        time.sleep(0.2); lines = read_temp_raw(sensor_file_path); read_attempts -= 1
    return None

# --- PWM Pump Functions ---
def setup_pwm():
    global pwm_pump
    GPIO.setwarnings(False); GPIO.setmode(GPIO.BCM)
    GPIO.setup(current_settings["PUMP_PWM_PIN"], GPIO.OUT)
    if pwm_pump: pwm_pump.stop()
    pwm_pump = GPIO.PWM(current_settings["PUMP_PWM_PIN"], current_settings["PWM_FREQUENCY"])
    pwm_pump.start(0)
    update_status(pump_speed=0, target_pump_speed=0, system_message="PWM Initialized. Pump is OFF.")

def set_pump_speed(speed_percent_target):
    global pwm_pump
    if pwm_pump is None:
        update_status(system_message="Error: PWM not initialized."); return
    target_speed = max(0, min(100, speed_percent_target))
    actual_duty_cycle = 0
    if target_speed > 0:
        actual_duty_cycle = max(current_settings["MIN_PUMP_SPEED"], min(current_settings["MAX_PUMP_SPEED"], target_speed))
    if pwm_pump: pwm_pump.ChangeDutyCycle(float(actual_duty_cycle))
    else: print("Error: pwm_pump object is None in set_pump_speed.")
    update_status(pump_speed=actual_duty_cycle, target_pump_speed=target_speed)

def stop_pump():
    set_pump_speed(0)
    update_status(system_message="Pump stopped.", thermal_power_watts="N/A")

# --- Thermal Power & Statistics ---
def calculate_estimated_thermal_power(delta_t_celsius, current_actual_pump_speed_percent):
    if delta_t_celsius is None or current_actual_pump_speed_percent is None or current_actual_pump_speed_percent == 0: return None
    estimated_flow_lps = (float(current_actual_pump_speed_percent) / 100.0) * (current_settings["MAX_PUMP_FLOW_RATE_LPM"] / 60.0)
    return (estimated_flow_lps * WATER_DENSITY_KG_L) * SPECIFIC_HEAT_CAPACITY_WATER * delta_t_celsius

def update_daily_stats(pump_is_on_flag, seconds_elapsed, current_power_watts):
    with data_lock:
        today_iso = datetime.date.today().isoformat()
        if app_status.get("last_stats_reset_date") != today_iso:
            app_status["pump_on_time_today_s"] = 0.0
            app_status["energy_harvested_today_wh"] = 0.0
            app_status["last_stats_reset_date"] = today_iso
            print(f"Daily statistics reset for {today_iso}")
        if pump_is_on_flag:
            app_status["pump_on_time_today_s"] += seconds_elapsed
            if current_power_watts is not None and isinstance(current_power_watts, (float, int)) and current_power_watts > 0:
                app_status["energy_harvested_today_wh"] += (current_power_watts * seconds_elapsed) / 3600.0

# --- CSV Logging ---
def write_log_buffer_to_csv():
    global log_buffer
    with data_lock:
        if not log_buffer: return
        data_to_write = list(log_buffer); log_buffer.clear()
    if not data_to_write: return
    log_file = current_settings.get("TEMPERATURE_LOG_FILE", DEFAULT_SETTINGS["TEMPERATURE_LOG_FILE"])
    file_exists = os.path.isfile(log_file)
    try:
        with open(log_file, 'a', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=['timestamp', 'inlet_temp_c', 'outlet_temp_c']) 
            if not file_exists or csvfile.tell() == 0: writer.writeheader()
            writer.writerows(data_to_write)
        print(f"Wrote {len(data_to_write)} entries to {log_file}")
    except Exception as e: print(f"Error CSV writing: {e}")

# --- Status Update ---
def update_status_and_history(inlet_temp_c=None, outlet_temp_c=None, delta_t_c=None, **kwargs):
    global log_buffer
    with data_lock:
        display_unit = current_settings.get("DISPLAY_TEMP_UNIT", "C")
        current_time_str_graph, full_timestamp_log = time.strftime("%H:%M:%S"), time.strftime("%Y-%m-%d %H:%M:%S")
        status_updates = kwargs.copy()

        status_updates["inlet_temp_display"] = convert_temp_for_display(inlet_temp_c, display_unit)
        status_updates["outlet_temp_display"] = convert_temp_for_display(outlet_temp_c, display_unit)
        
        calculated_delta_t_c = None
        if isinstance(inlet_temp_c, float) and isinstance(outlet_temp_c, float):
            calculated_delta_t_c = outlet_temp_c - inlet_temp_c
        elif isinstance(delta_t_c, float): 
            calculated_delta_t_c = delta_t_c
        
        status_updates["delta_t_display"] = convert_temp_for_display(calculated_delta_t_c, display_unit)
        
        actual_pump_speed = float(app_status.get("pump_speed", 0)) 
        power_w = calculate_estimated_thermal_power(calculated_delta_t_c, actual_pump_speed)
        status_updates["thermal_power_watts"] = f"{power_w:.1f}" if power_w is not None else "N/A"
            
        for key, value in status_updates.items():
            if key in app_status: app_status[key] = value
        app_status["last_update"] = full_timestamp_log
        app_status["display_temp_unit_symbol"] = "°F" if display_unit == "F" else "°C"

        if isinstance(inlet_temp_c, float) and isinstance(outlet_temp_c, float):
            temperature_history.append({"time": current_time_str_graph, 
                                        "inlet_c": round(inlet_temp_c, 2) if inlet_temp_c is not None else None,
                                        "outlet_c": round(outlet_temp_c, 2) if outlet_temp_c is not None else None})
            log_buffer.append({"timestamp": full_timestamp_log, "inlet_temp_c": round(inlet_temp_c, 2), "outlet_temp_c": round(outlet_temp_c, 2)})
        elif not any(k in kwargs for k in ["pump_speed", "system_message", "target_pump_speed"]):
             temperature_history.append({"time": current_time_str_graph, "inlet_c": None, "outlet_c": None})

def update_status(**kwargs): 
    with data_lock:
        for key, value in kwargs.items():
            if key in app_status:
                if isinstance(value, float) and key not in ["inlet_temp_display", "outlet_temp_display", "delta_t_display", "thermal_power_watts", "pump_on_time_today_s", "energy_harvested_today_wh", "max_delta_t_found_display"]:
                    app_status[key] = f"{value:.2f}" 
                else: app_status[key] = value
        app_status["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
        app_status["display_temp_unit_symbol"] = "°F" if current_settings.get("DISPLAY_TEMP_UNIT", "C") == "F" else "°C"

# --- Main Control Logic ---
def optimize_pump_speed():
    with data_lock: last_optimal_raw = app_status.get("optimal_pump_speed_found", "N/A")
    try: last_optimal_speed = int(float(last_optimal_raw)) if last_optimal_raw != "N/A" else None
    except ValueError: last_optimal_speed = None
    start_speed = current_settings["MIN_PUMP_SPEED"]
    if last_optimal_speed and current_settings["MIN_PUMP_SPEED"] <= last_optimal_speed <= current_settings["MAX_PUMP_SPEED"]:
        start_speed = max(current_settings["MIN_PUMP_SPEED"], last_optimal_speed - 2 * current_settings["PUMP_SPEED_STEP"])
    
    update_status_and_history(system_message="Optimizing pump speed...") 
    current_max_delta_t_c_this_cycle, current_optimal_speed_this_cycle = -100.0, start_speed
    initial_inlet_temp_c = read_temp_c(inlet_sensor_file)

    if initial_inlet_temp_c is None or initial_inlet_temp_c < current_settings["MIN_INLET_TEMP_TO_RUN"]:
        msg = f"Opt aborted: Inlet ({convert_temp_for_display(initial_inlet_temp_c, current_settings.get('DISPLAY_TEMP_UNIT','C'))}{app_status['display_temp_unit_symbol']}) < {convert_temp_for_display(current_settings['MIN_INLET_TEMP_TO_RUN'], current_settings.get('DISPLAY_TEMP_UNIT','C'))}{app_status['display_temp_unit_symbol']}."
        stop_pump(); update_status_and_history(inlet_temp_c=initial_inlet_temp_c, system_message=msg); return
    
    speeds_to_check = sorted(list(set(list(range(start_speed, current_settings["MAX_PUMP_SPEED"] + 1, current_settings["PUMP_SPEED_STEP"])) + 
                                     list(range(current_settings["MIN_PUMP_SPEED"], start_speed, current_settings["PUMP_SPEED_STEP"])))))
    for speed_to_test in speeds_to_check:
        if not control_thread_running: return
        set_pump_speed(speed_to_test) 
        update_status(system_message=f"Optimizing: Stabilizing at {speed_to_test}%...")
        stabilization_s = current_settings["STABILIZATION_TIME_S"]
        for _ in range(stabilization_s):
            if not control_thread_running: return
            time.sleep(1)
        in_temp_c, out_temp_c, delta_t_c_val = read_temp_c(inlet_sensor_file), read_temp_c(outlet_sensor_file), None
        if in_temp_c and out_temp_c: delta_t_c_val = out_temp_c - in_temp_c
        update_status_and_history(inlet_temp_c=in_temp_c, outlet_temp_c=out_temp_c, delta_t_c=delta_t_c_val, system_message=f"Optimizing: Tested {speed_to_test}%")
        if in_temp_c and out_temp_c:
            if delta_t_c_val > current_max_delta_t_c_this_cycle:
                current_max_delta_t_c_this_cycle, current_optimal_speed_this_cycle = delta_t_c_val, speed_to_test
            if out_temp_c > current_settings["MAX_OUTLET_TEMP_CUTOFF"]:
                msg = f"SAFETY: Outlet {convert_temp_for_display(out_temp_c, current_settings.get('DISPLAY_TEMP_UNIT','C'))}{app_status['display_temp_unit_symbol']} > {convert_temp_for_display(current_settings['MAX_OUTLET_TEMP_CUTOFF'], current_settings.get('DISPLAY_TEMP_UNIT','C'))}{app_status['display_temp_unit_symbol']}. Stopping."
                stop_pump(); update_status_and_history(outlet_temp_c=out_temp_c, system_message=msg); return
    
    if current_max_delta_t_c_this_cycle >= current_settings["DELTA_T_OFF"]: 
        with data_lock:
            app_status["max_delta_t_found_display"] = convert_temp_for_display(current_max_delta_t_c_this_cycle, current_settings.get("DISPLAY_TEMP_UNIT","C"))
            app_status["optimal_pump_speed_found"] = current_optimal_speed_this_cycle
        msg = f"Opt complete. Optimal: {current_optimal_speed_this_cycle}% (ΔT: {app_status['max_delta_t_found_display']}{app_status['display_temp_unit_symbol']})."
        set_pump_speed(current_optimal_speed_this_cycle)
        final_in_c, final_out_c, final_dt_c = read_temp_c(inlet_sensor_file), read_temp_c(outlet_sensor_file), None
        if final_in_c and final_out_c: final_dt_c = final_out_c - final_in_c
        update_status_and_history(inlet_temp_c=final_in_c, outlet_temp_c=final_out_c, delta_t_c=final_dt_c, system_message=msg)
    else:
        msg = f"Opt: No speed yielded ΔT >= {convert_temp_for_display(current_settings['DELTA_T_OFF'], current_settings.get('DISPLAY_TEMP_UNIT','C'))}{app_status['display_temp_unit_symbol']}. Stopping."
        with data_lock: app_status["max_delta_t_found_display"] = convert_temp_for_display(current_max_delta_t_c_this_cycle if current_max_delta_t_c_this_cycle > -100 else None, current_settings.get("DISPLAY_TEMP_UNIT","C"))
        stop_pump(); update_status(system_message=msg)

def control_logic_thread_func():
    global control_thread_running
    load_settings()
    if not discover_sensors(): control_thread_running = False; return
    setup_pwm(); time.sleep(1)
    if current_settings.get("ENABLE_HARDWARE_WATCHDOG", False): setup_watchdog()
    last_control_cycle_time = time.time() - current_settings["LOOP_INTERVAL_S"] 
    last_log_save_time, last_watchdog_kick_time = time.time(), time.time()
    with data_lock:
        app_status["last_stats_reset_date"] = datetime.date.today().isoformat()
        app_status["pump_on_time_today_s"], app_status["energy_harvested_today_wh"] = 0.0, 0.0
    while control_thread_running:
        current_time = time.time()
        loop_interval = current_settings["LOOP_INTERVAL_S"]
        log_interval = current_settings["LOG_SAVE_INTERVAL_S"]
        watchdog_kick_interval = current_settings.get("WATCHDOG_KICK_INTERVAL_S", 30)
        
        pump_speed_val, power_val_watts = 0.0, 0.0
        with data_lock:
            try:
                pump_speed_val = float(app_status.get("pump_speed", 0.0))
                power_str = app_status.get("thermal_power_watts", "N/A")
                if power_str != "N/A": power_val_watts = float(power_str)
            except ValueError: pass
        update_daily_stats(pump_is_on_flag=(pump_speed_val > 0), seconds_elapsed=1, current_power_watts=power_val_watts)

        with data_lock: control_mode = current_settings.get("CONTROL_MODE", "auto")
        
        if control_mode == "manual":
            with data_lock: manual_target_speed = current_settings.get("MANUAL_PUMP_SPEED_SETTING", 0)
            set_pump_speed(manual_target_speed)
            if (current_time - last_control_cycle_time) >= loop_interval: 
                in_temp_c, out_temp_c, dt_c = read_temp_c(inlet_sensor_file), read_temp_c(outlet_sensor_file), None
                if in_temp_c and out_temp_c: dt_c = out_temp_c - in_temp_c
                update_status_and_history(inlet_temp_c=in_temp_c, outlet_temp_c=out_temp_c, delta_t_c=dt_c,
                                          system_message=f"Manual: Target {app_status['target_pump_speed']}% (Actual: {app_status['pump_speed']}%).")
                last_control_cycle_time = current_time
        elif control_mode == "auto":
            if (current_time - last_control_cycle_time) >= loop_interval:
                update_status(system_message="Auto: Checking conditions...")
                inlet_temp_c, outlet_temp_c, dt_c_val = read_temp_c(inlet_sensor_file), read_temp_c(outlet_sensor_file), None
                if inlet_temp_c and outlet_temp_c: dt_c_val = outlet_temp_c - inlet_temp_c
                update_status_and_history(inlet_temp_c=inlet_temp_c, outlet_temp_c=outlet_temp_c, delta_t_c=dt_c_val, system_message="Auto: Checked conditions.")
                current_pump_on = float(app_status.get("pump_speed", "0")) > 0
                display_unit = current_settings.get("DISPLAY_TEMP_UNIT", "C")
                unit_symbol = "°F" if display_unit == "F" else "°C"

                if inlet_temp_c and outlet_temp_c:
                    if outlet_temp_c > current_settings["MAX_OUTLET_TEMP_CUTOFF"]:
                        msg = f"SAFETY: Outlet {convert_temp_for_display(outlet_temp_c, display_unit)}{unit_symbol} > {convert_temp_for_display(current_settings['MAX_OUTLET_TEMP_CUTOFF'], display_unit)}{unit_symbol}. Stopping."
                        stop_pump(); update_status(system_message=msg)
                    elif inlet_temp_c < current_settings["MIN_INLET_TEMP_TO_RUN"]:
                        msg = f"AUTO: Inlet {convert_temp_for_display(inlet_temp_c, display_unit)}{unit_symbol} < {convert_temp_for_display(current_settings['MIN_INLET_TEMP_TO_RUN'], display_unit)}{unit_symbol}. Pump OFF."
                        if current_pump_on: stop_pump()
                        update_status(system_message=msg)
                    elif current_pump_on and dt_c_val < current_settings["DELTA_T_OFF"]:
                        msg = f"AUTO: ΔT ({convert_temp_for_display(dt_c_val, display_unit)}{unit_symbol}) < DELTA_T_OFF ({convert_temp_for_display(current_settings['DELTA_T_OFF'], display_unit)}{unit_symbol}). Stopping."
                        stop_pump(); update_status(system_message=msg)
                    elif not current_pump_on and dt_c_val >= current_settings["DELTA_T_ON"]:
                        msg = f"AUTO: ΔT ({convert_temp_for_display(dt_c_val, display_unit)}{unit_symbol}) >= DELTA_T_ON ({convert_temp_for_display(current_settings['DELTA_T_ON'], display_unit)}{unit_symbol}). Optimizing..."
                        update_status(system_message=msg); optimize_pump_speed()
                    elif current_pump_on: 
                         msg = f"AUTO: ΔT ({convert_temp_for_display(dt_c_val, display_unit)}{unit_symbol}) OK. Re-optimizing..."
                         update_status(system_message=msg); optimize_pump_speed()
                    else:
                        msg = f"AUTO: ΔT ({convert_temp_for_display(dt_c_val, display_unit)}{unit_symbol}) insufficient. Pump OFF."
                        update_status(system_message=msg)
                else: 
                    errmsg = "AUTO: Sensor error during evaluation. Stopping pump."
                    stop_pump(); update_status(system_message=errmsg)
                last_control_cycle_time = current_time
        if (current_time - last_log_save_time) >= log_interval:
            write_log_buffer_to_csv(); last_log_save_time = current_time
        if current_settings.get("ENABLE_HARDWARE_WATCHDOG", False) and (current_time - last_watchdog_kick_time) >= watchdog_kick_interval:
            kick_watchdog(); last_watchdog_kick_time = current_time
        time.sleep(1)
    stop_pump(); update_status(system_message="Control thread stopped."); write_log_buffer_to_csv()
    if current_settings.get("ENABLE_HARDWARE_WATCHDOG", False): close_watchdog()

# --- Flask Web Application ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    with data_lock: current_display_status = app_status.copy()
    current_display_status['inlet_temp_str'] = f"{current_display_status['inlet_temp_display']} {current_display_status['display_temp_unit_symbol']}"
    current_display_status['outlet_temp_str'] = f"{current_display_status['outlet_temp_display']} {current_display_status['display_temp_unit_symbol']}"
    current_display_status['delta_t_str'] = f"{current_display_status['delta_t_display']} {current_display_status['display_temp_unit_symbol']}"
    current_display_status['max_delta_t_found_str'] = f"{current_display_status['max_delta_t_found_display']} {current_display_status['display_temp_unit_symbol']}" if current_display_status['max_delta_t_found_display'] != "N/A" else "N/A"
    return render_template('dashboard.html', status=current_display_status)

@flask_app.route('/check_update')
def check_update_route():
    with data_lock:
        # Ensure last_update is always a string, even if somehow not set initially
        last_update_timestamp = app_status.get("last_update", time.strftime("%Y-%m-%d %H:%M:%S"))
    return jsonify({"last_update": last_update_timestamp})


@flask_app.route('/graph_data')
def get_graph_data():
    with data_lock:
        display_unit = current_settings.get("DISPLAY_TEMP_UNIT", "C")
        unit_symbol = "°F" if display_unit == "F" else "°C"
        graph_data_points = []
        for point in list(temperature_history): # Iterate over a copy
            inlet_c_val = point.get("inlet_c")
            outlet_c_val = point.get("outlet_c")

            display_inlet = None
            display_outlet = None

            if inlet_c_val is not None:
                if display_unit == "F":
                    display_inlet = round(celsius_to_fahrenheit(inlet_c_val), 1)
                else: # Celsius
                    display_inlet = round(inlet_c_val, 2)

            if outlet_c_val is not None:
                if display_unit == "F":
                    display_outlet = round(celsius_to_fahrenheit(outlet_c_val), 1)
                else: # Celsius
                    display_outlet = round(outlet_c_val, 2)

            graph_data_points.append({
                "time": point.get("time"), "inlet": display_inlet, "outlet": display_outlet, "unit_symbol": unit_symbol
            })
        return jsonify(graph_data_points)

@flask_app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    global current_settings, temperature_history 
    message = request.args.get('message', None)
    if request.method == 'POST':
        try:
            settings_changed_overall = False; form_errors = []
            critical_settings_keys = ["INLET_SENSOR_ID", "OUTLET_SENSOR_ID", "PUMP_PWM_PIN", "WATCHDOG_DEVICE", "ENABLE_HARDWARE_WATCHDOG"]
            changed_critical_settings = []
            
            new_settings_candidate = current_settings.copy() 

            for key in DEFAULT_SETTINGS.keys(): 
                form_value = request.form.get(key)
                if form_value is not None: 
                    original_value_in_current = current_settings.get(key) 
                    try:
                        default_val_type = type(DEFAULT_SETTINGS[key])
                        converted_value = None
                        if default_val_type == bool: 
                            converted_value = str(form_value).lower() in ['true', 'on', '1', 'yes', 'checked']
                        elif default_val_type == float: 
                            converted_value = float(form_value)
                        elif default_val_type == int: 
                            converted_value = int(float(form_value))
                        else: 
                            converted_value = str(form_value)
                        
                        new_settings_candidate[key] = converted_value 
                        if original_value_in_current != converted_value: 
                            settings_changed_overall = True
                            if key in critical_settings_keys: 
                                changed_critical_settings.append(key.replace('_', ' ').title())
                    except ValueError:
                        form_errors.append(f"Invalid format for '{key.replace('_',' ').title()}'. Value '{form_value}' ignored.")
            
            if form_errors:
                message = "Please correct errors: " + " | ".join(form_errors)
            else: 
                if settings_changed_overall:
                    with data_lock: 
                        current_settings = new_settings_candidate 
                    
                    if save_settings(): 
                        message = "Settings updated and saved successfully."
                        if changed_critical_settings:
                            message += f" Critical settings ({', '.join(changed_critical_settings)}) changed. A manual script restart (sudo systemctl restart solarheater.service) is highly recommended."
                        
                        with data_lock: 
                            app_status["control_mode"] = current_settings["CONTROL_MODE"]
                            app_status["display_temp_unit_symbol"] = "°F" if current_settings.get("DISPLAY_TEMP_UNIT", "C") == "F" else "°C"
                            if current_settings["CONTROL_MODE"] == "manual":
                                app_status["target_pump_speed"] = current_settings["MANUAL_PUMP_SPEED_SETTING"]
                            else: 
                                app_status["target_pump_speed"] = 0 
                            if temperature_history.maxlen != current_settings["MAX_HISTORY_POINTS"]:
                                 temperature_history = collections.deque(maxlen=current_settings["MAX_HISTORY_POINTS"])
                                 print(f"Graph history points reconfigured to: {current_settings['MAX_HISTORY_POINTS']}")
                    else:
                        message = "Settings updated in memory, but failed to save to file."
                else:
                    message = "No changes detected in settings."
        except Exception as e:
            message = f"An unexpected error occurred while updating settings: {e}"
            print(f"Error in /settings POST: {e}")
        return redirect(url_for('settings_page', message=message))

    with data_lock: settings_to_display = current_settings.copy()
    return render_template('settings.html', settings=settings_to_display, message=message, DEFAULT_SETTINGS=DEFAULT_SETTINGS)

@flask_app.route('/history')
def history_page():
    message = request.args.get('message', None)
    log_data_preview = []
    log_file_name, display_unit_hist, unit_symbol_hist = "N/A", "C", "°C"
    try:
        with data_lock:
            log_file_name = current_settings.get("TEMPERATURE_LOG_FILE", DEFAULT_SETTINGS["TEMPERATURE_LOG_FILE"])
            max_rows = current_settings.get("MAX_HISTORY_TABLE_ROWS", DEFAULT_SETTINGS["MAX_HISTORY_TABLE_ROWS"])
            display_unit_hist = current_settings.get("DISPLAY_TEMP_UNIT", "C")
            unit_symbol_hist = "°F" if display_unit_hist == "F" else "°C"
        script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
        log_path = os.path.join(script_dir, log_file_name)
        if os.path.exists(log_path):
            with open(log_path, 'r', newline='') as csvfile:
                reader = csv.DictReader(csvfile)
                all_rows = list(reader)
                preview_rows_dicts = all_rows[-(max_rows):]
                if preview_rows_dicts:
                    log_data_preview.append(['Timestamp', f'Inlet Temp ({unit_symbol_hist})', f'Outlet Temp ({unit_symbol_hist})'])
                    for row_dict in preview_rows_dicts:
                        ts = row_dict.get('timestamp', 'N/A')
                        in_c_str, out_c_str = row_dict.get('inlet_temp_c', 'N/A'), row_dict.get('outlet_temp_c', 'N/A')
                        try:
                            in_c, out_c = (float(in_c_str) if in_c_str != 'N/A' else None), (float(out_c_str) if out_c_str != 'N/A' else None)
                            log_data_preview.append([ts, convert_temp_for_display(in_c, display_unit_hist), convert_temp_for_display(out_c, display_unit_hist)])
                        except ValueError: log_data_preview.append([ts, "Err", "Err"])
        else: message = f"Log file '{log_file_name}' not found."
    except Exception as e:
        message = f"Error reading log file: {e}"; print(f"Error on /history: {e}")
    return render_template('history.html', log_data_preview=log_data_preview, message=message, log_file_name=log_file_name, max_rows=current_settings.get("MAX_HISTORY_TABLE_ROWS", DEFAULT_SETTINGS["MAX_HISTORY_TABLE_ROWS"]), unit_symbol_hist=unit_symbol_hist)

@flask_app.route('/download_log')
def download_log():
    try:
        log_filename = current_settings.get("TEMPERATURE_LOG_FILE", DEFAULT_SETTINGS["TEMPERATURE_LOG_FILE"])
        script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
        log_path = os.path.join(script_dir, log_filename)
        if not os.path.isfile(log_path): return "Error: Log file not found.", 404
        return send_file(log_path, as_attachment=True, download_name=log_filename, mimetype='text/csv')
    except Exception as e: return f"Error sending log file: {e}", 500

# --- Re-added Flask routes for manual control ---
@flask_app.route('/set_control_mode', methods=['POST'])
def set_control_mode():
    global current_settings
    new_mode = request.form.get('control_mode')
    message = "No change in control mode."
    if new_mode in ['auto', 'manual']:
        with data_lock:
            if current_settings["CONTROL_MODE"] != new_mode:
                current_settings["CONTROL_MODE"] = new_mode
                app_status["control_mode"] = new_mode
                if new_mode == "manual":
                    app_status["target_pump_speed"] = current_settings["MANUAL_PUMP_SPEED_SETTING"]
                else: 
                    app_status["target_pump_speed"] = 0 
                save_settings() 
                message = f"Control mode set to {new_mode}."
                print(message)
            else:
                message = f"Control mode already {new_mode}."
    else:
        message = "Invalid control mode specified."
    return redirect(url_for('index', message=message))

@flask_app.route('/set_manual_pump_speed', methods=['POST'])
def set_manual_pump_speed_route():
    global current_settings
    message = "Failed to set manual speed."
    try:
        speed_str = request.form.get('manual_speed')
        if speed_str is not None:
            speed = int(speed_str)
            if 0 <= speed <= 100:
                with data_lock:
                    if current_settings["CONTROL_MODE"] == "manual":
                        current_settings["MANUAL_PUMP_SPEED_SETTING"] = speed
                        app_status["target_pump_speed"] = speed 
                        save_settings() 
                        message = f"Manual pump speed target set to {speed}%. Control thread will apply."
                        print(message)
                    else:
                        message = "Cannot set manual speed, not in manual mode."
            else:
                message = "Invalid speed value. Must be 0-100."
        else:
            message = "No speed value provided."
    except ValueError:
        message = "Invalid speed format. Must be a number."
    except Exception as e:
        message = f"Error setting manual speed: {e}"
    return redirect(url_for('index', message=message))

# --- Main Execution ---
if __name__ == '__main__':
    control_thread = None
    load_settings() 
    try:
        print("Initializing Solar Heater Controller...")
        os.system('sudo modprobe w1-gpio > /dev/null 2>&1')
        os.system('sudo modprobe w1-therm > /dev/null 2>&1')
        time.sleep(1)
        control_thread_running = True
        control_thread = threading.Thread(target=control_logic_thread_func, daemon=True); control_thread.start()
        update_status(system_message="Web server started. Control logic initializing...")
        flask_app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except KeyboardInterrupt: print("\nCtrl+C received. Shutting down...")
    except Exception as e: print(f"Critical error in main: {e}")
    finally:
        print("Initiating cleanup..."); control_thread_running = False
        if control_thread and control_thread.is_alive():
            control_thread.join(timeout=15)
            if control_thread.is_alive(): print("Control thread timed out.")
        write_log_buffer_to_csv()
        if current_settings.get("ENABLE_HARDWARE_WATCHDOG", False): close_watchdog()
        if pwm_pump: pwm_pump.stop()
        GPIO.cleanup(); print("Program terminated.")

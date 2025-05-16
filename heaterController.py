import os
import glob
import time
import RPi.GPIO as GPIO
import threading
from flask import Flask, render_template_string, jsonify, request, redirect, url_for, send_file
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

        graph_inlet = celsius_to_fahrenheit(inlet_temp_c) if display_unit == "F" and inlet_temp_c is not None else inlet_temp_c
        graph_outlet = celsius_to_fahrenheit(outlet_temp_c) if display_unit == "F" and outlet_temp_c is not None else outlet_temp_c

        if isinstance(inlet_temp_c, float) and isinstance(outlet_temp_c, float):
            temperature_history.append({"time": current_time_str_graph, 
                                        "inlet": round(graph_inlet, 2) if graph_inlet is not None else None, 
                                        "outlet": round(graph_outlet, 2) if graph_outlet is not None else None})
            log_buffer.append({"timestamp": full_timestamp_log, "inlet_temp_c": round(inlet_temp_c, 2), "outlet_temp_c": round(outlet_temp_c, 2)})
        elif not any(k in kwargs for k in ["pump_speed", "system_message", "target_pump_speed"]):
             temperature_history.append({"time": current_time_str_graph, "inlet": None, "outlet": None})

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
    html_template_dashboard = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="10"><title>Solar Heater Dashboard</title><script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background-color: #f0f2f5; color: #333; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
        header { background-color: #0056b3; color: white; padding: 12px 0; text-align: center; width: 100%; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
        header h1 { margin: 0; font-size: 1.6em; } nav { margin-top: 8px; }
        nav a { color: #e0e0e0; margin: 0 10px; text-decoration: none; font-size: 0.95em; padding: 5px 10px; border-radius: 4px; transition: background-color 0.3s, color 0.3s;}
        nav a:hover, nav a.active { color: #ffffff; background-color: #004080; text-decoration: none; }
        .content-wrapper { display: flex; flex-direction: column; align-items: center; width: 100%; padding: 0 10px; box-sizing: border-box;}
        .container { background-color: #ffffff; padding: 20px 25px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 90%; max-width: 850px; margin-bottom: 20px; }
        h2.page-title { color: #0056b3; text-align: center; margin-bottom: 20px; font-size: 1.7em;}
        .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-top: 10px; }
        .status-item { background-color: #e9ecef; padding: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .status-item strong { color: #0056b3; font-weight: 600; display: block; margin-bottom: 7px; font-size: 0.9em;}
        .status-item span { font-size: 1.05em; font-weight: 500; }
        .message-box { margin-top: 15px; margin-bottom: 15px; padding: 15px; background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; border-radius: 8px; text-align: center; font-weight: 500; font-size: 1em;}
        .chart-container { background-color: #ffffff; padding: 20px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 90%; max-width: 850px; margin-top: 10px; }
        .manual-controls { margin-top:20px; padding:15px; border: 1px solid #ddd; border-radius:8px; background-color:#f9f9f9;}
        .manual-controls label {margin-right:10px;} .manual-controls input[type=number] {width: 70px; margin-right:10px; padding:5px;}
        .manual-controls button {padding:5px 10px; background-color:#007bff; color:white; border:none; border-radius:4px; cursor:pointer;}
        .manual-controls button:hover {background-color:#0056b3;}
        footer { text-align: center; margin-top: 30px; font-size: 0.85em; color: #777; padding-bottom: 20px; width:100%;}
        @media (max-width: 700px) { header h1 { font-size: 1.4em; } nav a { margin: 0 8px; font-size: 0.9em;} .container, .chart-container { width: 95%; } h2.page-title {font-size: 1.4em;} .status-grid { grid-template-columns: 1fr 1fr; } }
        @media (max-width: 480px) { .status-grid { grid-template-columns: 1fr; } } 
    </style></head><body> <header><h1>Solar Heater Controller</h1><nav> <a href="/" class="active">Dashboard</a><a href="/settings">Settings</a><a href="/history">History</a> </nav></header>
    <div class="content-wrapper"><div class="container"><h2 class="page-title">Live Status & Control</h2> <div class="message-box">{{ status.system_message }}</div>
    <div class="manual-controls"> <form method="POST" action="{{ url_for('set_control_mode') }}" style="display:inline-block; margin-bottom:10px;"> <strong>Mode:</strong>
    <label><input type="radio" name="control_mode" value="auto" {% if status.control_mode == 'auto' %}checked{% endif %}> Auto</label>
    <label><input type="radio" name="control_mode" value="manual" {% if status.control_mode == 'manual' %}checked{% endif %}> Manual</label>
    <button type="submit">Set Mode</button> </form> {% if status.control_mode == 'manual' %}
    <form method="POST" action="{{ url_for('set_manual_pump_speed_route') }}" style="display:inline-block;"> <label for="manual_speed">Manual Speed (%):</label>
    <input type="number" id="manual_speed" name="manual_speed" value="{{ status.target_pump_speed }}" min="0" max="100" step="5">
    <button type="submit">Set Speed</button> </form> {% endif %} </div> <div class="status-grid">
    <div class="status-item"><strong>Inlet Temp:</strong> <span>{{ status.inlet_temp_str }}</span></div>
    <div class="status-item"><strong>Outlet Temp:</strong> <span>{{ status.outlet_temp_str }}</span></div>
    <div class="status-item"><strong>Delta T:</strong> <span>{{ status.delta_t_str }}</span></div>
    <div class="status-item"><strong>Target Speed:</strong> <span>{{ status.target_pump_speed }} %</span></div>
    <div class="status-item"><strong>Actual Speed:</strong> <span>{{ status.pump_speed }} %</span></div>
    <div class="status-item"><strong>Est. Power:</strong> <span>{{ status.thermal_power_watts }} W</span></div>
    <div class="status-item"><strong>Optimal Speed:</strong> <span>{{ status.optimal_pump_speed_found }} %</span></div>
    <div class="status-item"><strong>Max Delta T:</strong> <span>{{ status.max_delta_t_found_str }}</span></div>
    <div class="status-item"><strong>Pump ON Today:</strong> <span>{{ '%.2f' | format(status.pump_on_time_today_s / 3600.0) }} hrs</span></div>
    <div class="status-item"><strong>Energy Today:</strong> <span>{{ '%.2f' | format(status.energy_harvested_today_wh) }} Wh</span></div>
    </div></div><div class="chart-container"><canvas id="temperatureChart" height="300"></canvas></div></div>
    <footer>Last Update: {{ status.last_update }} <br/> (Page auto-refreshes every 10 seconds)</footer>
    <script> let tempChart; async function fetchGraphData() { /* ... Chart.js script ... */ } document.addEventListener('DOMContentLoaded', fetchGraphData); </script>
    </body></html>"""
    html_template_dashboard = html_template_dashboard.replace("/* ... Chart.js script ... */", """
                try {
                    const response = await fetch('/graph_data');
                    if (!response.ok) { console.error('Failed to fetch graph data:', response.status); return; }
                    const data = await response.json(); const labels = data.map(d => d.time);
                    const inletTemps = data.map(d => d.inlet); const outletTemps = data.map(d => d.outlet);
                    const displayUnitSymbol = data.length > 0 ? data[0].unit_symbol : '°C'; 
                    const chartData = { labels: labels, datasets: [
                            { label: 'Inlet Temp (' + displayUnitSymbol + ')', data: inletTemps, borderColor: 'rgb(54, 162, 235)', backgroundColor: 'rgba(54, 162, 235, 0.1)', tension: 0.1, spanGaps: true },
                            { label: 'Outlet Temp (' + displayUnitSymbol + ')', data: outletTemps, borderColor: 'rgb(255, 99, 132)', backgroundColor: 'rgba(255, 99, 132, 0.1)', tension: 0.1, spanGaps: true }
                        ]};
                    const ctx = document.getElementById('temperatureChart').getContext('2d');
                    if (tempChart) { 
                        tempChart.data = chartData; 
                        tempChart.options.scales.y.title.text = 'Temperature (' + displayUnitSymbol + ')';
                        tempChart.update('none');
                    } else { tempChart = new Chart(ctx, { type: 'line', data: chartData, options: { responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
                                scales: { y: { beginAtZero: false, title: { display: true, text: 'Temperature (' + displayUnitSymbol + ')'}}, x: { title: { display: true, text: 'Time'}}},
                                plugins: { legend: { position: 'top' }, title: { display: true, text: 'Temperature Trends' } }
                            }}); }
                } catch (error) { console.error('Error fetching or processing graph data:', error); } """)
    return render_template_string(html_template_dashboard, status=current_display_status)


@flask_app.route('/graph_data')
def get_graph_data():
    with data_lock:
        display_unit = current_settings.get("DISPLAY_TEMP_UNIT", "C")
        unit_symbol = "°F" if display_unit == "F" else "°C"
        graph_data_points = []
        for point in list(temperature_history): 
            graph_data_points.append({
                "time": point.get("time"), "inlet": point.get("inlet"), "outlet": point.get("outlet"), "unit_symbol": unit_symbol
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
    # Corrected Settings Page HTML Template
    settings_html_template = """
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Controller Settings</title><style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background-color: #f0f2f5; color: #333; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
        header { background-color: #0056b3; color: white; padding: 12px 0; text-align: center; width: 100%; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
        header h1 { margin: 0; font-size: 1.6em; } nav { margin-top: 8px; }
        nav a { color: #e0e0e0; margin: 0 10px; text-decoration: none; font-size: 0.95em; padding: 5px 10px; border-radius: 4px; transition: background-color 0.3s, color 0.3s;}
        nav a:hover, nav a.active { color: #ffffff; background-color: #004080; text-decoration: none; }
        .content-wrapper { display: flex; flex-direction: column; align-items: center; width: 100%; padding: 0 10px; box-sizing: border-box;}
        .container { background-color: #ffffff; padding: 25px 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 90%; max-width: 800px; margin-bottom: 20px; }
        h2.page-title { color: #0056b3; text-align: center; margin-bottom: 25px; font-size: 1.8em;}
        .form-grid { display: grid; grid-template-columns: 1fr; gap: 0px; } 
        @media (min-width: 768px) { .form-grid { grid-template-columns: 1fr 1fr; gap: 20px; } } 
        .form-section { margin-bottom: 20px; padding: 15px; background-color: #fdfdfd; border-radius: 5px; border: 1px solid #eee;} 
        .form-section h3 {color: #004080; border-bottom: 1px solid #eee; padding-bottom:8px; margin-top:0; margin-bottom:18px; font-size:1.1em;}
        .form-group { margin-bottom: 18px; }
        .form-group label { display: block; margin-bottom: 7px; font-weight: 600; color: #333; font-size:0.9em; }
        .form-group input[type="text"], .form-group input[type="number"], .form-group select { width: calc(100% - 24px); padding: 10px; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box; font-size: 0.95em; }
        .form-group small { display: block; font-size: 0.8em; color: #555; margin-top: 5px; }
        .submit-btn, .download-btn { background-color: #28a745; color: white; padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1.05em; text-decoration:none; text-align:center; }
        .submit-btn { display: block; width: 100%; margin-top: 25px;} .submit-btn:hover { background-color: #218838; }
        .download-btn { background-color: #007bff; display:inline-block; width:auto; padding: 10px 15px; margin-top:5px;} .download-btn:hover { background-color: #0056b3;}
        .message-box { margin-bottom: 20px; padding: 15px; border-radius: 5px; text-align: center; font-weight: 500;}
        .message-box.success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb;}
        .message-box.error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb;}
        .message-box.warning { background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba;}
        footer { text-align: center; margin-top: 30px; font-size: 0.85em; color: #777; padding-bottom: 20px; width:100%;}
    </style></head>
    <body><header><h1>Solar Heater Controller</h1><nav><a href="/">Dashboard</a><a href="/settings" class="active">Settings</a><a href="/history">History</a></nav></header>
    <div class="content-wrapper"><div class="container"><h2 class="page-title">Application Settings</h2>
    {% if message %}
        <div class="message-box {{ 'success' if 'success' in message.lower() else ('error' if 'error' in message.lower() else ('warning' if ('warning' in message.lower() or 'restart' in message.lower()) else '')) }}">{{ message }}</div>
    {% endif %}
    <form method="POST"><div class="form-grid">
    <div class="form-section"><h3>Sensor & Hardware</h3>
    {% for key in ['INLET_SENSOR_ID', 'OUTLET_SENSOR_ID', 'PUMP_PWM_PIN', 'PWM_FREQUENCY'] %}
    <div class="form-group">
        <label for="{{ key }}">{{ key.replace('_', ' ').title() }}:</label>
        <input type="{{ 'number' if DEFAULT_SETTINGS[key] is number else 'text' }}" id="{{ key }}" name="{{ key }}" value="{{ settings[key] }}">
        <small>Default: {{ DEFAULT_SETTINGS[key] }}</small>
    </div>
    {% endfor %}
    </div> 
    <div class="form-section"><h3>Pump Control</h3>
    {% for key in ['MIN_PUMP_SPEED', 'MAX_PUMP_SPEED', 'PUMP_SPEED_STEP', 'MAX_PUMP_FLOW_RATE_LPM'] %}
    <div class="form-group">
        <label for="{{ key }}">{{ key.replace('_', ' ').title() }}:</label>
        <input type="number" id="{{ key }}" name="{{ key }}" value="{{ settings[key] }}" 
               {% if key in ['MIN_PUMP_SPEED', 'MAX_PUMP_SPEED'] %}min="0" max="100"{% elif key=='PUMP_SPEED_STEP'%}min="1"{% else %}step="0.1" min="0"{% endif %}>
        <small>Default: {{ DEFAULT_SETTINGS[key] }}</small>
    </div>
    {% endfor %}
    </div>
    <div class="form-section"><h3>Operational Logic</h3>
    {% for key in ['STABILIZATION_TIME_S', 'LOOP_INTERVAL_S', 'DELTA_T_ON', 'DELTA_T_OFF', 'MIN_INLET_TEMP_TO_RUN', 'MAX_OUTLET_TEMP_CUTOFF'] %}
    <div class="form-group">
        <label for="{{ key }}">{{ key.replace('_', ' ').title() }}:</label>
        <input type="number" id="{{ key }}" name="{{ key }}" value="{{ settings[key] }}" 
               {% if 'TEMP' in key or 'DELTA' in key %}step="0.1"{% else %}min="1"{% endif %}>
        <small>Default: {{ DEFAULT_SETTINGS[key] }}</small>
    </div>
    {% endfor %}
    </div>
    <div class="form-section"><h3>Logging & UI</h3>
    {% for key in ['LOG_SAVE_INTERVAL_S', 'TEMPERATURE_LOG_FILE', 'MAX_HISTORY_POINTS', 'MAX_HISTORY_TABLE_ROWS', 'DISPLAY_TEMP_UNIT'] %}
    <div class="form-group">
        <label for="{{ key }}">{{ key.replace('_', ' ').title() }}:</label>
        {% if key == 'DISPLAY_TEMP_UNIT' %}
            <select id="{{ key }}" name="{{ key }}">
                <option value="C" {% if settings[key] == 'C' %}selected{% endif %}>Celsius (°C)</option>
                <option value="F" {% if settings[key] == 'F' %}selected{% endif %}>Fahrenheit (°F)</option>
            </select>
        {% else %}
            <input type="{{ 'number' if DEFAULT_SETTINGS[key] is number else 'text' }}" id="{{ key }}" name="{{ key }}" value="{{ settings[key] }}" 
                   {% if DEFAULT_SETTINGS[key] is number %}min="1"{% endif %}>
        {% endif %}
        <small>Default: {{ DEFAULT_SETTINGS[key] }}</small>
    </div>
    {% endfor %}
    </div>
    <div class="form-section"><h3>Advanced</h3>
    <div class="form-group">
        <label for="ENABLE_HARDWARE_WATCHDOG">Enable Hardware Watchdog:</label>
        <select id="ENABLE_HARDWARE_WATCHDOG" name="ENABLE_HARDWARE_WATCHDOG">
        <option value="true" {% if settings.ENABLE_HARDWARE_WATCHDOG %}selected{% endif %}>Yes</option>
        <option value="false" {% if not settings.ENABLE_HARDWARE_WATCHDOG %}selected{% endif %}>No</option></select>
        <small>Default: {{ DEFAULT_SETTINGS.ENABLE_HARDWARE_WATCHDOG }}. Requires OS config: dtparam=watchdog=on in /boot/config.txt</small>
    </div>
    <div class="form-group">
        <label for="WATCHDOG_KICK_INTERVAL_S">Watchdog Kick Interval (s):</label>
        <input type="number" id="WATCHDOG_KICK_INTERVAL_S" name="WATCHDOG_KICK_INTERVAL_S" value="{{ settings.WATCHDOG_KICK_INTERVAL_S }}" min="5">
        <small>Default: {{ DEFAULT_SETTINGS.WATCHDOG_KICK_INTERVAL_S }}</small>
    </div>
    <div class="form-group">
        <label for="WATCHDOG_DEVICE">Watchdog Device Path:</label>
        <input type="text" id="WATCHDOG_DEVICE" name="WATCHDOG_DEVICE" value="{{ settings.WATCHDOG_DEVICE }}">
        <small>Default: {{ DEFAULT_SETTINGS.WATCHDOG_DEVICE }}</small>
    </div>
    </div></div> 
    <div class="form-group" style="margin-top:20px; text-align:center;"> <label style="margin-bottom:10px;">Download Log File:</label>
        <a href="/download_log" class="download-btn">Download {{ settings.TEMPERATURE_LOG_FILE }}</a>
    </div>
    <button type="submit" class="submit-btn">Save Settings</button></form>
    </div></div><footer>Controller Version 1.5</footer></body></html>"""
    return render_template_string(settings_html_template, settings=settings_to_display, message=message, DEFAULT_SETTINGS=DEFAULT_SETTINGS)

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
    except Exception as e: message = f"Error reading log file: {e}"; print(f"Error on /history: {e}")
    history_html_template = """"""
    history_html_template = """
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Log History</title><style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background-color: #f0f2f5; color: #333; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
        header { background-color: #0056b3; color: white; padding: 12px 0; text-align: center; width: 100%; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
        header h1 { margin: 0; font-size: 1.6em; } nav { margin-top: 8px; }
        nav a { color: #e0e0e0; margin: 0 10px; text-decoration: none; font-size: 0.95em; padding: 5px 10px; border-radius: 4px;}
        nav a:hover, nav a.active { color: #ffffff; background-color: #004080;}
        .content-wrapper { display: flex; flex-direction: column; align-items: center; width: 100%; padding: 0 10px; box-sizing: border-box;}
        .container { background-color: #ffffff; padding: 25px 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 95%; max-width: 1000px; margin-bottom: 20px; }
        h2.page-title { color: #0056b3; text-align: center; margin-bottom: 10px; font-size: 1.8em;}
        .info-text { text-align:center; font-size:0.9em; color:#555; margin-bottom:20px;}
        .download-section { margin-bottom: 20px; text-align: center; }
        .download-btn { background-color: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1em; text-decoration:none;}
        .download-btn:hover { background-color: #0056b3;}
        table { width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 0.9em; }
        th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
        th { background-color: #e9ecef; color: #0056b3; }
        tr:nth-child(even) { background-color: #f9f9f9; }
        .message-box { margin-bottom: 20px; padding: 15px; border-radius: 5px; text-align: center; font-weight: 500;}
        .message-box.error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb;}
        footer { text-align: center; margin-top: 30px; font-size: 0.85em; color: #777; padding-bottom: 20px; width:100%;}
    </style></head>
    <body><header><h1>Solar Heater Controller</h1><nav><a href="/">Dashboard</a><a href="/settings">Settings</a><a href="/history" class="active">History</a></nav></header>
    <div class="content-wrapper"><div class="container">
    <h2 class="page-title">Temperature Log History (Last {{ max_rows }} Entries)</h2>
    <p class="info-text">Displaying temperatures in {{ unit_symbol_hist }}. Logged data in CSV is always in Celsius.</p>
    {% if message %}<div class="message-box error">{{ message }}</div>{% endif %}
    <div class="download-section"><a href="/download_log" class="download-btn">Download Full Log ({{ log_file_name }})</a></div>
    {% if log_data_preview and log_data_preview[0] %} 
        <table><thead><tr>
        {% for header_cell in log_data_preview[0] %}<th>{{ header_cell }}</th>{% endfor %}
        </tr></thead><tbody>
        {% for row in log_data_preview[1:] %}<tr>
        {% for cell in row %}<td>{{ cell }}</td>{% endfor %}
        </tr>{% endfor %}
        </tbody></table>
    {% else %} <p>No log data to display, or log file is empty/not found.</p> {% endif %}
    </div></div><footer>Controller Version 1.5</footer></body></html>"""
    return render_template_string(history_html_template, log_data_preview=log_data_preview, message=message, log_file_name=log_file_name, max_rows=current_settings.get("MAX_HISTORY_TABLE_ROWS", DEFAULT_SETTINGS["MAX_HISTORY_TABLE_ROWS"]), unit_symbol_hist=unit_symbol_hist)

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

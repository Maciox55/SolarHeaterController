import os
import glob
import time
import RPi.GPIO as GPIO
import threading
from flask import Flask, render_template_string, jsonify, request, redirect, url_for, send_file # Added send_file
import collections # For deque
import csv         # For CSV logging
import json        # For settings persistence
# import datetime # Not used in this reverted version for daily stats

# --- Configuration File ---
SETTINGS_FILE = 'solar_heater_settings.json'

# --- Default Settings (used if settings file is missing or invalid) ---
# These were the settings at the point of the "Settings Page & Data Logging & Download" version
DEFAULT_SETTINGS = {
    "INLET_SENSOR_ID": "28-330cd445a1e8",
    "OUTLET_SENSOR_ID": "28-7c0ed445ed1e",
    "PUMP_PWM_PIN": 18,
    "PWM_FREQUENCY": 100,
    "MIN_PUMP_SPEED": 20,
    "MAX_PUMP_SPEED": 100,
    "PUMP_SPEED_STEP": 5,
    "STABILIZATION_TIME_S": 45,
    "LOOP_INTERVAL_S": 300,
    "MIN_TEMP_DIFFERENCE_TO_RUN": 1.0, # This was the single threshold before DELTA_T_ON/OFF
    "MIN_INLET_TEMP_TO_RUN": 10.0,
    "MAX_OUTLET_TEMP_CUTOFF": 75.0,
    "MAX_PUMP_FLOW_RATE_LPM": 20.0,
    "LOG_SAVE_INTERVAL_S": 300,
    "TEMPERATURE_LOG_FILE": "temperature_log.csv",
    "MAX_HISTORY_POINTS": 150
}
# --- Active Settings (loaded from file or defaults) ---
current_settings = DEFAULT_SETTINGS.copy()

# --- Constants ---
BASE_DIR = '/sys/bus/w1/devices/'
SPECIFIC_HEAT_CAPACITY_WATER = 4186  # J/kg°C
WATER_DENSITY_KG_L = 1.0  # kg/L (approx)

# --- Application State & Data ---
app_status = {
    "inlet_temp": "N/A",
    "outlet_temp": "N/A",
    "delta_t": "N/A",
    "pump_speed": 0,
    "thermal_power_watts": "N/A",
    "system_message": "Initializing...",
    "optimal_pump_speed_found": "N/A", # Will be set from settings
    "max_delta_t_found": "N/A",
    "last_update": time.strftime("%Y-%m-%d %H:%M:%S")
}
temperature_history = collections.deque(maxlen=DEFAULT_SETTINGS["MAX_HISTORY_POINTS"])
log_buffer = []
data_lock = threading.Lock()

# --- Globals ---
inlet_sensor_file = None
outlet_sensor_file = None
pwm_pump = None
control_thread_running = False
# watchdog_fd = None # Watchdog not in this version

# --- Settings Load/Save Functions ---
def load_settings():
    global current_settings, temperature_history, app_status
    print(f"Attempting to load settings from {SETTINGS_FILE}...")
    try:
        with open(SETTINGS_FILE, 'r') as f:
            loaded_s = json.load(f)
            temp_settings = DEFAULT_SETTINGS.copy()
            for key, value in loaded_s.items():
                if key in temp_settings:
                    default_val_type = type(DEFAULT_SETTINGS[key])
                    try:
                        if default_val_type == bool: # Should not have bools in this version's defaults
                            converted_value = str(value).lower() in ['true', 'on', '1', 'yes', 'checked']
                        elif default_val_type == int:
                            converted_value = int(float(value))
                        elif default_val_type == float:
                            converted_value = float(value)
                        else: 
                            converted_value = str(value)
                        temp_settings[key] = converted_value
                    except ValueError:
                         print(f"Warning: Could not convert setting '{key}' value '{value}' to {default_val_type}. Using default.")
                else:
                    print(f"Warning: Unknown setting '{key}' in {SETTINGS_FILE}. Ignoring.")
            current_settings = temp_settings
            print("Settings loaded successfully.")
    except FileNotFoundError:
        print(f"{SETTINGS_FILE} not found. Using default settings and creating file.")
        current_settings = DEFAULT_SETTINGS.copy()
        save_settings()
    except json.JSONDecodeError:
        print(f"Error decoding JSON from {SETTINGS_FILE}. Using default settings and overwriting.")
        current_settings = DEFAULT_SETTINGS.copy()
        save_settings()
    except Exception as e:
        print(f"Error loading settings: {e}. Using default settings.")
        current_settings = DEFAULT_SETTINGS.copy()
    
    with data_lock:
        app_status["optimal_pump_speed_found"] = current_settings.get("MIN_PUMP_SPEED", DEFAULT_SETTINGS["MIN_PUMP_SPEED"])
        # control_mode and target_pump_speed were not in app_status in this version
    
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

# --- Sensor Functions ---
def discover_sensors():
    global inlet_sensor_file, outlet_sensor_file
    try:
        inlet_id = current_settings["INLET_SENSOR_ID"]
        outlet_id = current_settings["OUTLET_SENSOR_ID"]
        if "x" in inlet_id.lower() or "x" in outlet_id.lower():
            raise KeyError("Default/placeholder sensor IDs are still in use.")
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

def read_temp_c(sensor_file_path):
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
    update_status(pump_speed=0, system_message="PWM Initialized. Pump is OFF.")

def set_pump_speed(speed_percent_target): # Renamed from speed_percent for clarity
    global pwm_pump
    if pwm_pump is None:
        update_status(system_message="Error: PWM not initialized."); return
    
    target_speed = max(0, min(100, speed_percent_target)) # Ensure target is 0-100
    
    actual_duty_cycle = 0
    if target_speed > 0: # Only apply min/max if trying to run
        actual_duty_cycle = max(current_settings["MIN_PUMP_SPEED"], min(current_settings["MAX_PUMP_SPEED"], target_speed))
    
    if pwm_pump: pwm_pump.ChangeDutyCycle(float(actual_duty_cycle))
    else: print("Error: pwm_pump object is None in set_pump_speed.")
    
    # In this version, app_status["pump_speed"] reflected the actual duty cycle
    update_status(pump_speed=actual_duty_cycle)


def stop_pump():
    set_pump_speed(0)
    update_status(system_message="Pump stopped.", thermal_power_watts="N/A")

# --- Thermal Power & Statistics ---
def calculate_estimated_thermal_power(delta_t_celsius, current_actual_pump_speed_percent):
    if delta_t_celsius is None or current_actual_pump_speed_percent is None or current_actual_pump_speed_percent == 0: return None
    estimated_flow_lps = (float(current_actual_pump_speed_percent) / 100.0) * (current_settings["MAX_PUMP_FLOW_RATE_LPM"] / 60.0)
    return (estimated_flow_lps * WATER_DENSITY_KG_L) * SPECIFIC_HEAT_CAPACITY_WATER * delta_t_celsius

# Daily stats were not in this version
# def update_daily_stats(...): pass

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
def update_status_and_history(inlet_temp=None, outlet_temp=None, delta_t=None, **kwargs):
    global log_buffer
    with data_lock:
        current_time_str_graph, full_timestamp_log = time.strftime("%H:%M:%S"), time.strftime("%Y-%m-%d %H:%M:%S")
        status_updates = kwargs.copy()
        status_updates["inlet_temp"] = f"{inlet_temp:.2f}" if isinstance(inlet_temp, float) else "N/A"
        status_updates["outlet_temp"] = f"{outlet_temp:.2f}" if isinstance(outlet_temp, float) else "N/A"
        calculated_delta_t = None
        if isinstance(inlet_temp, float) and isinstance(outlet_temp, float): calculated_delta_t = outlet_temp - inlet_temp
        elif isinstance(delta_t, float): calculated_delta_t = delta_t
        status_updates["delta_t"] = f"{calculated_delta_t:.2f}" if calculated_delta_t is not None else "N/A"
        actual_pump_speed = float(app_status.get("pump_speed", 0)) # This is actual duty cycle
        power_w = calculate_estimated_thermal_power(calculated_delta_t, actual_pump_speed)
        status_updates["thermal_power_watts"] = f"{power_w:.1f}" if power_w is not None else "N/A"
        for key, value in status_updates.items():
            if key in app_status: app_status[key] = value
        app_status["last_update"] = full_timestamp_log
        if isinstance(inlet_temp, float) and isinstance(outlet_temp, float):
            temperature_history.append({"time": current_time_str_graph, "inlet": round(inlet_temp, 2), "outlet": round(outlet_temp, 2)})
            log_buffer.append({"timestamp": full_timestamp_log, "inlet_temp_c": round(inlet_temp, 2), "outlet_temp_c": round(outlet_temp, 2)})
        elif not any(k in kwargs for k in ["pump_speed", "system_message"]): # target_pump_speed not present
             temperature_history.append({"time": current_time_str_graph, "inlet": None, "outlet": None})

def update_status(**kwargs):
    with data_lock:
        for key, value in kwargs.items():
            if key in app_status:
                if isinstance(value, float) and key not in ["inlet_temp", "outlet_temp", "delta_t", "thermal_power_watts"]: # pump_on_time & energy_harvested not present
                    app_status[key] = f"{value:.2f}" 
                else: app_status[key] = value
        app_status["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")

# --- Main Control Logic ---
def optimize_pump_speed():
    # Learning algorithm was not in this version
    update_status_and_history(system_message="Optimizing pump speed...") 
    current_max_delta_t_this_cycle, current_optimal_speed_this_cycle = -100.0, current_settings["MIN_PUMP_SPEED"]
    initial_inlet_temp = read_temp_c(inlet_sensor_file)
    if initial_inlet_temp is None or initial_inlet_temp < current_settings["MIN_INLET_TEMP_TO_RUN"]:
        msg = f"Opt aborted: Inlet ({initial_inlet_temp or 'N/A'}) < {current_settings['MIN_INLET_TEMP_TO_RUN']}°C."
        stop_pump(); update_status_and_history(inlet_temp=initial_inlet_temp, system_message=msg); return
    
    # Simple sweep from MIN to MAX
    for speed_to_test in range(current_settings["MIN_PUMP_SPEED"], current_settings["MAX_PUMP_SPEED"] + 1, current_settings["PUMP_SPEED_STEP"]):
        if not control_thread_running: return
        set_pump_speed(speed_to_test) 
        update_status(system_message=f"Optimizing: Stabilizing at {speed_to_test}%...")
        stabilization_s = current_settings["STABILIZATION_TIME_S"]
        for _ in range(stabilization_s):
            if not control_thread_running: return
            time.sleep(1)
        in_temp, out_temp, delta_t_val = read_temp_c(inlet_sensor_file), read_temp_c(outlet_sensor_file), None
        if in_temp and out_temp: delta_t_val = out_temp - in_temp
        update_status_and_history(inlet_temp=in_temp, outlet_temp=out_temp, delta_t=delta_t_val, system_message=f"Optimizing: Tested {speed_to_test}%")
        if in_temp and out_temp:
            if delta_t_val > current_max_delta_t_this_cycle:
                current_max_delta_t_this_cycle, current_optimal_speed_this_cycle = delta_t_val, speed_to_test
            if out_temp > current_settings["MAX_OUTLET_TEMP_CUTOFF"]:
                msg = f"SAFETY: Outlet {out_temp:.2f}°C > {current_settings['MAX_OUTLET_TEMP_CUTOFF']}°C. Stopping."
                stop_pump(); update_status_and_history(outlet_temp=out_temp, system_message=msg); return
    
    # Use MIN_TEMP_DIFFERENCE_TO_RUN as the threshold
    if current_max_delta_t_this_cycle >= current_settings["MIN_TEMP_DIFFERENCE_TO_RUN"]:
        with data_lock:
            app_status["max_delta_t_found"] = f"{current_max_delta_t_this_cycle:.2f}"
            app_status["optimal_pump_speed_found"] = current_optimal_speed_this_cycle
        msg = f"Opt complete. Optimal: {current_optimal_speed_this_cycle}% (ΔT: {current_max_delta_t_this_cycle:.2f}°C)."
        set_pump_speed(current_optimal_speed_this_cycle)
        final_in, final_out, final_dt = read_temp_c(inlet_sensor_file), read_temp_c(outlet_sensor_file), None
        if final_in and final_out: final_dt = final_out - final_in
        update_status_and_history(inlet_temp=final_in, outlet_temp=final_out, delta_t=final_dt, system_message=msg)
    else:
        msg = f"Opt: No speed yielded ΔT >= {current_settings['MIN_TEMP_DIFFERENCE_TO_RUN']}°C. Stopping."
        with data_lock: app_status["max_delta_t_found"] = f"{current_max_delta_t_this_cycle:.2f}" if current_max_delta_t_this_cycle > -100 else "N/A"
        stop_pump(); update_status(system_message=msg)

def control_logic_thread_func():
    global control_thread_running
    load_settings()
    if not discover_sensors(): control_thread_running = False; return
    setup_pwm(); time.sleep(1)
    # Watchdog setup was not in this version
    last_control_cycle_time = time.time() - current_settings["LOOP_INTERVAL_S"] 
    last_log_save_time = time.time()
    # last_watchdog_kick_time not needed
    # Daily stats init not in this version

    while control_thread_running:
        current_time = time.time()
        loop_interval = current_settings["LOOP_INTERVAL_S"]
        log_interval = current_settings["LOG_SAVE_INTERVAL_S"]
        # watchdog_kick_interval not needed
        
        # Daily stats update was not in this version
        
        # Control mode handling was simpler in this version (no manual mode from UI)
        if (current_time - last_control_cycle_time) >= loop_interval:
            update_status(system_message="Checking conditions...")
            inlet_temp, outlet_temp, dt_val = read_temp_c(inlet_sensor_file), read_temp_c(outlet_sensor_file), None
            if inlet_temp and outlet_temp: dt_val = outlet_temp - inlet_temp
            update_status_and_history(inlet_temp=inlet_temp, outlet_temp=outlet_temp, delta_t=dt_val, system_message="Checked conditions.")
            
            if inlet_temp and outlet_temp:
                if outlet_temp > current_settings["MAX_OUTLET_TEMP_CUTOFF"]:
                    msg = f"SAFETY: Outlet {outlet_temp:.2f}°C > {current_settings['MAX_OUTLET_TEMP_CUTOFF']}°C. Stopping."
                    stop_pump(); update_status(system_message=msg)
                elif inlet_temp < current_settings["MIN_INLET_TEMP_TO_RUN"]:
                    msg = f"COND: Inlet {inlet_temp:.2f}°C < {current_settings['MIN_INLET_TEMP_TO_RUN']}°C. Pump OFF."
                    if float(app_status.get("pump_speed", "0")) > 0 : stop_pump() # Ensure it's off
                    update_status(system_message=msg)
                elif dt_val >= current_settings["MIN_TEMP_DIFFERENCE_TO_RUN"]: # Single threshold
                    msg = f"COND: ΔT ({dt_val:.2f}°C) sufficient. Optimizing..."
                    update_status(system_message=msg); optimize_pump_speed()
                else: # Delta T too low
                    msg = f"COND: ΔT ({dt_val:.2f}°C) < {current_settings['MIN_TEMP_DIFFERENCE_TO_RUN']}°C. Pump OFF."
                    if float(app_status.get("pump_speed", "0")) > 0 : stop_pump() # Ensure it's off
                    update_status(system_message=msg)
            else: 
                errmsg = "Sensor error in main loop. Stopping pump."
                stop_pump(); update_status(system_message=errmsg)
            last_control_cycle_time = current_time
        
        if (current_time - last_log_save_time) >= log_interval:
            write_log_buffer_to_csv(); last_log_save_time = current_time
        
        # Watchdog kicking not in this version
        
        time.sleep(1) 
    
    stop_pump(); update_status(system_message="Control thread stopped."); write_log_buffer_to_csv()
    # close_watchdog not needed

# --- Flask Web Application ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    with data_lock: current_display_status = app_status.copy()
    # This version had the simpler header (Dashboard, Settings)
    html_template_dashboard = """
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="10"><title>Solar Heater Dashboard</title><script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background-color: #f0f2f5; color: #333; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
        header { background-color: #0056b3; color: white; padding: 12px 0; text-align: center; width: 100%; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
        header h1 { margin: 0; font-size: 1.6em; }
        nav { margin-top: 8px; }
        nav a { color: #e0e0e0; margin: 0 15px; text-decoration: none; font-size: 0.95em; padding: 5px 10px; border-radius: 4px; transition: background-color 0.3s, color 0.3s;}
        nav a:hover, nav a.active { color: #ffffff; background-color: #004080; text-decoration: none; }
        .content-wrapper { display: flex; flex-direction: column; align-items: center; width: 100%; padding: 0 10px; box-sizing: border-box;}
        .container { background-color: #ffffff; padding: 20px 25px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 90%; max-width: 850px; margin-bottom: 20px; }
        h2.page-title { color: #0056b3; text-align: center; margin-bottom: 20px; font-size: 1.7em;}
        .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 15px; margin-top: 10px; } /* Adjusted for fewer items */
        .status-item { background-color: #e9ecef; padding: 15px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .status-item strong { color: #0056b3; font-weight: 600; display: block; margin-bottom: 7px; font-size: 0.9em;}
        .status-item span { font-size: 1.05em; font-weight: 500; }
        .message-box { margin-top: 15px; margin-bottom: 15px; padding: 15px; background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; border-radius: 8px; text-align: center; font-weight: 500; font-size: 1em;}
        .chart-container { background-color: #ffffff; padding: 20px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 90%; max-width: 850px; margin-top: 10px; }
        footer { text-align: center; margin-top: 30px; font-size: 0.85em; color: #777; padding-bottom: 20px; width:100%;}
        @media (max-width: 700px) {
             header h1 { font-size: 1.4em; } nav a { margin: 0 8px; font-size: 0.9em;}
            .container, .chart-container { width: 95%; } h2.page-title {font-size: 1.4em;}
        }
    </style></head><body> <header><h1>Solar Heater Controller</h1><nav>
    <a href="/" class="active">Dashboard</a><a href="/settings">Settings</a>
    </nav></header>
    <div class="content-wrapper"><div class="container"><h2 class="page-title">Live Status</h2>
    <div class="message-box">{{ status.system_message }}</div><div class="status-grid">
    <div class="status-item"><strong>Inlet Temp:</strong> <span>{{ status.inlet_temp }} °C</span></div>
    <div class="status-item"><strong>Outlet Temp:</strong> <span>{{ status.outlet_temp }} °C</span></div>
    <div class="status-item"><strong>Delta T:</strong> <span>{{ status.delta_t }} °C</span></div>
    <div class="status-item"><strong>Pump Speed:</strong> <span>{{ status.pump_speed }} %</span></div>
    <div class="status-item"><strong>Est. Power:</strong> <span>{{ status.thermal_power_watts }} W</span></div>
    <div class="status-item"><strong>Optimal Speed:</strong> <span>{{ status.optimal_pump_speed_found }} %</span></div>
    <div class="status-item"><strong>Max Delta T:</strong> <span>{{ status.max_delta_t_found }} °C</span></div>
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
                    const chartData = { labels: labels, datasets: [
                            { label: 'Inlet Temp (°C)', data: inletTemps, borderColor: 'rgb(54, 162, 235)', backgroundColor: 'rgba(54, 162, 235, 0.1)', tension: 0.1, spanGaps: true },
                            { label: 'Outlet Temp (°C)', data: outletTemps, borderColor: 'rgb(255, 99, 132)', backgroundColor: 'rgba(255, 99, 132, 0.1)', tension: 0.1, spanGaps: true }
                        ]};
                    const ctx = document.getElementById('temperatureChart').getContext('2d');
                    if (tempChart) { tempChart.data = chartData; tempChart.update('none');
                    } else { tempChart = new Chart(ctx, { type: 'line', data: chartData, options: { responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
                                scales: { y: { beginAtZero: false, title: { display: true, text: 'Temperature (°C)'}}, x: { title: { display: true, text: 'Time'}}},
                                plugins: { legend: { position: 'top' }, title: { display: true, text: 'Temperature Trends' } }
                            }}); }
                } catch (error) { console.error('Error fetching or processing graph data:', error); } """)
    return render_template_string(html_template_dashboard, status=current_display_status)


@flask_app.route('/graph_data')
def get_graph_data():
    with data_lock: return jsonify(list(temperature_history))

@flask_app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    global current_settings, temperature_history 
    message = request.args.get('message', None)
    if request.method == 'POST':
        try:
            settings_changed = False
            form_errors = []
            with data_lock: 
                new_settings_data = current_settings.copy()
                for key in DEFAULT_SETTINGS.keys(): # Only iterate over keys present in DEFAULT_SETTINGS
                    if key == "CONTROL_MODE" or key == "MANUAL_PUMP_SPEED_SETTING" or "WATCHDOG" in key: # These were not in this version's settings form
                        continue 
                    form_value = request.form.get(key)
                    if form_value is not None:
                        try:
                            default_val_type = type(DEFAULT_SETTINGS[key])
                            converted_value = None
                            if default_val_type == bool: converted_value = str(form_value).lower() in ['true', 'on', '1', 'yes', 'checked']
                            elif default_val_type == float: converted_value = float(form_value)
                            elif default_val_type == int: converted_value = int(float(form_value))
                            else: converted_value = str(form_value)
                            if new_settings_data.get(key) != converted_value:
                                new_settings_data[key] = converted_value
                                settings_changed = True
                        except ValueError:
                            form_errors.append(f"Invalid format for '{key.replace('_',' ').title()}'.")
                
                if form_errors:
                    message = "Please correct errors: " + " | ".join(form_errors)
                else:
                    if settings_changed:
                        current_settings = new_settings_data
                        if save_settings(): 
                            message = "Settings updated and saved! Some changes may need a script restart."
                            if temperature_history.maxlen != current_settings["MAX_HISTORY_POINTS"]:
                                 temperature_history = collections.deque(maxlen=current_settings["MAX_HISTORY_POINTS"])
                        else: message = "Settings updated in memory, but failed to save to file."
                    else: message = "No changes detected."
        except Exception as e: message = f"Error updating settings: {e}"
        return redirect(url_for('settings_page', message=message))

    with data_lock: settings_to_display = current_settings.copy()
    # Simplified settings HTML for this reverted version
    settings_html_template = """
    <!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Controller Settings</title><style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background-color: #f0f2f5; color: #333; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
        header { background-color: #0056b3; color: white; padding: 12px 0; text-align: center; width: 100%; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
        header h1 { margin: 0; font-size: 1.6em; } nav { margin-top: 8px; }
        nav a { color: #e0e0e0; margin: 0 10px; text-decoration: none; font-size: 0.95em; padding: 5px 10px; border-radius: 4px;}
        nav a:hover, nav a.active { color: #ffffff; background-color: #004080;}
        .content-wrapper { display: flex; flex-direction: column; align-items: center; width: 100%; padding: 0 10px; box-sizing: border-box;}
        .container { background-color: #ffffff; padding: 25px 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 90%; max-width: 700px; margin-bottom: 20px; }
        h2.page-title { color: #0056b3; text-align: center; margin-bottom: 25px; font-size: 1.8em;}
        .form-grid { display: grid; grid-template-columns: 1fr; gap: 0px; } 
        @media (min-width: 768px) { .form-grid { grid-template-columns: 1fr 1fr; gap: 20px; } } 
        .form-section { margin-bottom: 20px; padding: 15px; background-color: #fdfdfd; border-radius: 5px; border: 1px solid #eee;} 
        .form-section h3 {color: #004080; border-bottom: 1px solid #eee; padding-bottom:8px; margin-top:0; margin-bottom:18px; font-size:1.1em;}
        .form-group { margin-bottom: 18px; }
        .form-group label { display: block; margin-bottom: 7px; font-weight: 600; color: #333; font-size:0.9em; }
        .form-group input[type="text"], .form-group input[type="number"] { width: calc(100% - 24px); padding: 10px; border: 1px solid #ccc; border-radius: 5px; box-sizing: border-box; font-size: 0.95em; }
        .form-group small { display: block; font-size: 0.8em; color: #555; margin-top: 5px; }
        .submit-btn, .download-btn { background-color: #28a745; color: white; padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 1.05em; text-decoration:none; text-align:center; }
        .submit-btn { display: block; width: 100%; margin-top: 25px;} .submit-btn:hover { background-color: #218838; }
        .download-btn { background-color: #007bff; display:inline-block; width:auto; padding: 10px 15px; margin-top:5px;} .download-btn:hover { background-color: #0056b3;}
        .message-box { margin-bottom: 20px; padding: 15px; border-radius: 5px; text-align: center; font-weight: 500;}
        .message-box.success { background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb;}
        .message-box.error { background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb;}
        footer { text-align: center; margin-top: 30px; font-size: 0.85em; color: #777; padding-bottom: 20px; width:100%;}
    </style></head>
    <body><header><h1>Solar Heater Controller</h1><nav><a href="/">Dashboard</a><a href="/settings" class="active">Settings</a></nav></header>
    <div class="content-wrapper"><div class="container"><h2 class="page-title">Application Settings</h2>
    {% if message %}<div class="message-box {{ 'success' if 'success' in message.lower() else 'error' if 'error' in message.lower() else '' }}">{{ message }}</div>{% endif %}
    <form method="POST"><div class="form-grid"> <div class="form-section"><h3>Core Settings</h3>
    {% for key in ['INLET_SENSOR_ID', 'OUTLET_SENSOR_ID', 'PUMP_PWM_PIN', 'PWM_FREQUENCY', 'MIN_PUMP_SPEED', 'MAX_PUMP_SPEED', 'PUMP_SPEED_STEP'] %}
        <div class="form-group"><label for="{{ key }}">{{ key.replace('_', ' ').title() }}:</label>
        <input type="{{ 'number' if DEFAULT_SETTINGS[key] is number else 'text' }}" id="{{ key }}" name="{{ key }}" value="{{ settings[key] }}">
        <small>Default: {{ DEFAULT_SETTINGS[key] }}</small></div>
    {% endfor %}</div> <div class="form-section"><h3>Operational Logic & Logging</h3>
    {% for key in ['STABILIZATION_TIME_S', 'LOOP_INTERVAL_S', 'MIN_TEMP_DIFFERENCE_TO_RUN', 'MIN_INLET_TEMP_TO_RUN', 'MAX_OUTLET_TEMP_CUTOFF', 'MAX_PUMP_FLOW_RATE_LPM', 'LOG_SAVE_INTERVAL_S', 'TEMPERATURE_LOG_FILE', 'MAX_HISTORY_POINTS'] %}
        <div class="form-group"><label for="{{ key }}">{{ key.replace('_', ' ').title() }}:</label>
        <input type="{{ 'number' if DEFAULT_SETTINGS[key] is number else 'text' }}" id="{{ key }}" name="{{ key }}" value="{{ settings[key] }}" {% if 'TEMP' in key or 'DELTA' in key or 'FLOW' in key %}step="0.1"{% endif %}>
        <small>Default: {{ DEFAULT_SETTINGS[key] }}</small></div>
    {% endfor %}
    </div></div> 
    <div class="form-group" style="margin-top:20px; text-align:center;"> <label style="margin-bottom:10px;">Download Log File:</label>
        <a href="/download_log" class="download-btn">Download {{ settings.TEMPERATURE_LOG_FILE }}</a>
    </div>
    <button type="submit" class="submit-btn">Save Settings</button></form>
    </div></div><footer>Controller Version Reverted (Settings/Log/Download)</footer></body></html>"""
    return render_template_string(settings_html_template, settings=settings_to_display, message=message, DEFAULT_SETTINGS=DEFAULT_SETTINGS)

# /history route was not in this version
# @flask_app.route('/history') ...

@flask_app.route('/download_log')
def download_log():
    try:
        log_filename = current_settings.get("TEMPERATURE_LOG_FILE", DEFAULT_SETTINGS["TEMPERATURE_LOG_FILE"])
        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(script_dir, log_filename)
        if not os.path.isfile(log_path): return "Error: Log file not found.", 404
        return send_file(log_path, as_attachment=True, download_name=log_filename, mimetype='text/csv')
    except Exception as e: return f"Error sending log file: {e}", 500

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
        # close_watchdog not needed in this version
        if pwm_pump: pwm_pump.stop()
        GPIO.cleanup(); print("Program terminated.")
7

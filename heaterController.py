import os
import glob
import time
import RPi.GPIO as GPIO
import threading
from flask import Flask, render_template_string, jsonify
import collections # Added for deque

# --- Configuration ---
# DS18B20 Sensor Configuration
BASE_DIR = '/sys/bus/w1/devices/'
# !!! IMPORTANT: Replace with your actual sensor IDs after discovering them.
INLET_SENSOR_ID = '28-xxxxxxxxxxxx'  # Replace with your INLET sensor's actual ID
OUTLET_SENSOR_ID = '28-xxxxxxxxxxxx' # Replace with your OUTLET sensor's actual ID

# PWM Pump Control Configuration
PUMP_PWM_PIN = 18
PWM_FREQUENCY = 100
MIN_PUMP_SPEED = 20
MAX_PUMP_SPEED = 100
PUMP_SPEED_STEP = 5
STABILIZATION_TIME_S = 45
LOOP_INTERVAL_S = 300 # 5 minutes

# Control Logic Configuration
MIN_TEMP_DIFFERENCE_TO_RUN = 1.0
MIN_INLET_TEMP_TO_RUN = 10.0
MAX_OUTLET_TEMP_CUTOFF = 75.0

# Thermal Power Calculation Configuration
# !!! IMPORTANT: Estimate your pump's MAX flow rate in Liters Per Minute at 100% speed in your system.
# This is used for a ROUGH power estimation. For accuracy, a flow meter is needed.
MAX_PUMP_FLOW_RATE_LPM = 20.0  # Example: 20 Liters Per Minute. ADJUST THIS!
SPECIFIC_HEAT_CAPACITY_WATER = 4186  # J/kg°C
WATER_DENSITY_KG_L = 1.0  # kg/L (approx)


# --- Web Interface Data & Graph Data ---
app_status = {
    "inlet_temp": "N/A",
    "outlet_temp": "N/A",
    "delta_t": "N/A",
    "pump_speed": 0,
    "thermal_power_watts": "N/A", # Added for estimated power
    "system_message": "Initializing...",
    "optimal_pump_speed_found": MIN_PUMP_SPEED,
    "max_delta_t_found": "N/A",
    "last_update": time.strftime("%Y-%m-%d %H:%M:%S")
}
# Store the last N temperature readings for the graph
MAX_HISTORY_POINTS = 150
temperature_history = collections.deque(maxlen=MAX_HISTORY_POINTS)
data_lock = threading.Lock()

# --- Globals ---
inlet_sensor_file = None
outlet_sensor_file = None
pwm_pump = None
control_thread_running = False

# --- Sensor Functions (Mostly Unchanged) ---
def discover_sensors():
    global inlet_sensor_file, outlet_sensor_file
    try:
        inlet_device_folder = glob.glob(BASE_DIR + INLET_SENSOR_ID)[0]
        inlet_sensor_file = inlet_device_folder + '/w1_slave'
        outlet_device_folder = glob.glob(BASE_DIR + OUTLET_SENSOR_ID)[0]
        outlet_sensor_file = outlet_device_folder + '/w1_slave'
        print(f"Inlet sensor ({INLET_SENSOR_ID}) found at: {inlet_sensor_file}")
        print(f"Outlet sensor ({OUTLET_SENSOR_ID}) found at: {outlet_sensor_file}")
        update_status(system_message="Sensors discovered successfully.")
        return True
    except IndexError:
        errmsg = f"Error: Sensor(s) not found. Check IDs: Inlet='{INLET_SENSOR_ID}', Outlet='{OUTLET_SENSOR_ID}' and 1-Wire setup."
        print(errmsg)
        update_status(system_message=errmsg)
        return False

def read_temp_raw(sensor_file_path):
    if not sensor_file_path: return None
    try:
        with open(sensor_file_path, 'r') as f: lines = f.readlines()
        return lines
    except FileNotFoundError:
        print(f"Error: Sensor file not found at {sensor_file_path}")
        return None
    except Exception as e:
        print(f"Error reading raw sensor data from {sensor_file_path}: {e}")
        return None

def read_temp_c(sensor_file_path):
    lines = read_temp_raw(sensor_file_path)
    if not lines: return None
    read_attempts = 3
    while read_attempts > 0:
        if lines and lines[0].strip().endswith('YES'):
            equals_pos = lines[1].find('t=')
            if equals_pos != -1:
                temp_string = lines[1][equals_pos+2:]
                try:
                    temp_c = float(temp_string) / 1000.0
                    return temp_c
                except ValueError:
                    print(f"Error: Could not parse temperature from {sensor_file_path}. Data: {temp_string}")
                    return None
            else: break
        time.sleep(0.2)
        lines = read_temp_raw(sensor_file_path)
        read_attempts -= 1
    print(f"Error: Could not get a valid reading from {sensor_file_path} after retries.")
    return None

# --- PWM Pump Functions (Unchanged) ---
def setup_pwm():
    global pwm_pump
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PUMP_PWM_PIN, GPIO.OUT)
    if pwm_pump: pwm_pump.stop()
    pwm_pump = GPIO.PWM(PUMP_PWM_PIN, PWM_FREQUENCY)
    pwm_pump.start(0)
    print(f"PWM setup on GPIO {PUMP_PWM_PIN} with frequency {PWM_FREQUENCY} Hz. Pump OFF.")
    update_status(pump_speed=0, system_message="PWM Initialized. Pump is OFF.")

def set_pump_speed(speed_percent):
    global pwm_pump
    if pwm_pump is None:
        print("Error: PWM not initialized. Cannot set pump speed.")
        update_status(system_message="Error: PWM not initialized.")
        return

    current_pump_speed_val = 0
    if speed_percent < 0: current_pump_speed_val = 0
    elif speed_percent > 100: current_pump_speed_val = 100
    else: current_pump_speed_val = speed_percent
    
    actual_duty_cycle = 0
    if current_pump_speed_val > 0:
        if current_pump_speed_val < MIN_PUMP_SPEED: actual_duty_cycle = MIN_PUMP_SPEED
        elif current_pump_speed_val > MAX_PUMP_SPEED: actual_duty_cycle = MAX_PUMP_SPEED
        else: actual_duty_cycle = current_pump_speed_val
    
    pwm_pump.ChangeDutyCycle(float(actual_duty_cycle))
    # Update status with the intended speed for display, actual_duty_cycle is what's applied
    update_status(pump_speed=current_pump_speed_val)


def stop_pump():
    print("Command received to stop pump.")
    set_pump_speed(0) # This will update app_status["pump_speed"] to 0
    update_status(system_message="Pump stopped.", thermal_power_watts="N/A") # Explicitly set power to N/A when stopped


# --- Thermal Power Calculation ---
def calculate_estimated_thermal_power(delta_t_celsius, current_pump_speed_percent):
    """
    Estimates thermal power based on delta_T and an estimated flow rate
    derived from pump speed percentage.
    Returns: Estimated power in Watts as float, or None if inputs are invalid.
    """
    if delta_t_celsius is None or current_pump_speed_percent is None or current_pump_speed_percent == 0:
        return None

    # Estimate flow rate: (current_speed / 100) * MAX_FLOW_RATE_LPM
    # Convert LPM to Liters per second (LPS), then to kg/s (assuming 1L = 1kg)
    estimated_flow_lps = (float(current_pump_speed_percent) / 100.0) * (MAX_PUMP_FLOW_RATE_LPM / 60.0)
    estimated_flow_kgs = estimated_flow_lps * WATER_DENSITY_KG_L

    power_watts = estimated_flow_kgs * SPECIFIC_HEAT_CAPACITY_WATER * delta_t_celsius
    return power_watts

# --- Status and History Update Function ---
def update_status_and_history(inlet_temp=None, outlet_temp=None, delta_t=None, **kwargs):
    """
    Thread-safely updates app_status and adds to temperature_history.
    Also calculates and updates estimated thermal power.
    """
    with data_lock:
        current_time_str = time.strftime("%H:%M:%S")
        
        status_updates = kwargs.copy()
        if inlet_temp is not None: status_updates["inlet_temp"] = f"{inlet_temp:.2f}" if isinstance(inlet_temp, float) else "N/A"
        if outlet_temp is not None: status_updates["outlet_temp"] = f"{outlet_temp:.2f}" if isinstance(outlet_temp, float) else "N/A"
        
        calculated_delta_t = None
        if isinstance(inlet_temp, float) and isinstance(outlet_temp, float):
            calculated_delta_t = outlet_temp - inlet_temp
            status_updates["delta_t"] = f"{calculated_delta_t:.2f}"
        elif delta_t is not None: # If delta_t is passed directly (e.g. already calculated)
             status_updates["delta_t"] = f"{delta_t:.2f}" if isinstance(delta_t, float) else "N/A"
             calculated_delta_t = delta_t if isinstance(delta_t, float) else None
        else:
            status_updates["delta_t"] = "N/A"

        # Calculate and update estimated thermal power
        current_pump_speed_for_power = float(app_status.get("pump_speed", 0)) # Get current pump speed from status
        if calculated_delta_t is not None and current_pump_speed_for_power > 0:
            power_w = calculate_estimated_thermal_power(calculated_delta_t, current_pump_speed_for_power)
            status_updates["thermal_power_watts"] = f"{power_w:.1f}" if power_w is not None else "N/A"
        else:
            status_updates["thermal_power_watts"] = "N/A"
            
        for key, value in status_updates.items():
            if key in app_status:
                app_status[key] = value
        app_status["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # Add to temperature_history
        if isinstance(inlet_temp, float) and isinstance(outlet_temp, float):
            temperature_history.append({
                "time": current_time_str,
                "inlet": round(inlet_temp, 2),
                "outlet": round(outlet_temp, 2)
            })
        elif "pump_speed" not in kwargs and "system_message" not in kwargs: # Avoid logging N/A for simple status/pump updates
             temperature_history.append({
                "time": current_time_str, "inlet": None, "outlet": None
            })

def update_status(**kwargs): # For simple updates not involving full temp/power recalc
    with data_lock:
        for key, value in kwargs.items():
            if key in app_status:
                if isinstance(value, float) and key not in ["inlet_temp", "outlet_temp", "delta_t", "thermal_power_watts"]:
                    app_status[key] = f"{value:.2f}"
                else:
                    app_status[key] = value
        app_status["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")


# --- Main Control Logic ---
def optimize_pump_speed():
    print("Starting pump speed optimization cycle...")
    update_status_and_history(system_message="Optimizing pump speed...") # Logs N/A for temps initially

    current_max_delta_t_this_cycle = -100.0
    current_optimal_speed_this_cycle = MIN_PUMP_SPEED
    
    initial_inlet_temp = read_temp_c(inlet_sensor_file)
    if initial_inlet_temp is None or initial_inlet_temp < MIN_INLET_TEMP_TO_RUN:
        msg = f"Optimization aborted: Inlet temp ({initial_inlet_temp or 'N/A'}°C) < {MIN_INLET_TEMP_TO_RUN}°C."
        print(msg)
        stop_pump()
        update_status_and_history(inlet_temp=initial_inlet_temp, system_message=msg)
        return

    for speed_to_test in range(MIN_PUMP_SPEED, MAX_PUMP_SPEED + 1, PUMP_SPEED_STEP):
        if not control_thread_running: return

        print(f"Optimizing: Testing pump speed: {speed_to_test}%")
        set_pump_speed(speed_to_test) # This updates app_status["pump_speed"]
        # System message will be updated after temp reading for this step
        update_status(system_message=f"Optimizing: Stabilizing at {speed_to_test}%...")


        print(f"  Waiting {STABILIZATION_TIME_S}s for temperatures to stabilize...")
        for _ in range(STABILIZATION_TIME_S):
            if not control_thread_running: return
            time.sleep(1)

        in_temp = read_temp_c(inlet_sensor_file)
        out_temp = read_temp_c(outlet_sensor_file)
        delta_t_val = None
        if in_temp is not None and out_temp is not None:
            delta_t_val = out_temp - in_temp
        
        # Log this reading to history and update current status, including power
        update_status_and_history(inlet_temp=in_temp, outlet_temp=out_temp, delta_t=delta_t_val,
                                  system_message=f"Optimizing: Tested {speed_to_test}%")

        if in_temp is not None and out_temp is not None: # delta_t_val is already set
            print(f"  Speed: {speed_to_test}% -> Inlet: {in_temp:.2f}°C, Outlet: {out_temp:.2f}°C, ΔT: {delta_t_val:.2f}°C, Power: {app_status['thermal_power_watts']}W")

            if delta_t_val > current_max_delta_t_this_cycle:
                current_max_delta_t_this_cycle = delta_t_val
                current_optimal_speed_this_cycle = speed_to_test

            if out_temp > MAX_OUTLET_TEMP_CUTOFF:
                msg = f"SAFETY CUTOFF: Outlet temperature {out_temp:.2f}°C > {MAX_OUTLET_TEMP_CUTOFF}°C. Stopping pump."
                print(msg)
                stop_pump()
                update_status_and_history(outlet_temp=out_temp, system_message=msg) # Log final temp before exit
                return
        else:
            print(f"  Could not read temperatures for speed {speed_to_test}%. Skipping.")
            # update_status_and_history already logged N/A for this point

    if current_max_delta_t_this_cycle >= MIN_TEMP_DIFFERENCE_TO_RUN:
        with data_lock:
            app_status["max_delta_t_found"] = f"{current_max_delta_t_this_cycle:.2f}"
            app_status["optimal_pump_speed_found"] = current_optimal_speed_this_cycle
        msg = f"Optimization complete. Optimal speed: {current_optimal_speed_this_cycle}% (ΔT: {current_max_delta_t_this_cycle:.2f}°C). Running."
        print(msg)
        set_pump_speed(current_optimal_speed_this_cycle)
        # Update status with final decision, power will be recalculated based on optimal speed
        final_in = read_temp_c(inlet_sensor_file)
        final_out = read_temp_c(outlet_sensor_file)
        final_dt = None
        if final_in is not None and final_out is not None: final_dt = final_out - final_in
        update_status_and_history(inlet_temp=final_in, outlet_temp=final_out, delta_t=final_dt, system_message=msg)

    else:
        msg = f"Optimization complete. No speed yielded ΔT >= {MIN_TEMP_DIFFERENCE_TO_RUN}°C. Stopping."
        print(msg)
        with data_lock:
            app_status["max_delta_t_found"] = f"{current_max_delta_t_this_cycle:.2f}" if current_max_delta_t_this_cycle > -100 else "N/A"
        stop_pump() # This also sets power to N/A via update_status
        update_status(system_message=msg)


def control_logic_thread_func():
    global control_thread_running
    if not discover_sensors():
        control_thread_running = False
        return

    setup_pwm()
    time.sleep(1)

    while control_thread_running:
        print("\n--- Control Logic Cycle Start ---")
        update_status(system_message="Checking system conditions...")

        inlet_temp = read_temp_c(inlet_sensor_file)
        outlet_temp = read_temp_c(outlet_sensor_file)
        delta_t_val = None
        if inlet_temp is not None and outlet_temp is not None:
            delta_t_val = outlet_temp - inlet_temp
        
        # This call updates status, history, and estimated power
        update_status_and_history(inlet_temp=inlet_temp, outlet_temp=outlet_temp, delta_t=delta_t_val,
                                  system_message="Checked system conditions.") # Will be overwritten by specific action

        if inlet_temp is not None and outlet_temp is not None:
            print(f"Current Temps - Inlet: {inlet_temp:.2f}°C, Outlet: {outlet_temp:.2f}°C, ΔT: {delta_t_val:.2f}°C, Power: {app_status['thermal_power_watts']}W")

            if outlet_temp > MAX_OUTLET_TEMP_CUTOFF:
                msg = f"SAFETY CUTOFF: Outlet temp {outlet_temp:.2f}°C > {MAX_OUTLET_TEMP_CUTOFF}°C. Stopping."
                print(msg)
                stop_pump()
                update_status(system_message=msg)
            elif inlet_temp < MIN_INLET_TEMP_TO_RUN:
                msg = f"Condition: Inlet temp {inlet_temp:.2f}°C < {MIN_INLET_TEMP_TO_RUN}°C. Pump OFF."
                print(msg)
                if float(app_status.get("pump_speed", "0")) > 0 : stop_pump()
                update_status(system_message=msg)
            elif delta_t_val >= MIN_TEMP_DIFFERENCE_TO_RUN:
                msg = f"Condition: ΔT ({delta_t_val:.2f}°C) sufficient. Optimizing..."
                print(msg)
                update_status(system_message=msg)
                optimize_pump_speed()
            else:
                msg = f"Condition: ΔT ({delta_t_val:.2f}°C) < {MIN_TEMP_DIFFERENCE_TO_RUN}°C. Pump OFF."
                print(msg)
                if float(app_status.get("pump_speed", "0")) > 0 : stop_pump()
                update_status(system_message=msg)
        else:
            errmsg = "Sensor reading error in main loop. Stopping pump for safety."
            print(errmsg)
            stop_pump() # This also sets power to N/A
            update_status(system_message=errmsg)

        print(f"--- Control Logic Cycle End. Waiting {LOOP_INTERVAL_S}s for next cycle. ---")
        for _ in range(LOOP_INTERVAL_S):
            if not control_thread_running: break
            time.sleep(1)
    
    print("Control logic thread is stopping.")
    stop_pump()
    update_status(system_message="Control thread stopped.")

# --- Flask Web Application ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    with data_lock:
        current_display_status = app_status.copy()
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <meta http-equiv="refresh" content="10">
        <title>Solar Heater Control Status</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background-color: #f0f2f5; color: #333; display: flex; flex-direction: column; align-items: center; min-height: 100vh; padding-top: 20px;}
            .container { background-color: #ffffff; padding: 25px 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 90%; max-width: 800px; margin-bottom: 20px; }
            h1 { color: #0056b3; text-align: center; margin-bottom: 25px; font-size: 1.8em;}
            .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 18px; margin-top: 20px; } /* Adjusted minmax */
            .status-item { background-color: #e9ecef; padding: 18px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); transition: transform 0.2s ease-in-out; }
            .status-item:hover { transform: translateY(-3px); }
            .status-item strong { color: #0056b3; font-weight: 600; display: block; margin-bottom: 8px; font-size: 0.95em;}
            .status-item span { font-size: 1.1em; font-weight: 500; }
            .message-box { margin-top: 25px; padding: 18px; background-color: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; border-radius: 8px; text-align: center; font-weight: 500; font-size: 1.05em;}
            .chart-container { background-color: #ffffff; padding: 20px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); width: 90%; max-width: 800px; margin-top: 10px; }
            footer { text-align: center; margin-top: 30px; font-size: 0.85em; color: #777; padding-bottom: 20px; }
            /* @media (max-width: 600px) { Removed to allow more items with auto-fit
                 .status-grid { grid-template-columns: 1fr; } 
            }*/
            @media (max-width: 700px) { /* General responsiveness for smaller screens */
                 h1 { font-size: 1.5em; }
                .container, .chart-container { width: 95%; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Solar Heater Status</h1>
            <div class="message-box">{{ status.system_message }}</div>
            <div class="status-grid">
                <div class="status-item"><strong>Inlet Temp:</strong> <span>{{ status.inlet_temp }} °C</span></div>
                <div class="status-item"><strong>Outlet Temp:</strong> <span>{{ status.outlet_temp }} °C</span></div>
                <div class="status-item"><strong>Delta T:</strong> <span>{{ status.delta_t }} °C</span></div>
                <div class="status-item"><strong>Pump Speed:</strong> <span>{{ status.pump_speed }} %</span></div>
                <div class="status-item"><strong>Est. Power:</strong> <span>{{ status.thermal_power_watts }} W</span></div>
                <div class="status-item"><strong>Optimal Speed:</strong> <span>{{ status.optimal_pump_speed_found }} %</span></div>
                <div class="status-item"><strong>Max Delta T Found:</strong> <span>{{ status.max_delta_t_found }} °C</span></div>
            </div>
        </div>

        <div class="chart-container">
            <canvas id="temperatureChart" height="300"></canvas> </div>

        <footer>Last Update: {{ status.last_update }} <br/> (Page auto-refreshes every 10 seconds)</footer>

        <script>
            let tempChart;
            async function fetchGraphData() {
                try {
                    const response = await fetch('/graph_data');
                    if (!response.ok) {
                        console.error('Failed to fetch graph data:', response.status);
                        return;
                    }
                    const data = await response.json();
                    
                    const labels = data.map(d => d.time);
                    const inletTemps = data.map(d => d.inlet); // Will be null if data was N/A
                    const outletTemps = data.map(d => d.outlet); // Will be null if data was N/A

                    const chartData = {
                        labels: labels,
                        datasets: [
                            {
                                label: 'Inlet Temp (°C)',
                                data: inletTemps,
                                borderColor: 'rgb(54, 162, 235)',
                                backgroundColor: 'rgba(54, 162, 235, 0.1)',
                                tension: 0.1,
                                spanGaps: true 
                            },
                            {
                                label: 'Outlet Temp (°C)',
                                data: outletTemps,
                                borderColor: 'rgb(255, 99, 132)',
                                backgroundColor: 'rgba(255, 99, 132, 0.1)',
                                tension: 0.1,
                                spanGaps: true
                            }
                        ]
                    };

                    const ctx = document.getElementById('temperatureChart').getContext('2d');
                    if (tempChart) {
                        tempChart.data = chartData;
                        tempChart.update('none'); // 'none' for no animation, for smoother updates
                    } else {
                        tempChart = new Chart(ctx, {
                            type: 'line',
                            data: chartData,
                            options: {
                                responsive: true,
                                maintainAspectRatio: false,
                                animation: { duration: 0 }, // Disable animation for new chart too
                                scales: {
                                    y: {
                                        beginAtZero: false, 
                                        title: { display: true, text: 'Temperature (°C)'}
                                    },
                                    x: {
                                        title: { display: true, text: 'Time'}
                                    }
                                },
                                plugins: {
                                    legend: { position: 'top' },
                                    title: { display: true, text: 'Temperature Trends' }
                                }
                            }
                        });
                    }
                } catch (error) {
                    console.error('Error fetching or processing graph data:', error);
                }
            }
            // Fetch data immediately on load, then rely on meta refresh to call it again.
            document.addEventListener('DOMContentLoaded', fetchGraphData);
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, status=current_display_status)

@flask_app.route('/graph_data')
def get_graph_data():
    with data_lock:
        return jsonify(list(temperature_history))

# --- Main Execution ---
if __name__ == '__main__':
    control_thread = None
    try:
        print("Initializing Solar Heater Controller with Web Interface...")
        print("Loading 1-Wire kernel modules (requires permissions)...")
        os.system('sudo modprobe w1-gpio')
        os.system('sudo modprobe w1-therm')
        time.sleep(1)

        print("Starting control logic thread...")
        control_thread_running = True
        control_thread = threading.Thread(target=control_logic_thread_func, daemon=True)
        control_thread.start()

        print("Starting Flask web server on http://0.0.0.0:5000")
        update_status(system_message="Web server started. Control logic initializing...")
        flask_app.run(host='0.0.0.0', port=5000, debug=False)

    except KeyboardInterrupt:
        print("\nCtrl+C received. Shutting down...")
    except Exception as e:
        print(f"An critical error occurred in main execution: {e}")
    finally:
        print("Initiating cleanup sequence...")
        control_thread_running = False
        if control_thread and control_thread.is_alive():
            print("Waiting for control thread to complete...")
            control_thread.join(timeout=10)
            if control_thread.is_alive(): print("Control thread did not stop in time.")
        else: print("Control thread already stopped or not started.")
        if pwm_pump:
            print("Stopping PWM output...")
            pwm_pump.stop()
        print("Cleaning up GPIO settings...")
        GPIO.cleanup()
        print("Program terminated.")
